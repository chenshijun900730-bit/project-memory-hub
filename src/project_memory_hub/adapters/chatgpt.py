from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from project_memory_hub.adapters.base import IngestionError, ReconcileRequiredError
from project_memory_hub.discovery.fingerprint import fingerprint_git_remote
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    Namespace,
    NamespaceVerification,
    NormalizedTaskRecord,
    SourceAgent,
)
from project_memory_hub.security.archive import (
    ArchiveLimits,
    SafeZipReader,
    UnsafeArchiveError,
)
from project_memory_hub.security.capture_privacy import (
    CapturePrivacyCanonicalizer,
    MAX_CAPTURE_BYTES,
    MAX_FIELD_BYTES,
    MAX_LIST_ITEMS,
)
from project_memory_hub.security.identifiers import safe_persisted_identifier
from project_memory_hub.security.redaction import Redactor, normalize_redacted_text
from project_memory_hub.services.capture import (
    CaptureService,
    PreparedVerifiedCapture,
    _IncompatibleSourceProvenance,
    _ProjectIdentityChanged,
)
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import (
    Database,
    ReadonlyDatabaseSnapshot,
    ReadonlySnapshotChangedError,
)
import project_memory_hub.storage.path_identity as path_identity_module
from project_memory_hub.utf8 import (
    InvalidUtf8Text,
    contains_unsafe_text_control,
    strict_utf8_size,
)


_CONVERSATION_MEMBER = re.compile(r"^conversations(?:-([0-9]+))?[.]json$")
_LABEL = re.compile(
    r"^(Decision|Verified|Outcome|Failed|Preference|Risk|Open issue|Resolved issue)"
    r"\s*:\s*(.*)$",
    re.IGNORECASE,
)
_REMOTE = re.compile(r"(?:https?://|ssh://|git@)[^\s\"'<>]+", re.IGNORECASE)
_CODING_EVIDENCE = re.compile(
    r"(?:\b(?:git|pytest|uv|npm|pnpm|yarn|cargo|go)\b|"
    r"[A-Za-z0-9_./-]+[.](?:py|js|jsx|ts|tsx|go|rs|java|swift|rb|php|c|cc|cpp)\b|"
    r"```)",
    re.IGNORECASE,
)
_SAFE_MODEL_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class VisibleMessage:
    role: str
    text: str
    model_slug: str | None
    create_time: float | None = None
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedConversation:
    conversation_id: str
    title: str
    messages: tuple[VisibleMessage, ...]

    @classmethod
    def synthetic(cls, conversation_id: str, text: str) -> NormalizedConversation:
        return cls(
            conversation_id=conversation_id,
            title="Synthetic",
            messages=(VisibleMessage("user", text, None),),
        )

    @property
    def searchable_text(self) -> str:
        return "\n".join((self.title, *(message.text for message in self.messages)))


@dataclass(frozen=True, slots=True)
class ProjectMatch:
    project_id: UUID | None
    project_path: Path | None
    confidence: float
    evidence: tuple[str, ...]
    requires_confirmation: bool
    matched_project_ids: tuple[UUID, ...] = ()
    blocked_by_disabled: bool = False


@dataclass(frozen=True, slots=True)
class ConversationImportResult:
    conversation_id: str
    status: str
    confidence: float
    evidence: tuple[str, ...]
    model_id: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    archive_hash: str
    dry_run: bool
    imported_count: int
    duplicate_count: int
    confirmation_count: int
    processed_members: tuple[str, ...]
    processed_conversation_ids: tuple[str, ...]
    results: tuple[ConversationImportResult, ...]
    warnings: tuple[str, ...]
    resolved_count: int = 0
    already_resolved_count: int = 0
    unmatched_resolution_count: int = 0
    warning_count: int = 0


@dataclass(frozen=True, slots=True)
class _ConversationCommit:
    receipt_duplicate: bool
    capture_results: tuple[CaptureResult, ...] = ()


@dataclass(frozen=True, slots=True)
class _ProjectRow:
    project_id: UUID
    canonical_path: Path
    display_name: str
    remote_fingerprint: str | None
    path_device: int
    path_inode: int
    enabled: bool
    live_identity: path_identity_module.PathIdentity | None = None


@dataclass(frozen=True, slots=True)
class _ProjectSnapshot:
    generation: int
    projects: tuple[_ProjectRow, ...]


