from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Final, TypeGuard
from uuid import UUID

from project_memory_hub.domain import (
    AdapterBatch,
    AdapterCheckpoint,
    CapturePayload,
    Namespace,
    NamespaceVerification,
    NormalizedTaskRecord,
    SourceAgent,
)
from project_memory_hub.security.capture_privacy import (
    CapturePrivacyCanonicalizer,
    MAX_CAPTURE_BYTES,
    MAX_FIELD_BYTES,
    MAX_LIST_ITEMS,
)
from project_memory_hub.security.identifiers import (
    safe_model_identifier,
    safe_persisted_identifier,
    safe_provenance_component,
)
from project_memory_hub.security.redaction import Redactor, normalize_redacted_text
from project_memory_hub.storage.deferred_records import (
    CodexDeferredLocator,
    DeferredRecoveryError,
)
from project_memory_hub.storage.path_identity import (
    persisted_identity_matches_at_same_path,
)
from project_memory_hub.utf8 import (
    InvalidUtf8Text,
    contains_unsafe_text_control,
    strict_utf8_size,
)


_PARSER_VERSION: Final = "codex-v3"
_PREFIX_HASH_CHUNK_BYTES: Final = 65_536
_CWD_MAX_CHARS: Final = 4096
_CWD_MAX_BYTES: Final = 16_384
_SUMMARY_MAX_CHARS: Final = 8192
_SUMMARY_MAX_BYTES: Final = 32_768
_CAPTURE_BLOCK_MAX_BYTES: Final = 131_072
_CAPTURE_VALUE_MAX_CHARS: Final = 8192
_CAPTURE_VALUE_MAX_BYTES: Final = 32_768
_DEFAULT_MAX_NONSEMANTIC_RECORD_BYTES: Final = 16_777_216
_NONSEMANTIC_RECORD_POLICY_VERSION: Final = 1
CAPTURE_START: Final = "<!-- project-memory-hub:capture:v1:start -->"
CAPTURE_END: Final = "<!-- project-memory-hub:capture:v1:end -->"
_IGNORED_RECORD_TYPES: Final = frozenset(
    {
        "response_item",
        "tool_stdout",
        "tool_stderr",
        "base_instructions",
        "world_state",
        "compacted_history",
        "compacted",
        "inter_agent_communication_metadata",
    }
)
_IGNORED_EVENT_TYPES: Final = frozenset(
    {
        "agent_message",
        "agent_reasoning",
        "context_compacted",
        "image_generation_end",
        "mcp_tool_call_end",
        "patch_apply_end",
        "sub_agent_activity",
        "task_started",
        "thread_settings_applied",
        "token_count",
        "user_message",
        "web_search_end",
    }
)
_LABEL = re.compile(
    r"^(Objective|Outcome|Verified|Failed|Decision|Changed|Preference|Risk|Open issue|Resolved issue|Lesson): (.+)$",
)
_LABEL_KEYS: Final = {
    "Objective": "objective",
    "Outcome": "outcome",
    "Verified": "verified",
    "Failed": "failed",
    "Decision": "decision",
    "Changed": "changed",
    "Preference": "preference",
    "Risk": "risk",
    "Open issue": "open issue",
    "Resolved issue": "resolved issue",
    "Lesson": "lesson",
}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class DiscoveryLimitExceeded(RuntimeError):
    """Session discovery exceeded its configured bounded work budget."""


class CodexContextUnavailable(RuntimeError):
    """The exact active Codex namespace could not be resolved safely."""


class _CaptureBlockMissing(ValueError):
    pass


class _InvalidCaptureBlock(ValueError):
    pass