class _ConversationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProjectMatcher:
    def __init__(self, database: Database | ReadonlyDatabaseSnapshot) -> None:
        self._database = database

    def match(
        self,
        conversation: NormalizedConversation,
        *,
        project_snapshot: _ProjectSnapshot | None = None,
    ) -> ProjectMatch:
        if project_snapshot is None:
            project_snapshot = self.verified_project_snapshot()
        else:
            self.assert_project_snapshot_current(project_snapshot)
        projects = project_snapshot.projects
        text = conversation.searchable_text
        folded = text.casefold()
        has_coding_evidence = _CODING_EVIDENCE.search(text) is not None
        candidates: dict[UUID, tuple[_ProjectRow, float, set[str]]] = {}

        for project in projects:
            escaped_path = re.escape(str(project.canonical_path))
            if re.search(rf"(?<![\w./~%+-]){escaped_path}(?![\w.~%+-])", text):
                candidates[project.project_id] = (
                    project,
                    1.0,
                    {"absolute_path"},
                )

        for raw_remote in _REMOTE.findall(text):
            sanitized = raw_remote.rstrip(".,;:)")
            try:
                remote_fingerprint = fingerprint_git_remote(sanitized)
            except ValueError:
                continue
            for project in projects:
                if (
                    project.remote_fingerprint is not None
                    and project.remote_fingerprint == remote_fingerprint
                ):
                    _merge_candidate(
                        candidates,
                        project,
                        0.95,
                        "exact_remote",
                    )

        if has_coding_evidence:
            name_groups: dict[str, list[_ProjectRow]] = {}
            for project in projects:
                project_names = {
                    project.display_name.casefold(),
                    project.canonical_path.name.casefold(),
                }
                for name in project_names:
                    name_groups.setdefault(name, []).append(project)
            for name, rows in name_groups.items():
                if not name or not _contains_name(folded, name):
                    continue
                for project in rows:
                    _merge_candidate(
                        candidates,
                        project,
                        0.85,
                        "exact_project_name",
                    )

        if not candidates:
            return ProjectMatch(None, None, 0.0, (), True)

        ranked_candidates = _prefer_nested_absolute_path_matches(tuple(candidates.values()))
        enabled_candidates = tuple(value for value in ranked_candidates if value[0].enabled)
        disabled_candidates = tuple(value for value in ranked_candidates if not value[0].enabled)
        if not enabled_candidates:
            highest = max(value[1] for value in disabled_candidates)
            evidence = tuple(
                sorted(
                    {
                        item
                        for value in disabled_candidates
                        if value[1] == highest
                        for item in value[2]
                    }
                )
            )
            return ProjectMatch(None, None, highest, evidence, True, (), True)

        highest = max(value[1] for value in enabled_candidates)
        winners = [value for value in enabled_candidates if value[1] == highest]
        disabled_blockers = tuple(
            value
            for value in disabled_candidates
            if value[1] >= highest
            or any(
                _is_same_or_nested_path(
                    value[0].canonical_path,
                    winner[0].canonical_path,
                )
                for winner in winners
            )
        )
        if disabled_blockers:
            blocked_values = (*winners, *disabled_blockers)
            return ProjectMatch(
                None,
                None,
                max(value[1] for value in blocked_values),
                tuple(sorted({item for value in blocked_values for item in value[2]})),
                True,
                (),
                True,
            )

        evidence = tuple(sorted({item for value in winners for item in value[2]}))
        matched_project_ids = tuple(sorted((value[0].project_id for value in winners), key=str))
        if any(not self._project_is_current(value[0]) for value in winners):
            raise ReconcileRequiredError("project registry requires reconcile")
        if len(winners) != 1 or highest < 0.85:
            return ProjectMatch(
                None,
                None,
                highest,
                evidence,
                True,
                matched_project_ids,
            )
        project = winners[0][0]
        return ProjectMatch(
            project.project_id,
            project.canonical_path,
            highest,
            evidence,
            False,
            matched_project_ids,
        )

    def verified_project_snapshot(self) -> _ProjectSnapshot:
        try:
            with self._database.connect(readonly=True) as connection:
                generation = self._project_generation(connection)
                projects = self._project_rows(connection)
                if self._project_generation(connection) != generation:
                    raise ReconcileRequiredError("project registry requires reconcile")
        except ReadonlySnapshotChangedError:
            raise ReconcileRequiredError("project registry requires reconcile") from None
        observed_projects: list[_ProjectRow] = []
        for project in projects:
            if not project.enabled:
                observed_projects.append(project)
                continue
            live_identity = path_identity_module.validated_persisted_directory_identity(
                project.canonical_path,
                project.path_device,
                project.path_inode,
            )
            if live_identity is None:
                raise ReconcileRequiredError("project registry requires reconcile")
            observed_projects.append(replace(project, live_identity=live_identity))
        snapshot = _ProjectSnapshot(generation, tuple(observed_projects))
        try:
            with self._database.connect(readonly=True) as connection:
                if self._project_generation(connection) != snapshot.generation:
                    raise ReconcileRequiredError("project registry requires reconcile")
                if any(
                    project.enabled and not self._path_identity_is_current(project)
                    for project in snapshot.projects
                ):
                    raise ReconcileRequiredError("project registry requires reconcile")
                if self._project_generation(connection) != snapshot.generation:
                    raise ReconcileRequiredError("project registry requires reconcile")
        except ReadonlySnapshotChangedError:
            raise ReconcileRequiredError("project registry requires reconcile") from None
        return snapshot

    def assert_project_snapshot_current(
        self,
        snapshot: _ProjectSnapshot,
        connection: sqlite3.Connection | None = None,
        *,
        validate_paths: bool = False,
    ) -> None:
        if connection is None:
            try:
                with self._database.connect(readonly=True) as selected_connection:
                    self._assert_project_snapshot_on_connection(
                        selected_connection,
                        snapshot,
                        validate_paths=validate_paths,
                    )
            except ReadonlySnapshotChangedError:
                raise ReconcileRequiredError("project registry requires reconcile") from None
            return
        self._assert_project_snapshot_on_connection(
            connection,
            snapshot,
            validate_paths=validate_paths,
        )

    def assert_project_match_current(
        self,
        project_match: ProjectMatch,
        snapshot: _ProjectSnapshot,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.assert_project_snapshot_current(snapshot, connection)
        by_id = {project.project_id: project for project in snapshot.projects}
        for project_id in project_match.matched_project_ids:
            project = by_id.get(project_id)
            if project is None or not self._path_identity_is_current(project):
                raise ReconcileRequiredError("project registry requires reconcile")

    def _assert_project_snapshot_on_connection(
        self,
        connection: sqlite3.Connection,
        snapshot: _ProjectSnapshot,
        *,
        validate_paths: bool,
    ) -> None:
        if self._project_generation(connection) != snapshot.generation:
            raise ReconcileRequiredError("project registry requires reconcile")
        if validate_paths:
            if any(
                project.enabled and not self._path_identity_is_current(project)
                for project in snapshot.projects
            ):
                raise ReconcileRequiredError("project registry requires reconcile")
            if self._project_generation(connection) != snapshot.generation:
                raise ReconcileRequiredError("project registry requires reconcile")
            if any(
                project.enabled and not self._path_identity_is_current(project)
                for project in snapshot.projects
            ):
                raise ReconcileRequiredError("project registry requires reconcile")

    def _project_is_current(self, project: _ProjectRow) -> bool:
        try:
            with self._database.connect(readonly=True) as connection:
                row = connection.execute(
                    """
                    select canonical_path, display_name, git_remote_fingerprint,
                           path_device, path_inode
                    from projects
                    where project_id = ? and enabled = 1
                    """,
                    (str(project.project_id).lower(),),
                ).fetchone()
        except ReadonlySnapshotChangedError:
            raise ReconcileRequiredError("project registry requires reconcile") from None
        if row is None:
            return False
        if (
            row["canonical_path"] != str(project.canonical_path)
            or row["display_name"] != project.display_name
            or row["git_remote_fingerprint"] != project.remote_fingerprint
            or row["path_device"] != project.path_device
            or row["path_inode"] != project.path_inode
        ):
            return False
        return self._path_identity_is_current(project)

    @staticmethod
    def _path_identity_is_current(project: _ProjectRow) -> bool:
        return (
            project.live_identity is not None
            and path_identity_module.complete_directory_identity(project.canonical_path)
            == project.live_identity
        )

    @staticmethod
    def _project_generation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()
        if row is None or type(row["generation"]) is not int or row["generation"] < 0:
            raise ReconcileRequiredError("project registry requires reconcile")
        return row["generation"]

    @staticmethod
    def _project_rows(connection: sqlite3.Connection) -> tuple[_ProjectRow, ...]:
        rows = connection.execute(
            """
            select project_id, canonical_path, display_name,
                   git_remote_fingerprint, path_device, path_inode, enabled
            from projects
            order by canonical_path, project_id
            """
        ).fetchall()
        return tuple(
            _ProjectRow(
                project_id=UUID(row["project_id"]),
                canonical_path=Path(row["canonical_path"]),
                display_name=row["display_name"],
                remote_fingerprint=row["git_remote_fingerprint"],
                path_device=row["path_device"],
                path_inode=row["path_inode"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        )


class ExplicitTaskExtractor:
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._canonicalizer = CapturePrivacyCanonicalizer(redactor)

    def select_completed_segment(
        self, conversation: NormalizedConversation
    ) -> NormalizedConversation | None:
        for index in range(len(conversation.messages) - 1, 0, -1):
            assistant = conversation.messages[index]
            user = conversation.messages[index - 1]
            if assistant.role != "assistant" or user.role != "user":
                continue
            redacted = self._redactor.redact(assistant.text).text
            if not any(
                _LABEL.fullmatch(line.strip()) is not None for line in redacted.splitlines()
            ):
                continue
            return NormalizedConversation(
                conversation_id=conversation.conversation_id,
                title="",
                messages=(user, assistant),
            )
        return None

    def extract(
        self,
        conversation: NormalizedConversation,
        *,
        project_path: Path | None = None,
    ) -> list[NormalizedTaskRecord]:
        if project_path is None:
            return []
        labels: dict[str, list[str]] = {
            "decision": [],
            "verified": [],
            "outcome": [],
            "failed": [],
            "preference": [],
            "risk": [],
            "open issue": [],
            "resolved issue": [],
        }
        model_id = "unknown"
        verified_at = datetime.now(timezone.utc)
        for message in reversed(conversation.messages):
            if message.role != "assistant":
                continue
            candidate_labels: dict[str, list[str]] = {key: [] for key in labels}
            redacted = self._redactor.redact(message.text).text
            invalid_capture = False
            for line in redacted.splitlines():
                match = _LABEL.fullmatch(line.strip())
                if match is None:
                    continue
                key = match.group(1).casefold()
                content = match.group(2).strip()
                if not content:
                    if key == "resolved issue":
                        invalid_capture = True
                        break
                    continue
                content = normalize_redacted_text(self._redactor, content)
                if not content:
                    if key == "resolved issue":
                        invalid_capture = True
                        break
                    continue
                try:
                    content_too_large = strict_utf8_size(content) > MAX_FIELD_BYTES
                except InvalidUtf8Text:
                    return []
                if content_too_large or len(candidate_labels[key]) >= MAX_LIST_ITEMS:
                    invalid_capture = True
                    break
                candidate_labels[key].append(content)
            if invalid_capture:
                return []
            if not any(candidate_labels.values()):
                continue
            candidate_labels["resolved issue"] = list(
                dict.fromkeys(candidate_labels["resolved issue"])
            )
            if set(candidate_labels["open issue"]).intersection(candidate_labels["resolved issue"]):
                return []
            labels = candidate_labels
            model_id = _model_id(message.model_slug, self._redactor)
            if message.create_time is not None:
                try:
                    verified_at = datetime.fromtimestamp(message.create_time, timezone.utc)
                except (OverflowError, OSError, ValueError):
                    pass
            break
        if not any(labels.values()):
            return []
        outcome = "; ".join(labels["outcome"])
        if not _chatgpt_labels_fit_capture(labels, outcome):
            return []
        namespace = Namespace(source_agent=SourceAgent.CHATGPT, model_id=model_id)
        source_record_id = conversation.conversation_id
        record = NormalizedTaskRecord(
            cwd=project_path,
            namespace=namespace,
            source_record_id=source_record_id,
            objective="ChatGPT exported task",
            outcome=outcome,
            decisions=tuple(labels["decision"]),
            failed_attempts=tuple(labels["failed"]),
            verified_commands=tuple(labels["verified"]),
            preferences=tuple(labels["preference"]),
            risks=tuple(labels["risk"]),
            open_issues=tuple(labels["open issue"]),
            resolved_open_issues=tuple(labels["resolved issue"]),
            verification=NamespaceVerification(
                namespace=namespace,
                source_record_id=source_record_id,
                verified_by="chatgpt_adapter",
                verified_at=verified_at,
            ),
        )
        try:
            self._canonicalizer.portable_structure(_capture_payload(record))
        except ValueError:
            return []
        return [record]


def _chatgpt_labels_fit_capture(labels: dict[str, list[str]], outcome: str) -> bool:
    try:
        if strict_utf8_size(outcome) > MAX_FIELD_BYTES:
            return False
        total_bytes = strict_utf8_size("ChatGPT exported task") + strict_utf8_size(outcome)
        total_bytes += sum(
            strict_utf8_size(value)
            for key, values in labels.items()
            if key != "outcome"
            for value in values
        )
    except InvalidUtf8Text:
        return False
    return total_bytes <= MAX_CAPTURE_BYTES


class ChatGPTExportAdapter:
    source_agent = SourceAgent.CHATGPT

    def __init__(
        self,
        *,
        matcher: ProjectMatcher,
        extractor: ExplicitTaskExtractor,
        capture: CaptureService,
        checkpoints: CheckpointRepository,
        redactor: Redactor,
        database: Database | ReadonlyDatabaseSnapshot | None = None,
        archive_limits: ArchiveLimits = ArchiveLimits(),
        max_numbered_members: int = 10_000,
        max_conversations: int = 100_000,
        max_nodes_per_conversation: int = 20_000,
        max_conversation_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        limits = (
            max_numbered_members,
            max_conversations,
            max_nodes_per_conversation,
            max_conversation_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("ChatGPT adapter limits must be positive integers")
        self._matcher = matcher
        self._extractor = extractor
        self._capture = capture
        self._checkpoints = checkpoints
        self._database = checkpoints.database if database is None else database
        self._redactor = redactor
        self._archive_limits = archive_limits
        self._max_numbered_members = max_numbered_members
        self._max_conversations = max_conversations
        self._max_nodes = max_nodes_per_conversation
        self._max_conversation_bytes = max_conversation_bytes

    def import_zip(self, path: Path, *, dry_run: bool = False) -> ImportReport:
        try:
            return self._import_zip(path, dry_run=dry_run)
        except ReadonlySnapshotChangedError:
            raise ReconcileRequiredError("project registry requires reconcile") from None

    def _commit_conversation(
        self,
        *,
        archive_hash: str,
        conversation_id: str,
        project_snapshot: _ProjectSnapshot,
        project_match: ProjectMatch | None = None,
        confirmation: dict[str, object] | None = None,
        prepared_captures: tuple[PreparedVerifiedCapture, ...] = (),
    ) -> _ConversationCommit:
        def guard(connection: sqlite3.Connection) -> None:
            if not prepared_captures:
                self._matcher.assert_project_snapshot_current(
                    project_snapshot,
                    connection,
                    validate_paths=True,
                )
                return
            if project_match is None or not project_match.matched_project_ids:
                raise IngestionError("ChatGPT capture project match missing")
            self._matcher.assert_project_match_current(
                project_match,
                project_snapshot,
                connection,
            )

        try:
            with self._database.transaction() as connection:
                if self._checkpoints.receipt_exists_on_connection(
                    connection,
                    archive_hash,
                    conversation_id,
                    SourceAgent.CHATGPT,
                ):
                    return _ConversationCommit(receipt_duplicate=True)
                guard(connection)
                capture_results: list[CaptureResult] = []
                for prepared in prepared_captures:
                    result = self._capture.capture_prepared_on_connection(
                        connection,
                        prepared,
                    )
                    if result.status not in {
                        "inserted",
                        "duplicate",
                        "resolved",
                        "partial",
                    }:
                        raise IngestionError("ChatGPT capture was not accepted")
                    capture_results.append(result)
                self._checkpoints.commit_import_receipt_on_connection(
                    connection,
                    archive_hash,
                    conversation_id,
                    SourceAgent.CHATGPT,
                    confirmation=confirmation,
                )
                guard(connection)
                return _ConversationCommit(
                    receipt_duplicate=False,
                    capture_results=tuple(capture_results),
                )
        except _ProjectIdentityChanged:
            raise ReconcileRequiredError("project registry requires reconcile") from None
        except _IncompatibleSourceProvenance:
            raise IngestionError("ChatGPT capture provenance mismatch") from None

    def _import_zip(self, path: Path, *, dry_run: bool) -> ImportReport:
        project_snapshot = self._matcher.verified_project_snapshot()

        snapshot = SafeZipReader(path, self._archive_limits).read_json_snapshot(
            set(),
            name_pattern=_CONVERSATION_MEMBER,
        )
        archive_hash = snapshot.sha256
        loaded = dict(snapshot.members)
        numeric_members: dict[int, str] = {}
        for name in snapshot.validated_names:
            match = _CONVERSATION_MEMBER.fullmatch(name)
            if match is None or match.group(1) is None:
                continue
            try:
                number_text = match.group(1)
                number = int(number_text)
            except ValueError:
                raise UnsafeArchiveError("conversation member rejected") from None
            if number < 1 or number > self._max_numbered_members or number_text != str(number):
                raise UnsafeArchiveError("conversation member limit exceeded")
            if number in numeric_members:
                raise UnsafeArchiveError("duplicate numeric conversation member")
            numeric_members[number] = name
        member_names = tuple(sorted(loaded, key=_member_order))
        ordered_values: list[object] = []
        preflight_ids: set[str] = set()
        for member_name in member_names:
            values = loaded[member_name]
            if not isinstance(values, list):
                raise UnsafeArchiveError("invalid conversations member")
            if len(ordered_values) + len(values) > self._max_conversations:
                raise UnsafeArchiveError("conversation count limit exceeded")
            for value in values:
                ordered_values.append(value)
                if not isinstance(value, dict):
                    continue
                try:
                    conversation_id = _conversation_id(value.get("id"), self._redactor)
                except _ConversationError:
                    continue
                if conversation_id in preflight_ids:
                    raise UnsafeArchiveError("duplicate conversation id")
                preflight_ids.add(conversation_id)

        warnings: Counter[str] = Counter()
        results: list[ConversationImportResult] = []
        processed_ids: list[str] = []
        imported_count = duplicate_count = confirmation_count = 0
        resolved_count = already_resolved_count = unmatched_resolution_count = 0

        def append_duplicate(conversation_id: str) -> None:
            nonlocal duplicate_count
            duplicate_count += 1
            results.append(
                ConversationImportResult(
                    conversation_id,
                    "duplicate",
                    0.0,
                    (),
                    "unknown",
                )
            )

        for value in ordered_values:
            try:
                if not isinstance(value, dict):
                    raise _ConversationError("malformed_conversation")
                conversation_id = _conversation_id(value.get("id"), self._redactor)
            except _ConversationError as error:
                warnings[error.code] += 1
                continue
            processed_ids.append(conversation_id)
            if self._checkpoints.receipt_exists(
                archive_hash,
                conversation_id,
                SourceAgent.CHATGPT,
            ):
                append_duplicate(conversation_id)
                continue
            try:
                conversation = self._normalize_conversation(value)
            except _ConversationError as error:
                if dry_run:
                    warnings[error.code] += 1
                    continue
                commit = self._commit_conversation(
                    archive_hash=archive_hash,
                    conversation_id=conversation_id,
                    project_snapshot=project_snapshot,
                )
                if commit.receipt_duplicate:
                    append_duplicate(conversation_id)
                else:
                    warnings[error.code] += 1
                continue
            except (OverflowError, RecursionError, TypeError, ValueError):
                if dry_run:
                    warnings["malformed_conversation"] += 1
                    continue
                commit = self._commit_conversation(
                    archive_hash=archive_hash,
                    conversation_id=conversation_id,
                    project_snapshot=project_snapshot,
                )
                if commit.receipt_duplicate:
                    append_duplicate(conversation_id)
                else:
                    warnings["malformed_conversation"] += 1
                continue
            segment = self._extractor.select_completed_segment(conversation)
            if segment is None:
                if dry_run:
                    warnings["no_completed_task_segment"] += 1
                    continue
                commit = self._commit_conversation(
                    archive_hash=archive_hash,
                    conversation_id=conversation.conversation_id,
                    project_snapshot=project_snapshot,
                )
                if commit.receipt_duplicate:
                    append_duplicate(conversation.conversation_id)
                else:
                    warnings["no_completed_task_segment"] += 1
                continue
            project_match = self._matcher.match(
                segment,
                project_snapshot=project_snapshot,
            )
            if project_match.requires_confirmation or project_match.project_path is None:
                if project_match.blocked_by_disabled:
                    confirmation_count += 1
                    warnings["disabled_project_match"] += 1
                    results.append(
                        ConversationImportResult(
                            conversation.conversation_id,
                            "confirmation_required",
                            project_match.confidence,
                            project_match.evidence,
                            "unknown",
                        )
                    )
                    continue
                if dry_run:
                    confirmation_count += 1
                    results.append(
                        ConversationImportResult(
                            conversation.conversation_id,
                            "confirmation_required",
                            project_match.confidence,
                            project_match.evidence,
                            "unknown",
                        )
                    )
                    continue
                commit = self._commit_conversation(
                    archive_hash=archive_hash,
                    conversation_id=conversation.conversation_id,
                    project_snapshot=project_snapshot,
                    project_match=project_match,
                    confirmation={
                        "confidence": project_match.confidence,
                        "conversation_id": conversation.conversation_id,
                        "evidence": list(project_match.evidence),
                        "status": "confirmation_required",
                    },
                )
                if commit.receipt_duplicate:
                    append_duplicate(conversation.conversation_id)
                    continue
                confirmation_count += 1
                results.append(
                    ConversationImportResult(
                        conversation.conversation_id,
                        "confirmation_required",
                        project_match.confidence,
                        project_match.evidence,
                        "unknown",
                    )
                )
                continue
            records = self._extractor.extract(
                segment,
                project_path=project_match.project_path,
            )
            self._matcher.assert_project_match_current(
                project_match,
                project_snapshot,
            )
            if not records:
                if dry_run:
                    warnings["no_explicit_statements"] += 1
                    continue
                commit = self._commit_conversation(
                    archive_hash=archive_hash,
                    conversation_id=conversation.conversation_id,
                    project_snapshot=project_snapshot,
                    project_match=project_match,
                )
                if commit.receipt_duplicate:
                    append_duplicate(conversation.conversation_id)
                else:
                    warnings["no_explicit_statements"] += 1
                continue
            for record in records:
                _require_record_capture_binding(
                    record,
                    conversation_id=conversation.conversation_id,
                    project_match=project_match,
                )
            model_id = records[0].namespace.model_id
            prepared_captures: list[PreparedVerifiedCapture] = []
            for record in records:
                payload = _capture_payload(record)
                prepared = self._capture.prepare_verified(
                    payload,
                    record.verification,
                )
                if isinstance(prepared, CaptureResult):
                    self._matcher.assert_project_match_current(
                        project_match,
                        project_snapshot,
                    )
                    raise IngestionError(f"ChatGPT capture preparation rejected: {prepared.status}")
                _require_prepared_capture_binding(
                    prepared,
                    record=record,
                    payload=payload,
                    conversation_id=conversation.conversation_id,
                    project_match=project_match,
                )
                prepared_captures.append(prepared)
            try:
                for prepared in prepared_captures:
                    self._capture.validate_prepared_readonly(prepared)
            except _ProjectIdentityChanged:
                raise ReconcileRequiredError("project registry requires reconcile") from None
            except _IncompatibleSourceProvenance:
                raise IngestionError("ChatGPT capture provenance mismatch") from None
            if dry_run:
                imported_count += 1
                results.append(
                    ConversationImportResult(
                        conversation.conversation_id,
                        "would_import",
                        project_match.confidence,
                        project_match.evidence,
                        model_id,
                    )
                )
                continue
            self._matcher.assert_project_match_current(
                project_match,
                project_snapshot,
            )
            commit = self._commit_conversation(
                archive_hash=archive_hash,
                conversation_id=conversation.conversation_id,
                project_snapshot=project_snapshot,
                project_match=project_match,
                prepared_captures=tuple(prepared_captures),
            )
            if commit.receipt_duplicate:
                append_duplicate(conversation.conversation_id)
                continue
            capture_results = commit.capture_results
            capture_duplicate = bool(capture_results) and all(
                result.status == "duplicate"
                and not result.inserted_ids
                and result.resolved_count == 0
                and result.already_resolved_count == 0
                and result.unmatched_resolution_count == 0
                for result in capture_results
            )
            if capture_duplicate:
                append_duplicate(conversation.conversation_id)
                continue
            conversation_resolved = sum(result.resolved_count for result in capture_results)
            conversation_already_resolved = sum(
                result.already_resolved_count for result in capture_results
            )
            conversation_unmatched = sum(
                result.unmatched_resolution_count for result in capture_results
            )
            imported_count += 1
            resolved_count += conversation_resolved
            already_resolved_count += conversation_already_resolved
            unmatched_resolution_count += conversation_unmatched
            warnings["resolution_not_found"] += conversation_unmatched
            results.append(
                ConversationImportResult(
                    conversation.conversation_id,
                    "imported",
                    project_match.confidence,
                    project_match.evidence,
                    model_id,
                )
            )

        self._matcher.assert_project_snapshot_current(project_snapshot)
        return ImportReport(
            archive_hash=archive_hash,
            dry_run=dry_run,
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            confirmation_count=confirmation_count,
            processed_members=member_names,
            processed_conversation_ids=tuple(processed_ids),
            results=tuple(results),
            warnings=_warnings(warnings),
            resolved_count=resolved_count,
            already_resolved_count=already_resolved_count,
            unmatched_resolution_count=unmatched_resolution_count,
            warning_count=sum(warnings.values()),
        )

    def _normalize_conversation(self, value: object) -> NormalizedConversation:
        if not isinstance(value, dict):
            raise _ConversationError("malformed_conversation")
        conversation_id = _conversation_id(value.get("id"), self._redactor)
        title = value.get("title", "")
        mapping = value.get("mapping")
        if not isinstance(title, str) or not isinstance(mapping, dict):
            raise _ConversationError("malformed_conversation")
        _conversation_utf8_size(title)
        if len(mapping) > self._max_nodes:
            raise _ConversationError("conversation_node_limit")
        nodes: dict[str, dict[str, Any]] = {}
        for node_id, node in mapping.items():
            if not isinstance(node_id, str) or not isinstance(node, dict):
                raise _ConversationError("malformed_conversation")
            _conversation_utf8_size(node_id)
            if node.get("id") != node_id:
                raise _ConversationError("malformed_conversation")
            children = node.get("children")
            parent = node.get("parent")
            if not isinstance(children, list) or not all(
                isinstance(child, str) for child in children
            ):
                raise _ConversationError("malformed_conversation")
            if parent is not None and not isinstance(parent, str):
                raise _ConversationError("malformed_conversation")
            for child in children:
                _conversation_utf8_size(child)
            if parent is not None:
                _conversation_utf8_size(parent)
            nodes[node_id] = node
        if not nodes:
            raise _ConversationError("malformed_conversation")
        leaves = [node_id for node_id, node in nodes.items() if not node["children"]]
        leaf = max(leaves or nodes.keys(), key=lambda item: _leaf_key(item, nodes[item]))
        branch: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: str | None = leaf
        while current is not None:
            if current in seen:
                raise _ConversationError("conversation_cycle")
            seen.add(current)
            node = nodes.get(current)
            if node is None:
                raise _ConversationError("conversation_orphan")
            branch.append(node)
            parent = node.get("parent")
            if parent is not None:
                parent_node = nodes.get(parent)
                if parent_node is None or current not in parent_node["children"]:
                    raise _ConversationError("conversation_orphan")
            current = parent
        branch.reverse()
        messages: list[VisibleMessage] = []
        transient_bytes = _conversation_utf8_size(title)
        for node in branch:
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            author = message.get("author")
            content = message.get("content")
            metadata = message.get("metadata", {})
            if not isinstance(author, dict) or not isinstance(content, dict):
                continue
            role = author.get("role")
            if not isinstance(role, str):
                raise _ConversationError("malformed_conversation")
            _conversation_utf8_size(role)
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and not _assistant_is_visible(message, metadata):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            text = "\n".join(part for part in parts if isinstance(part, str))
            if not text:
                continue
            redacted = self._redactor.redact(text).text
            transient_bytes += _conversation_utf8_size(redacted)
            if transient_bytes > self._max_conversation_bytes:
                raise _ConversationError("conversation_text_limit")
            model_slug = None
            if role == "assistant" and isinstance(metadata, dict):
                candidate_model = metadata.get("model_slug")
                model_slug = _model_id(
                    candidate_model if isinstance(candidate_model, str) else None,
                    self._redactor,
                )
            create_time = _safe_create_time(message.get("create_time"))
            messages.append(
                VisibleMessage(
                    role=role,
                    text=redacted,
                    model_slug=model_slug,
                    create_time=(create_time),
                    node_id=node["id"],
                )
            )
        if not messages:
            raise _ConversationError("malformed_conversation")
        return NormalizedConversation(
            conversation_id=conversation_id,
            title=_valid_conversation_text(self._redactor.redact(title).text),
            messages=tuple(messages),
        )


def _capture_project_binding(project_match: ProjectMatch) -> tuple[UUID, Path]:
    project_id = project_match.project_id
    project_path = project_match.project_path
    if (
        project_id is None
        or project_path is None
        or project_match.matched_project_ids != (project_id,)
    ):
        raise IngestionError("ChatGPT capture binding mismatch")
    return project_id, project_path


def _require_record_capture_binding(
    record: NormalizedTaskRecord,
    *,
    conversation_id: str,
    project_match: ProjectMatch,
) -> None:
    _project_id, project_path = _capture_project_binding(project_match)
    if record.namespace.source_agent != SourceAgent.CHATGPT:
        raise IngestionError("ChatGPT source namespace mismatch")
    if (
        record.cwd != project_path
        or record.source_record_id != conversation_id
        or record.verification.source_record_id != conversation_id
        or record.verification.namespace != record.namespace
        or record.verification.verified_by != "chatgpt_adapter"
    ):
        raise IngestionError("ChatGPT capture binding mismatch")


def _require_prepared_capture_binding(
    prepared: PreparedVerifiedCapture,
    *,
    record: NormalizedTaskRecord,
    payload: CapturePayload,
    conversation_id: str,
    project_match: ProjectMatch,
) -> None:
    project_id, project_path = _capture_project_binding(project_match)
    if (
        prepared.source_record_id != conversation_id
        or prepared.project.project_id != project_id
        or prepared.project.canonical_path != project_path
        or prepared.payload != payload
        or prepared.verification != record.verification
    ):
        raise IngestionError("ChatGPT capture binding mismatch")


def _capture_payload(record: NormalizedTaskRecord) -> CapturePayload:
    return CapturePayload(
        cwd=record.cwd,
        namespace=record.namespace,
        source_record_id=record.source_record_id,
        objective=record.objective,
        outcome=record.outcome,
        decisions=list(record.decisions),
        failed_attempts=list(record.failed_attempts),
        verified_commands=list(record.verified_commands),
        changed_paths=list(record.changed_paths),
        preferences=list(record.preferences),
        risks=list(record.risks),
        open_issues=list(record.open_issues),
        resolved_open_issues=list(record.resolved_open_issues),
        reusable_lessons=list(record.reusable_lessons),
    )


def _conversation_id(value: object, redactor: Redactor) -> str:
    try:
        return safe_persisted_identifier(value, "conversation_id", redactor)
    except ValueError:
        raise _ConversationError("malformed_conversation")


def _model_id(value: str | None, redactor: Redactor) -> str:
    if not isinstance(value, str) or value != value.strip():
        return "unknown"
    if not value or _SAFE_MODEL_SLUG.fullmatch(value) is None or ".." in value:
        return "unknown"
    redacted = redactor.redact(value)
    if redacted.findings or redacted.text != value:
        return "unknown"
    return value


def _assistant_is_visible(message: dict[str, Any], metadata: object) -> bool:
    if isinstance(metadata, dict) and metadata.get("is_visually_hidden_from_conversation"):
        return False
    recipients = [message.get("recipient")]
    if isinstance(metadata, dict):
        recipients.append(metadata.get("recipient"))
    for recipient in recipients:
        if recipient is None:
            continue
        if not isinstance(recipient, str) or recipient.strip().casefold() not in {
            "",
            "all",
        }:
            return False
    return True


def _leaf_key(node_id: str, node: dict[str, Any]) -> tuple[int, float, str]:
    message = node.get("message")
    create_time = (
        _safe_create_time(message.get("create_time")) if isinstance(message, dict) else None
    )
    if create_time is not None:
        return (1, create_time, node_id)
    return (0, 0.0, node_id)


def _safe_create_time(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _conversation_utf8_size(value: str) -> int:
    try:
        size = strict_utf8_size(value)
    except InvalidUtf8Text:
        raise _ConversationError("invalid_unicode") from None
    if contains_unsafe_text_control(value, allow_normal_text_whitespace=True):
        raise _ConversationError("unsafe_text_control")
    return size


def _valid_conversation_text(value: str) -> str:
    _conversation_utf8_size(value)
    return value


def _member_order(name: str) -> tuple[int, int]:
    match = _CONVERSATION_MEMBER.fullmatch(name)
    assert match is not None
    number = match.group(1)
    return (0, 0) if number is None else (1, int(number))


def _contains_name(text: str, name: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text) is not None


def _prefer_nested_absolute_path_matches(
    candidates: tuple[tuple[_ProjectRow, float, set[str]], ...],
) -> tuple[tuple[_ProjectRow, float, set[str]], ...]:
    """Discard an exact-path ancestor when a more specific registered path also matched."""
    return tuple(
        candidate
        for candidate in candidates
        if "absolute_path" not in candidate[2]
        or not any(
            "absolute_path" in other[2]
            and candidate[0].canonical_path != other[0].canonical_path
            and candidate[0].canonical_path in other[0].canonical_path.parents
            for other in candidates
        )
    )


def _is_same_or_nested_path(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _merge_candidate(
    candidates: dict[UUID, tuple[_ProjectRow, float, set[str]]],
    project: _ProjectRow,
    confidence: float,
    evidence: str,
) -> None:
    existing = candidates.get(project.project_id)
    if existing is None or confidence > existing[1]:
        candidates[project.project_id] = (project, confidence, {evidence})
    elif confidence == existing[1]:
        existing[2].add(evidence)


def _warnings(warnings: Counter[str]) -> tuple[str, ...]:
    return tuple(
        f"{category}:{warnings[category]}" for category in sorted(warnings) if warnings[category]
    )