class _FieldTooLarge(ValueError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


class _DiscoveryBudget:
    def __init__(self, max_entries: int, max_scopes: int) -> None:
        self.max_entries = max_entries
        self.max_scopes = max_scopes
        self.entries = 0
        self.scopes = 0

    def observe_entry(self) -> None:
        self.entries += 1
        if self.entries > self.max_entries:
            raise DiscoveryLimitExceeded("discovery_limit_exceeded")

    def observe_scope(self) -> None:
        self.scopes += 1
        if self.scopes > self.max_scopes:
            raise DiscoveryLimitExceeded("discovery_limit_exceeded")


@dataclass(frozen=True, slots=True)
class CodexReplayBatch:
    records: tuple[NormalizedTaskRecord, ...]
    source_hash: str


class CodexAdapter:
    source_agent = SourceAgent.CODEX

    def __init__(
        self,
        sessions_root: Path,
        redactor: Redactor,
        *,
        max_line_bytes: int = 262_144,
        max_record_bytes: int = 4_194_304,
        max_nonsemantic_record_bytes: int | None = None,
        max_records: int = 10_000,
        max_read_bytes: int = 33_554_432,
        max_depth: int = 12,
        max_context_bytes: int = 1_048_576,
        max_discovery_entries: int = 50_000,
        max_scopes: int = 10_000,
        max_runtime_scan_bytes: int = 1_073_741_824,
        max_runtime_scan_records: int = 1_000_000,
    ) -> None:
        limits = (
            max_line_bytes,
            max_record_bytes,
            max_records,
            max_read_bytes,
            max_depth,
            max_context_bytes,
            max_discovery_entries,
            max_scopes,
            max_runtime_scan_bytes,
            max_runtime_scan_records,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("adapter limits must be positive integers")
        if max_nonsemantic_record_bytes is None:
            max_nonsemantic_record_bytes = min(
                _DEFAULT_MAX_NONSEMANTIC_RECORD_BYTES,
                max_read_bytes - 1,
            )
        if type(max_nonsemantic_record_bytes) is not int or max_nonsemantic_record_bytes <= 0:
            raise ValueError("adapter limits must be positive integers")
        if (
            max_record_bytes < max_line_bytes
            or max_nonsemantic_record_bytes < max_record_bytes
            or max_nonsemantic_record_bytes >= max_read_bytes
            or max_runtime_scan_bytes < max_nonsemantic_record_bytes
        ):
            raise ValueError("adapter byte limits are inconsistent")
        self._sessions_root = Path(sessions_root)
        self._redactor = redactor
        self._max_line_bytes = max_line_bytes
        self._max_record_bytes = max_record_bytes
        self._max_nonsemantic_record_bytes = max_nonsemantic_record_bytes
        self._max_records = max_records
        self._max_read_bytes = max_read_bytes
        self._max_depth = max_depth
        self._max_context_bytes = max_context_bytes
        self._max_discovery_entries = max_discovery_entries
        self._max_scopes = max_scopes
        self._max_runtime_scan_bytes = max_runtime_scan_bytes
        self._max_runtime_scan_records = max_runtime_scan_records
        self._parser_policy_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "max_context_bytes": max_context_bytes,
                    "max_line_bytes": max_line_bytes,
                    "max_nonsemantic_record_bytes": max_nonsemantic_record_bytes,
                    "max_record_bytes": max_record_bytes,
                    "max_records": max_records,
                    "nonsemantic_record_policy": _NONSEMANTIC_RECORD_POLICY_VERSION,
                    "parser_version": _PARSER_VERSION,
                }
            ).encode("utf-8")
        ).hexdigest()

    def discover_scopes(self) -> tuple[str, ...]:
        root_fd = _open_root(self._sessions_root)
        try:
            scopes: list[str] = []
            budget = _DiscoveryBudget(
                self._max_discovery_entries,
                self._max_scopes,
            )
            self._discover_directory(root_fd, (), 0, scopes, budget)
            if not _root_matches(self._sessions_root, root_fd):
                raise PermissionError("sessions root changed")
            return tuple(sorted(scopes))
        finally:
            os.close(root_fd)

    def resolve_namespace(self, thread_id: str, cwd: Path) -> Namespace:
        """Resolve the active Codex model from bounded, provenance-checked metadata."""
        canonical_thread_id = _canonical_thread_id(thread_id)
        expected_cwd = _canonical_runtime_cwd(cwd)
        suffix = f"{canonical_thread_id}.jsonl"
        try:
            scopes = self.discover_scopes()
        except DiscoveryLimitExceeded:
            raise CodexContextUnavailable("codex_context_unavailable") from None
        matches = tuple(scope for scope in scopes if PurePosixPath(scope).name.endswith(suffix))
        if len(matches) != 1:
            raise CodexContextUnavailable("codex_context_unavailable")
        model_id = self._read_runtime_model(matches[0], canonical_thread_id, expected_cwd)
        return Namespace(source_agent=SourceAgent.CODEX, model_id=model_id)

    def _read_runtime_model(self, scope: str, thread_id: str, cwd: str) -> str:
        _, parts = _normalized_scope(scope)
        root_fd = _open_root(self._sessions_root)
        file_fd = -1
        reopened_fd = -1
        try:
            file_fd = _open_relative_file(root_fd, parts)
            metadata = os.fstat(file_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > self._max_runtime_scan_bytes
            ):
                raise CodexContextUnavailable("codex_context_unavailable")
            model_id, digest = _runtime_model_from_descriptor(
                file_fd,
                metadata.st_size,
                thread_id,
                cwd,
                redactor=self._redactor,
                max_line_bytes=self._max_line_bytes,
                max_record_bytes=self._max_record_bytes,
                max_nonsemantic_record_bytes=self._max_nonsemantic_record_bytes,
                max_records=self._max_runtime_scan_records,
            )
            if not _root_matches(self._sessions_root, root_fd):
                raise PermissionError("sessions root changed")
            after = os.fstat(file_fd)
            if not _same_identity(metadata, after):
                raise PermissionError("session scope changed")
            if after.st_size != metadata.st_size:
                raise CodexContextUnavailable("codex_context_unavailable")
            reopened_fd = _open_relative_file(root_fd, parts)
            reopened = os.fstat(reopened_fd)
            if not _same_identity(metadata, reopened):
                raise PermissionError("session scope changed")
            if reopened.st_size != metadata.st_size:
                raise CodexContextUnavailable("codex_context_unavailable")
            if _prefix_sha256(reopened_fd, metadata.st_size) != digest:
                raise PermissionError("session scope changed")
            return model_id
        except CodexContextUnavailable:
            raise
        except OSError:
            raise PermissionError("session scope changed") from None
        finally:
            if reopened_fd >= 0:
                os.close(reopened_fd)
            if file_fd >= 0:
                os.close(file_fd)
            os.close(root_fd)

    def _discover_directory(
        self,
        directory_fd: int,
        relative_parts: tuple[str, ...],
        depth: int,
        scopes: list[str],
        budget: _DiscoveryBudget,
    ) -> None:
        if depth > self._max_depth:
            return
        names: list[str] = []
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                budget.observe_entry()
                names.append(entry.name)
        for name in sorted(names):
            if not name or name in {".", "..", ".git"}:
                continue
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                continue
            parts = (*relative_parts, name)
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                except OSError:
                    continue
                try:
                    opened = os.fstat(child_fd)
                    if _same_identity(metadata, opened):
                        self._discover_directory(
                            child_fd,
                            parts,
                            depth + 1,
                            scopes,
                            budget,
                        )
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(metadata.st_mode) and name.endswith(".jsonl"):
                budget.observe_scope()
                scopes.append(PurePosixPath(*parts).as_posix())

    def read_incremental(
        self,
        scope: str,
        checkpoint: AdapterCheckpoint | None,
    ) -> AdapterBatch:
        normalized_scope, parts = _normalized_scope(scope)
        warnings: Counter[str] = Counter()
        root_fd = _open_root(self._sessions_root)
        file_fd = -1
        try:
            file_fd = _open_relative_file(root_fd, parts)
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("session scope is not a regular file")
            offset, session_id, session_meta_id, discarding_line, contexts = self._resume_state(
                normalized_scope,
                metadata,
                file_fd,
                checkpoint,
                warnings,
            )
            available = max(0, metadata.st_size - offset)
            data = os.pread(file_fd, min(available, self._max_read_bytes), offset)
            (
                records,
                next_offset,
                session_id,
                session_meta_id,
                discarding_line,
                contexts,
            ) = self._parse_chunk(
                data,
                offset,
                session_id,
                session_meta_id,
                discarding_line,
                contexts,
                warnings,
            )
            if not _root_matches(self._sessions_root, root_fd):
                raise PermissionError("sessions root changed")
            after_metadata = os.fstat(file_fd)
            if (
                not _same_identity(metadata, after_metadata)
                or after_metadata.st_size < metadata.st_size
                or next_offset > metadata.st_size
            ):
                raise PermissionError("session scope changed")
            expected_prior_prefix_sha256 = hashlib.sha256(b"").hexdigest()
            if offset:
                if checkpoint is None:
                    raise PermissionError("session scope changed")
                stored_prefix_sha256 = checkpoint.cursor.get("prefix_sha256")
                if not isinstance(stored_prefix_sha256, str):
                    raise PermissionError("session scope changed")
                expected_prior_prefix_sha256 = stored_prefix_sha256
            consumed_size = next_offset - offset
            try:
                if consumed_size == 0:
                    # _resume_state already verified this exact checkpoint prefix. The
                    # reopened descriptor below is the final binding check, so hashing
                    # the same potentially large EOF prefix twice here only amplifies
                    # reconcile I/O without strengthening the boundary.
                    prefix_sha256 = expected_prior_prefix_sha256
                else:
                    prefix_sha256 = _prefix_sha256(file_fd, next_offset)
                    if (
                        _prefix_sha256(file_fd, offset) != expected_prior_prefix_sha256
                        or os.pread(file_fd, consumed_size, offset) != data[:consumed_size]
                    ):
                        raise PermissionError("session scope changed")
            except OSError:
                raise PermissionError("session scope changed") from None
            reopened_file_fd = -1
            try:
                reopened_file_fd = _open_relative_file(root_fd, parts)
                reopened_metadata = os.fstat(reopened_file_fd)
                if (
                    not _same_identity(metadata, reopened_metadata)
                    or reopened_metadata.st_size < after_metadata.st_size
                    or _prefix_sha256(reopened_file_fd, next_offset) != prefix_sha256
                ):
                    raise PermissionError("session scope changed")
            except OSError:
                raise PermissionError("session scope changed") from None
            finally:
                if reopened_file_fd >= 0:
                    os.close(reopened_file_fd)
            next_checkpoint = AdapterCheckpoint(
                adapter=SourceAgent.CODEX,
                scope=normalized_scope,
                cursor={
                    "contexts_json": _canonical_json(contexts),
                    "device": int(metadata.st_dev),
                    "inode": int(metadata.st_ino),
                    "observed_size": int(metadata.st_size),
                    "offset": next_offset,
                    "relative_path": normalized_scope,
                    "session_id": session_id,
                    "session_meta_id": session_meta_id,
                    "discarding_oversized_line": int(discarding_line),
                    "nonsemantic_record_policy": _NONSEMANTIC_RECORD_POLICY_VERSION,
                    "parser_policy_sha256": self._parser_policy_sha256,
                    "prefix_length": next_offset,
                    "prefix_sha256": prefix_sha256,
                },
                parser_version=_PARSER_VERSION,
            )
            return AdapterBatch(
                records=tuple(records),
                next_checkpoint=next_checkpoint,
                warnings=_warnings(warnings),
            )
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(root_fd)

    def replay_deferred(
        self,
        locator: CodexDeferredLocator,
    ) -> NormalizedTaskRecord:
        """Replay one content-free locator without trusting a replacement cwd."""
        if type(locator) is not CodexDeferredLocator:
            raise TypeError("locator must be a CodexDeferredLocator")
        if locator.parser_version != _PARSER_VERSION:
            raise DeferredRecoveryError("rejected")
        if locator.parser_policy_sha256 != self._parser_policy_sha256:
            raise DeferredRecoveryError("rejected")
        try:
            normalized_scope, parts = _normalized_scope(locator.scope)
        except (TypeError, ValueError):
            raise DeferredRecoveryError("rejected") from None
        if normalized_scope != locator.scope:
            raise DeferredRecoveryError("rejected")
        if locator.prefix_length > self._max_runtime_scan_bytes:
            raise DeferredRecoveryError("replay_limit")

        try:
            root_fd = _open_root(self._sessions_root)
        except OSError:
            raise DeferredRecoveryError("source_unavailable") from None
        file_fd = -1
        reopened_fd = -1
        try:
            try:
                file_fd = _open_relative_file(root_fd, parts)
                metadata = os.fstat(file_fd)
            except OSError:
                raise DeferredRecoveryError("source_unavailable") from None
            persisted_identity = (locator.source_device, locator.source_inode)
            live_identity = (int(metadata.st_dev), int(metadata.st_ino))
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not persisted_identity_matches_at_same_path(
                    persisted_identity,
                    live_identity,
                )
                or metadata.st_size < locator.prefix_length
            ):
                raise DeferredRecoveryError("source_changed")
            if _prefix_sha256(file_fd, locator.prefix_length) != locator.prefix_sha256:
                raise DeferredRecoveryError("source_changed")

            offset = 0
            session_id = ""
            session_meta_id = ""
            discarding_line = False
            contexts: dict[str, dict[str, str]] = {}
            warnings: Counter[str] = Counter()
            matches: list[NormalizedTaskRecord] = []
            while offset < locator.prefix_length:
                available = locator.prefix_length - offset
                data = os.pread(file_fd, min(available, self._max_read_bytes), offset)
                if not data:
                    raise DeferredRecoveryError("source_changed")
                (
                    records,
                    next_offset,
                    session_id,
                    session_meta_id,
                    discarding_line,
                    contexts,
                ) = self._parse_chunk(
                    data,
                    offset,
                    session_id,
                    session_meta_id,
                    discarding_line,
                    contexts,
                    warnings,
                )
                matches.extend(
                    record
                    for record in records
                    if record.source_record_id == locator.source_record_id
                )
                if next_offset <= offset or next_offset > locator.prefix_length:
                    raise DeferredRecoveryError("source_changed")
                offset = next_offset
                if len(matches) > 1:
                    raise DeferredRecoveryError("ambiguous_source")

            if len(matches) != 1:
                raise DeferredRecoveryError("ambiguous_source")
            if not _root_matches(self._sessions_root, root_fd):
                raise DeferredRecoveryError("source_changed")
            after = os.fstat(file_fd)
            if (
                not _same_identity(metadata, after)
                or after.st_size != metadata.st_size
                or _prefix_sha256(file_fd, locator.prefix_length) != locator.prefix_sha256
            ):
                raise DeferredRecoveryError("source_changed")
            try:
                reopened_fd = _open_relative_file(root_fd, parts)
                reopened = os.fstat(reopened_fd)
            except OSError:
                raise DeferredRecoveryError("source_changed") from None
            if (
                not _same_identity(metadata, reopened)
                or reopened.st_size != metadata.st_size
                or _prefix_sha256(reopened_fd, locator.prefix_length) != locator.prefix_sha256
            ):
                raise DeferredRecoveryError("source_changed")
            return matches[0]
        finally:
            if reopened_fd >= 0:
                os.close(reopened_fd)
            if file_fd >= 0:
                os.close(file_fd)
            os.close(root_fd)

    def replay_records(
        self,
        scope: str,
        source_record_ids: tuple[str, ...],
    ) -> CodexReplayBatch:
        """Replay exact records from one stable, fully anchored source scope."""
        if not source_record_ids or len(source_record_ids) > 256:
            raise DeferredRecoveryError("replay_limit")
        try:
            prepared_ids = tuple(
                safe_persisted_identifier(value, "source_record_id", self._redactor)
                for value in source_record_ids
            )
        except ValueError:
            raise DeferredRecoveryError("rejected") from None
        if len(prepared_ids) != len(set(prepared_ids)):
            raise DeferredRecoveryError("ambiguous_source")
        try:
            normalized_scope, parts = _normalized_scope(scope)
        except (TypeError, ValueError):
            raise DeferredRecoveryError("rejected") from None
        try:
            root_fd = _open_root(self._sessions_root)
        except OSError:
            raise DeferredRecoveryError("source_unavailable") from None
        file_fd = -1
        reopened_fd = -1
        try:
            try:
                file_fd = _open_relative_file(root_fd, parts)
                metadata = os.fstat(file_fd)
            except OSError:
                raise DeferredRecoveryError("source_unavailable") from None
            if not stat.S_ISREG(metadata.st_mode):
                raise DeferredRecoveryError("source_changed")
            prefix_length = _complete_prefix_length(
                file_fd,
                int(metadata.st_size),
                self._max_nonsemantic_record_bytes,
            )
            if prefix_length <= 0 or prefix_length > self._max_runtime_scan_bytes:
                raise DeferredRecoveryError("replay_limit")
            prefix_sha256 = _prefix_sha256(file_fd, prefix_length)

            requested = frozenset(prepared_ids)
            found: dict[str, NormalizedTaskRecord] = {}
            offset = 0
            total_records = 0
            session_id = ""
            session_meta_id = ""
            discarding_line = False
            contexts: dict[str, dict[str, str]] = {}
            warnings: Counter[str] = Counter()
            while offset < prefix_length:
                available = prefix_length - offset
                data = os.pread(file_fd, min(available, self._max_read_bytes), offset)
                if not data:
                    raise DeferredRecoveryError("source_changed")
                (
                    records,
                    next_offset,
                    session_id,
                    session_meta_id,
                    discarding_line,
                    contexts,
                ) = self._parse_chunk(
                    data,
                    offset,
                    session_id,
                    session_meta_id,
                    discarding_line,
                    contexts,
                    warnings,
                )
                if next_offset <= offset or next_offset > prefix_length:
                    raise DeferredRecoveryError("source_changed")
                total_records += data[: next_offset - offset].count(b"\n")
                if total_records > self._max_runtime_scan_records:
                    raise DeferredRecoveryError("replay_limit")
                for record in records:
                    if record.source_record_id not in requested:
                        continue
                    if record.source_record_id in found:
                        raise DeferredRecoveryError("ambiguous_source")
                    found[record.source_record_id] = record
                offset = next_offset

            if set(found) != requested:
                raise DeferredRecoveryError("ambiguous_source")
            if not _root_matches(self._sessions_root, root_fd):
                raise DeferredRecoveryError("source_changed")
            after = os.fstat(file_fd)
            if (
                not _same_identity(metadata, after)
                or after.st_size != metadata.st_size
                or _prefix_sha256(file_fd, prefix_length) != prefix_sha256
            ):
                raise DeferredRecoveryError("source_changed")
            try:
                reopened_fd = _open_relative_file(root_fd, parts)
                reopened = os.fstat(reopened_fd)
            except OSError:
                raise DeferredRecoveryError("source_changed") from None
            if (
                not _same_identity(metadata, reopened)
                or reopened.st_size != metadata.st_size
                or _prefix_sha256(reopened_fd, prefix_length) != prefix_sha256
            ):
                raise DeferredRecoveryError("source_changed")
            anchor = {
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
                "nonsemantic_record_policy": _NONSEMANTIC_RECORD_POLICY_VERSION,
                "parser_policy_sha256": self._parser_policy_sha256,
                "parser_version": _PARSER_VERSION,
                "prefix_length": prefix_length,
                "prefix_sha256": prefix_sha256,
                "scope": normalized_scope,
            }
            source_hash = hashlib.sha256(
                json.dumps(anchor, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return CodexReplayBatch(
                records=tuple(found[source_id] for source_id in prepared_ids),
                source_hash=source_hash,
            )
        finally:
            if reopened_fd >= 0:
                os.close(reopened_fd)
            if file_fd >= 0:
                os.close(file_fd)
            os.close(root_fd)

    def _resume_state(
        self,
        scope: str,
        metadata: os.stat_result,
        file_fd: int,
        checkpoint: AdapterCheckpoint | None,
        warnings: Counter[str],
    ) -> tuple[int, str, str, bool, dict[str, dict[str, str]]]:
        if checkpoint is None:
            return 0, "", "", False, {}
        cursor = checkpoint.cursor
        offset = cursor.get("offset")
        observed_size = cursor.get("observed_size")
        prefix_length = cursor.get("prefix_length")
        prefix_sha256 = cursor.get("prefix_sha256")
        compatible = (
            checkpoint.adapter is SourceAgent.CODEX
            and checkpoint.scope == scope
            and checkpoint.parser_version == _PARSER_VERSION
            and cursor.get("relative_path") == scope
            and cursor.get("device") == int(metadata.st_dev)
            and cursor.get("inode") == int(metadata.st_ino)
            and type(offset) is int
            and type(observed_size) is int
            and type(prefix_length) is int
            and isinstance(prefix_sha256, str)
            and 0 <= offset <= observed_size <= metadata.st_size
            and prefix_length == offset
            and cursor.get("nonsemantic_record_policy") == _NONSEMANTIC_RECORD_POLICY_VERSION
            and cursor.get("parser_policy_sha256") == self._parser_policy_sha256
        )
        if not compatible:
            warnings["source_restarted"] += 1
            return 0, "", "", False, {}
        assert type(offset) is int
        assert type(prefix_length) is int
        assert isinstance(prefix_sha256, str)
        if _prefix_sha256(file_fd, prefix_length) != prefix_sha256:
            warnings["source_restarted"] += 1
            return 0, "", "", False, {}
        try:
            contexts = json.loads(str(cursor.get("contexts_json", "{}")))
            if (
                not _valid_contexts(contexts)
                or strict_utf8_size(_canonical_json(contexts)) > self._max_context_bytes
            ):
                raise ValueError
        except (json.JSONDecodeError, ValueError, TypeError):
            warnings["source_restarted"] += 1
            return 0, "", "", False, {}
        session_id = cursor.get("session_id", "")
        session_meta_id = cursor.get("session_meta_id", "")
        discarding_value = cursor.get("discarding_oversized_line", 0)
        if (
            not isinstance(session_id, str)
            or not isinstance(session_meta_id, str)
            or type(discarding_value) is not int
            or discarding_value not in {0, 1}
        ):
            warnings["source_restarted"] += 1
            return 0, "", "", False, {}
        if not self._valid_checkpoint_lifecycle(session_id, session_meta_id, contexts):
            warnings["source_restarted"] += 1
            return 0, "", "", False, {}
        discarding_line = bool(discarding_value)
        if discarding_line and (session_id or session_meta_id or contexts):
            warnings["source_restarted"] += 1
            return 0, "", "", False, {}
        return offset, session_id, session_meta_id, discarding_line, contexts

    def _valid_checkpoint_lifecycle(
        self,
        session_id: str,
        session_meta_id: str,
        contexts: dict[str, dict[str, str]],
    ) -> bool:
        if not session_id:
            return not session_meta_id and not contexts
        try:
            safe_provenance_component(session_id, "session_id", self._redactor)
            safe_provenance_component(session_meta_id, "session_meta_id", self._redactor)
            for turn_id, context in contexts.items():
                safe_provenance_component(turn_id, "turn_id", self._redactor)
                if context["session_id"] != session_id:
                    return False
                safe_provenance_component(
                    context["session_id"],
                    "session_id",
                    self._redactor,
                )
                if context["blocked"] == "1":
                    if context["cwd"] or context["model"]:
                        return False
                    continue
                if _bounded_runtime_cwd(context["cwd"]) is None:
                    return False
                safe_model_identifier(context["model"], self._redactor)
        except (_FieldTooLarge, ValueError):
            return False
        return True

    def _parse_chunk(
        self,
        data: bytes,
        initial_offset: int,
        session_id: str,
        session_meta_id: str,
        discarding_line: bool,
        contexts: dict[str, dict[str, str]],
        warnings: Counter[str],
    ) -> tuple[
        list[NormalizedTaskRecord],
        int,
        str,
        str,
        bool,
        dict[str, dict[str, str]],
    ]:
        records: list[NormalizedTaskRecord] = []
        offset = initial_offset
        processed = 0
        cursor = 0
        if discarding_line:
            newline = data.find(b"\n")
            if newline < 0:
                return (
                    records,
                    offset + len(data),
                    session_id,
                    session_meta_id,
                    True,
                    contexts,
                )
            cursor = newline + 1
            offset += cursor
            discarding_line = False
        while cursor < len(data) and processed < self._max_records:
            newline = data.find(b"\n", cursor)
            if newline < 0:
                fragment_bytes = len(data) - cursor
                if fragment_bytes >= self._max_nonsemantic_record_bytes:
                    warnings["oversized_line"] += 1
                    session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                    offset += fragment_bytes
                    discarding_line = True
                break
            line_end = newline + 1
            raw_line = data[cursor:line_end]
            cursor = line_end
            processed += 1
            offset += len(raw_line)
            if len(raw_line) > self._max_nonsemantic_record_bytes:
                warnings["oversized_line"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                continue
            try:
                value = _load_record_json(
                    raw_line,
                    reject_duplicate_keys=len(raw_line) > self._max_record_bytes,
                )
            except (ValueError, UnicodeDecodeError, RecursionError):
                warnings["malformed_json"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                continue
            if not isinstance(value, dict):
                warnings["malformed_json"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                continue
            if len(raw_line) > self._max_record_bytes:
                warnings["oversized_line"] += 1
                if _is_known_nonsemantic_record(value):
                    continue
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                continue
            if len(raw_line) > self._max_line_bytes:
                warnings["oversized_line"] += 1
                if _is_known_nonsemantic_record(value):
                    continue
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                continue
            try:
                session_id, session_meta_id, produced = self._process_record(
                    value,
                    session_id,
                    session_meta_id,
                    contexts,
                    warnings,
                )
            except InvalidUtf8Text:
                warnings["invalid_unicode"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                continue
            if produced is not None:
                records.append(produced)
        return records, offset, session_id, session_meta_id, discarding_line, contexts

    def _process_record(
        self,
        value: dict[str, Any],
        session_id: str,
        session_meta_id: str,
        contexts: dict[str, dict[str, str]],
        warnings: Counter[str],
    ) -> tuple[str, str, NormalizedTaskRecord | None]:
        record_type = value.get("type")
        payload = value.get("payload")
        if not isinstance(record_type, str):
            warnings["malformed_record"] += 1
            session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
            return session_id, session_meta_id, None
        if record_type in _IGNORED_RECORD_TYPES:
            return session_id, session_meta_id, None
        if not isinstance(payload, dict):
            warnings["malformed_record"] += 1
            session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
            return session_id, session_meta_id, None
        if record_type == "session_meta":
            candidate = payload.get("session_id") if "session_id" in payload else payload.get("id")
            try:
                prepared_session_id = safe_provenance_component(
                    candidate,
                    "session_id",
                    self._redactor,
                )
                metadata_id = (
                    safe_provenance_component(
                        payload.get("id"),
                        "session_meta_id",
                        self._redactor,
                    )
                    if "id" in payload
                    else None
                )
            except ValueError:
                warnings["unsafe_identifier"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                return session_id, session_meta_id, None
            if prepared_session_id == session_id:
                if metadata_id not in {None, session_meta_id, session_id}:
                    warnings["ambiguous_session"] += 1
                    session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                return session_id, session_meta_id, None
            contexts.clear()
            session_id = prepared_session_id
            session_meta_id = metadata_id or prepared_session_id
            return session_id, session_meta_id, None
        if record_type == "turn_context":
            if not session_id:
                warnings["malformed_context"] += 1
                return session_id, session_meta_id, None
            try:
                turn_id = safe_provenance_component(
                    payload.get("turn_id"),
                    "turn_id",
                    self._redactor,
                )
            except ValueError:
                warnings["unsafe_identifier"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                return session_id, session_meta_id, None
            try:
                cwd = _bounded_runtime_cwd(payload.get("cwd"))
                summary = _bounded_text(
                    payload.get("summary", ""),
                    _SUMMARY_MAX_CHARS,
                    _SUMMARY_MAX_BYTES,
                    allow_empty=True,
                )
            except _FieldTooLarge:
                warnings["field_too_large"] += 1
                _block_turn(
                    contexts,
                    turn_id,
                    session_id,
                    self._max_context_bytes,
                    warnings,
                )
                return session_id, session_meta_id, None
            if cwd is None or summary is None:
                warnings["malformed_context"] += 1
                _block_turn(
                    contexts,
                    turn_id,
                    session_id,
                    self._max_context_bytes,
                    warnings,
                )
                return session_id, session_meta_id, None
            try:
                model = safe_model_identifier(payload.get("model"), self._redactor)
            except ValueError:
                warnings["unsafe_identifier"] += 1
                _block_turn(
                    contexts,
                    turn_id,
                    session_id,
                    self._max_context_bytes,
                    warnings,
                )
                return session_id, session_meta_id, None
            candidate_context = {
                "blocked": "0",
                "cwd": cwd,
                "model": model,
                "session_id": session_id,
            }
            if turn_id in contexts:
                existing = contexts[turn_id]
                if (
                    existing["blocked"] == "0"
                    and existing["cwd"] == candidate_context["cwd"]
                    and existing["model"] == candidate_context["model"]
                    and existing["session_id"] == candidate_context["session_id"]
                ):
                    return session_id, session_meta_id, None
                _block_turn(
                    contexts,
                    turn_id,
                    session_id,
                    self._max_context_bytes,
                    warnings,
                )
                warnings["ambiguous_turn"] += 1
                return session_id, session_meta_id, None
            if len(contexts) >= self._max_records:
                contexts.pop(next(iter(contexts)))
            tentative_contexts = {**contexts, turn_id: candidate_context}
            if strict_utf8_size(_canonical_json(tentative_contexts)) > self._max_context_bytes:
                warnings["context_limit_exceeded"] += 1
                session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
                return session_id, session_meta_id, None
            contexts[turn_id] = candidate_context
            return session_id, session_meta_id, None
        if record_type != "event_msg":
            warnings["unknown_record"] += 1
            session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
            return session_id, session_meta_id, None

        event_type = payload.get("type")
        if not isinstance(event_type, str):
            warnings["malformed_record"] += 1
            session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
            return session_id, session_meta_id, None
        if event_type in _IGNORED_EVENT_TYPES:
            return session_id, session_meta_id, None
        if event_type not in {"turn_aborted", "task_complete"}:
            warnings["unknown_event"] += 1
            session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
            return session_id, session_meta_id, None
        try:
            turn_id = safe_provenance_component(
                payload.get("turn_id"),
                "turn_id",
                self._redactor,
            )
        except ValueError:
            warnings["unsafe_identifier"] += 1
            session_id, session_meta_id = _invalidate_lifecycle(contexts, warnings)
            return session_id, session_meta_id, None
        context = contexts.pop(turn_id, None)
        if event_type == "turn_aborted":
            return session_id, session_meta_id, None
        if context is None:
            warnings["orphan_completion"] += 1
            return session_id, session_meta_id, None
        if context["blocked"] == "1":
            warnings["ambiguous_completion"] += 1
            return session_id, session_meta_id, None
        message = payload.get("last_agent_message")
        if not isinstance(message, str) or not message.strip():
            warnings["incomplete_completion"] += 1
            return session_id, session_meta_id, None
        verified_at = _timestamp(value.get("timestamp"))
        if verified_at is None:
            warnings["invalid_timestamp"] += 1
            return session_id, session_meta_id, None
        record_session_id = context["session_id"]
        if not record_session_id:
            warnings["orphan_completion"] += 1
            return session_id, session_meta_id, None
        try:
            parsed = _parse_labels(message, self._redactor)
        except _CaptureBlockMissing:
            warnings["no_capture_block"] += 1
            return session_id, session_meta_id, None
        except _InvalidCaptureBlock:
            warnings["invalid_capture_block"] += 1
            return session_id, session_meta_id, None
        namespace = Namespace(source_agent=SourceAgent.CODEX, model_id=context["model"])
        try:
            source_record_id = safe_persisted_identifier(
                f"{record_session_id}:{turn_id}",
                "source_record_id",
                self._redactor,
            )
        except ValueError:
            warnings["unsafe_identifier"] += 1
            return session_id, session_meta_id, None
        return (
            session_id,
            session_meta_id,
            NormalizedTaskRecord(
                cwd=Path(context["cwd"]),
                namespace=namespace,
                source_record_id=source_record_id,
                objective=parsed["objective"][0],
                outcome=parsed["outcome"][0],
                decisions=tuple(parsed["decision"]),
                failed_attempts=tuple(parsed["failed"]),
                verified_commands=tuple(parsed["verified"]),
                changed_paths=tuple(parsed["changed"]),
                preferences=tuple(parsed["preference"]),
                risks=tuple(parsed["risk"]),
                open_issues=tuple(parsed["open issue"]),
                resolved_open_issues=tuple(parsed["resolved issue"]),
                reusable_lessons=tuple(parsed["lesson"]),
                verification=NamespaceVerification(
                    namespace=namespace,
                    source_record_id=source_record_id,
                    verified_by="codex_adapter",
                    verified_at=verified_at,
                ),
            ),
        )


def _parse_labels(text: str, redactor: Redactor) -> dict[str, list[str]]:
    lines = text.splitlines()
    markers = _capture_markers(lines)
    completed: list[tuple[int, int, bool]] = []
    opened: int | None = None
    nested = False
    for line_index, marker in markers:
        if marker == CAPTURE_START:
            if opened is not None:
                nested = True
            opened = line_index
            continue
        if opened is not None:
            completed.append((opened, line_index, nested))
            opened = None
            nested = False
    if not completed:
        if any(marker == CAPTURE_START for _, marker in markers):
            raise _InvalidCaptureBlock
        raise _CaptureBlockMissing
    start, end, invalid = completed[-1]
    if (
        invalid
        or any(line_index > end for line_index, _ in markers)
        or not _valid_capture_suffix(lines[end + 1 :])
    ):
        raise _InvalidCaptureBlock
    block_lines = lines[start + 1 : end]
    if _capture_utf8_size("\n".join(block_lines)) > _CAPTURE_BLOCK_MAX_BYTES:
        raise _InvalidCaptureBlock
    parsed: dict[str, list[str]] = {
        "objective": [],
        "outcome": [],
        "verified": [],
        "failed": [],
        "decision": [],
        "changed": [],
        "preference": [],
        "risk": [],
        "open issue": [],
        "resolved issue": [],
        "lesson": [],
    }
    for line in block_lines:
        if not line:
            continue
        match = _LABEL.fullmatch(line)
        if match is None:
            raise _InvalidCaptureBlock
        content = match.group(2).strip()
        if (
            not content
            or len(content) > _CAPTURE_VALUE_MAX_CHARS
            or _capture_utf8_size(content) > _CAPTURE_VALUE_MAX_BYTES
        ):
            raise _InvalidCaptureBlock
        key = _LABEL_KEYS[match.group(1)]
        if key not in {"objective", "outcome"} and len(parsed[key]) >= MAX_LIST_ITEMS:
            raise _InvalidCaptureBlock
        normalized = (
            " ".join(content.split())
            if key == "changed"
            else normalize_redacted_text(redactor, content)
        )
        if not normalized or _capture_utf8_size(normalized) > MAX_FIELD_BYTES:
            raise _InvalidCaptureBlock
        parsed[key].append(normalized)
    parsed["resolved issue"] = list(dict.fromkeys(parsed["resolved issue"]))
    if set(parsed["open issue"]).intersection(parsed["resolved issue"]):
        raise _InvalidCaptureBlock
    if len(parsed["objective"]) != 1 or len(parsed["outcome"]) != 1:
        raise _InvalidCaptureBlock
    if (
        sum(_capture_utf8_size(value) for values in parsed.values() for value in values)
        > MAX_CAPTURE_BYTES
    ):
        raise _InvalidCaptureBlock
    try:
        CapturePrivacyCanonicalizer(redactor).portable_structure(
            CapturePayload(
                cwd=Path("."),
                namespace=Namespace(
                    source_agent=SourceAgent.CODEX,
                    model_id="adapter-validation",
                ),
                source_record_id="adapter-validation",
                objective=parsed["objective"][0],
                outcome=parsed["outcome"][0],
                decisions=parsed["decision"],
                failed_attempts=parsed["failed"],
                verified_commands=parsed["verified"],
                changed_paths=parsed["changed"],
                preferences=parsed["preference"],
                risks=parsed["risk"],
                open_issues=parsed["open issue"],
                resolved_open_issues=parsed["resolved issue"],
                reusable_lessons=parsed["lesson"],
            )
        )
    except ValueError:
        raise _InvalidCaptureBlock from None
    return parsed


def _invalidate_lifecycle(
    contexts: dict[str, dict[str, str]],
    warnings: Counter[str],
) -> tuple[str, str]:
    contexts.clear()
    warnings["unsafe_lifecycle"] += 1
    return "", ""


def _capture_utf8_size(value: str) -> int:
    try:
        return strict_utf8_size(value)
    except InvalidUtf8Text:
        raise _InvalidCaptureBlock from None


def _is_known_nonsemantic_record(value: dict[str, Any]) -> bool:
    record_type = value.get("type")
    if not isinstance(record_type, str):
        return False
    if record_type in _IGNORED_RECORD_TYPES:
        return True
    payload = value.get("payload")
    return (
        record_type == "event_msg"
        and isinstance(payload, dict)
        and isinstance(payload.get("type"), str)
        and payload.get("type") in _IGNORED_EVENT_TYPES
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey("duplicate JSON key")
        value[key] = item
    return value


def _load_record_json(raw_line: bytes, *, reject_duplicate_keys: bool) -> Any:
    if reject_duplicate_keys:
        return json.loads(raw_line, object_pairs_hook=_reject_duplicate_json_keys)
    return json.loads(raw_line)


def _block_turn(
    contexts: dict[str, dict[str, str]],
    turn_id: str,
    session_id: str,
    max_context_bytes: int,
    warnings: Counter[str],
) -> None:
    contexts[turn_id] = {
        "blocked": "1",
        "cwd": "",
        "model": "",
        "session_id": session_id,
    }
    if strict_utf8_size(_canonical_json(contexts)) > max_context_bytes:
        contexts.clear()
        warnings["context_limit_exceeded"] += 1


def _capture_markers(lines: list[str]) -> list[tuple[int, str]]:
    markers: list[tuple[int, str]] = []
    fence_character = ""
    fence_length = 0
    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        indentation = len(line) - len(stripped)
        if fence_character:
            if indentation <= 3 and _closes_fence(stripped, fence_character, fence_length):
                fence_character = ""
                fence_length = 0
            continue
        opening = _opens_fence(stripped) if indentation <= 3 else None
        if opening is not None:
            fence_character, fence_length = opening
            continue
        if line in {CAPTURE_START, CAPTURE_END}:
            markers.append((index, line))
    return markers


def _opens_fence(value: str) -> tuple[str, int] | None:
    if not value or value[0] not in {"`", "~"}:
        return None
    character = value[0]
    length = len(value) - len(value.lstrip(character))
    if length < 3:
        return None
    if character == "`" and "`" in value[length:]:
        return None
    return character, length


def _closes_fence(value: str, character: str, minimum: int) -> bool:
    length = len(value) - len(value.lstrip(character))
    return length >= minimum and not value[length:].strip()


def _valid_capture_suffix(lines: list[str]) -> bool:
    if len(lines) > 512:
        return False
    lower = 0
    upper = len(lines)
    while lower < upper and not lines[lower]:
        lower += 1
    while upper > lower and not lines[upper - 1]:
        upper -= 1
    suffix = lines[lower:upper]
    if not suffix:
        return True
    if len(suffix) < 6 or suffix[0] != "<oai-mem-citation>":
        return False
    if suffix[1] != "<citation_entries>" or suffix[-1] != "</oai-mem-citation>":
        return False
    try:
        citation_end = suffix.index("</citation_entries>", 2)
    except ValueError:
        return False
    if citation_end + 3 > len(suffix):
        return False
    if suffix[citation_end + 1] != "<rollout_ids>" or suffix[-2] != "</rollout_ids>":
        return False
    citation_lines = suffix[2:citation_end]
    rollout_lines = suffix[citation_end + 2 : -2]
    if len(citation_lines) > 100 or len(rollout_lines) > 100:
        return False
    if any(
        not line
        or not _valid_utf8_at_most(line, 4096)
        or any(ord(character) < 32 or ord(character) == 127 for character in line)
        for line in citation_lines
    ):
        return False
    for value in rollout_lines:
        try:
            if value != str(UUID(value)).lower():
                return False
        except (AttributeError, TypeError, ValueError):
            return False
    return True


def _canonical_thread_id(value: str) -> str:
    if not isinstance(value, str):
        raise CodexContextUnavailable("codex_context_unavailable")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise CodexContextUnavailable("codex_context_unavailable") from None
    canonical = str(parsed).lower()
    if value != canonical:
        raise CodexContextUnavailable("codex_context_unavailable")
    return canonical


def _canonical_runtime_cwd(value: Path) -> str:
    if not isinstance(value, Path):
        raise CodexContextUnavailable("codex_context_unavailable")
    text = str(value)
    try:
        invalid = (
            not value.is_absolute()
            or os.path.normpath(text) != text
            or len(text) > _CWD_MAX_CHARS
            or strict_utf8_size(text) > _CWD_MAX_BYTES
            or _contains_unsafe_runtime_character(text)
        )
    except InvalidUtf8Text:
        raise CodexContextUnavailable("codex_context_unavailable") from None
    if invalid:
        raise CodexContextUnavailable("codex_context_unavailable")
    return text


def _runtime_model_from_descriptor(
    descriptor: int,
    size: int,
    thread_id: str,
    cwd: str,
    *,
    redactor: Redactor,
    max_line_bytes: int,
    max_record_bytes: int,
    max_nonsemantic_record_bytes: int,
    max_records: int,
) -> tuple[str, str]:
    digest = hashlib.sha256()

    def lines() -> Iterable[bytes]:
        buffer = bytearray()
        read_offset = 0
        while read_offset < size:
            expected = min(1_048_576, size - read_offset)
            chunk = os.pread(descriptor, expected, read_offset)
            if len(chunk) != expected:
                raise CodexContextUnavailable("codex_context_unavailable")
            digest.update(chunk)
            read_offset += len(chunk)
            buffer.extend(chunk)
            cursor = 0
            while True:
                newline = buffer.find(b"\n", cursor)
                if newline < 0:
                    break
                line_end = newline + 1
                if line_end - cursor > max_nonsemantic_record_bytes:
                    raise CodexContextUnavailable("codex_context_unavailable")
                yield bytes(buffer[cursor:line_end])
                cursor = line_end
            if cursor:
                del buffer[:cursor]
            if len(buffer) > max_nonsemantic_record_bytes:
                raise CodexContextUnavailable("codex_context_unavailable")
        if buffer:
            raise CodexContextUnavailable("codex_context_unavailable")

    model_id = _runtime_model_from_lines(
        lines(),
        thread_id,
        cwd,
        redactor=redactor,
        max_line_bytes=max_line_bytes,
        max_record_bytes=max_record_bytes,
        max_nonsemantic_record_bytes=max_nonsemantic_record_bytes,
        max_records=max_records,
    )
    return model_id, digest.hexdigest()


def _runtime_model_from_lines(
    lines: Iterable[bytes],
    thread_id: str,
    cwd: str,
    *,
    redactor: Redactor,
    max_line_bytes: int,
    max_record_bytes: int,
    max_nonsemantic_record_bytes: int,
    max_records: int,
) -> str:
    saw_target_session = False
    session_alias: str | None = None
    contexts: dict[str, tuple[str, str]] = {}
    ambiguous_turns: set[str] = set()
    processed = 0
    for raw_line in lines:
        processed += 1
        if processed > max_records:
            raise CodexContextUnavailable("codex_context_unavailable")
        line_bytes = len(raw_line)
        if not raw_line.endswith(b"\n") or line_bytes > max_nonsemantic_record_bytes:
            raise CodexContextUnavailable("codex_context_unavailable")
        try:
            value = _load_record_json(
                raw_line,
                reject_duplicate_keys=line_bytes > max_record_bytes,
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise CodexContextUnavailable("codex_context_unavailable") from None
        if not isinstance(value, dict):
            raise CodexContextUnavailable("codex_context_unavailable")
        if line_bytes > max_record_bytes:
            if _is_known_nonsemantic_record(value):
                continue
            raise CodexContextUnavailable("codex_context_unavailable")
        if line_bytes > max_line_bytes:
            if _is_known_nonsemantic_record(value):
                continue
            raise CodexContextUnavailable("codex_context_unavailable")
        record_type = value.get("type")
        if not isinstance(record_type, str):
            raise CodexContextUnavailable("codex_context_unavailable")
        if record_type in _IGNORED_RECORD_TYPES:
            continue
        payload = value.get("payload")
        if record_type not in {"session_meta", "turn_context", "event_msg"}:
            raise CodexContextUnavailable("codex_context_unavailable")
        if not isinstance(payload, dict):
            raise CodexContextUnavailable("codex_context_unavailable")
        if record_type == "session_meta":
            identifiers: dict[str, str | None] = {"id": None, "session_id": None}
            for key in identifiers:
                if key not in payload:
                    continue
                try:
                    identifier = safe_provenance_component(
                        payload.get(key),
                        key,
                        redactor,
                    )
                except ValueError:
                    raise CodexContextUnavailable("codex_context_unavailable")
                identifiers[key] = identifier
            metadata_id = identifiers["id"]
            metadata_session_id = identifiers["session_id"]
            if session_alias is None:
                if metadata_id != thread_id:
                    raise CodexContextUnavailable("codex_context_unavailable")
                session_alias = metadata_session_id or metadata_id
                saw_target_session = True
                contexts.clear()
                ambiguous_turns.clear()
            else:
                if metadata_id is None and metadata_session_id is None:
                    raise CodexContextUnavailable("codex_context_unavailable")
                if metadata_id not in {None, thread_id, session_alias}:
                    raise CodexContextUnavailable("codex_context_unavailable")
                if metadata_session_id not in {None, session_alias}:
                    raise CodexContextUnavailable("codex_context_unavailable")
            continue
        if not saw_target_session:
            continue
        if record_type == "turn_context":
            try:
                turn_id = safe_provenance_component(
                    payload.get("turn_id"),
                    "turn_id",
                    redactor,
                )
                context_cwd = _bounded_runtime_cwd(
                    payload.get("cwd"),
                )
                model = safe_model_identifier(payload.get("model"), redactor)
            except (ValueError, _FieldTooLarge):
                raise CodexContextUnavailable("codex_context_unavailable") from None
            if context_cwd is None:
                raise CodexContextUnavailable("codex_context_unavailable")
            candidate = (context_cwd, model)
            if turn_id in ambiguous_turns:
                continue
            if turn_id not in contexts:
                contexts[turn_id] = candidate
            elif contexts[turn_id] != candidate:
                contexts.pop(turn_id, None)
                ambiguous_turns.add(turn_id)
            continue
        assert record_type == "event_msg"
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            raise CodexContextUnavailable("codex_context_unavailable")
        if event_type in _IGNORED_EVENT_TYPES:
            continue
        if event_type not in {"task_complete", "turn_aborted"}:
            raise CodexContextUnavailable("codex_context_unavailable")
        try:
            turn_id = safe_provenance_component(
                payload.get("turn_id"),
                "turn_id",
                redactor,
            )
        except ValueError:
            raise CodexContextUnavailable("codex_context_unavailable") from None
        contexts.pop(turn_id, None)
        ambiguous_turns.discard(turn_id)
    matches = {context[1] for context in contexts.values() if context[0] == cwd}
    if not saw_target_session or ambiguous_turns or len(matches) != 1:
        raise CodexContextUnavailable("codex_context_unavailable")
    return next(iter(matches))


def _open_root(root: Path) -> int:
    try:
        descriptor = _open_directory_components(root)
    except OSError:
        raise PermissionError("sessions root is not an anchored directory") from None
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PermissionError("sessions root must be a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_components(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_file(root_fd: int, parts: tuple[str, ...]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=current_fd)
        os.close(current_fd)
        return descriptor
    except BaseException:
        os.close(current_fd)
        raise


def _normalized_scope(scope: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(scope, str) or not scope or "\\" in scope:
        raise PermissionError("invalid session scope")
    path = PurePosixPath(scope)
    parts = path.parts
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise PermissionError("invalid session scope")
    if not parts or not parts[-1].endswith(".jsonl") or ".git" in parts:
        raise PermissionError("invalid session scope")
    normalized = path.as_posix()
    if normalized != scope:
        raise PermissionError("invalid session scope")
    return normalized, parts


def _root_matches(root: Path, root_fd: int) -> bool:
    reopened_fd = -1
    try:
        reopened_fd = _open_directory_components(root)
        return _same_identity(os.fstat(root_fd), os.fstat(reopened_fd))
    except OSError:
        return False
    finally:
        if reopened_fd >= 0:
            os.close(reopened_fd)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(first.st_mode) == stat.S_ISDIR(second.st_mode)
        and stat.S_ISREG(first.st_mode) == stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _valid_contexts(value: object) -> TypeGuard[dict[str, dict[str, str]]]:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(turn_id, str)
        and isinstance(context, dict)
        and set(context) == {"blocked", "cwd", "model", "session_id"}
        and context["blocked"] in {"0", "1"}
        and all(isinstance(item, str) for item in context.values())
        for turn_id, context in value.items()
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _bounded_text(
    value: object,
    max_chars: int,
    max_bytes: int,
    *,
    allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped and not allow_empty:
        return None
    if len(stripped) > max_chars or strict_utf8_size(stripped) > max_bytes:
        raise _FieldTooLarge
    return stripped


def _bounded_exact_text(
    value: object,
    max_chars: int,
    max_bytes: int,
) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if len(value) > max_chars or strict_utf8_size(value) > max_bytes:
        raise _FieldTooLarge
    return value


def _bounded_runtime_cwd(value: object) -> str | None:
    text = _bounded_exact_text(value, _CWD_MAX_CHARS, _CWD_MAX_BYTES)
    if text is None:
        return None
    path = Path(text)
    if (
        not path.is_absolute()
        or os.path.normpath(text) != text
        or _contains_unsafe_runtime_character(text)
    ):
        return None
    return text


def _contains_unsafe_runtime_character(value: str) -> bool:
    return contains_unsafe_text_control(value)


def _valid_utf8_at_most(value: str, max_bytes: int) -> bool:
    try:
        return strict_utf8_size(value) <= max_bytes
    except InvalidUtf8Text:
        return False


def _prefix_sha256(descriptor: int, length: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < length:
        expected = min(_PREFIX_HASH_CHUNK_BYTES, length - offset)
        chunk = os.pread(descriptor, expected, offset)
        if len(chunk) != expected:
            raise PermissionError("session prefix changed")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _complete_prefix_length(descriptor: int, size: int, max_record_bytes: int) -> int:
    if size <= 0:
        return 0
    tail_length = min(size, max_record_bytes)
    tail = os.pread(descriptor, tail_length, size - tail_length)
    if not tail:
        return 0
    newline = tail.rfind(b"\n")
    if newline < 0:
        return 0
    return size - tail_length + newline + 1


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _warnings(warnings: Counter[str]) -> tuple[str, ...]:
    return tuple(
        f"{category}:{warnings[category]}" for category in sorted(warnings) if warnings[category]
    )
