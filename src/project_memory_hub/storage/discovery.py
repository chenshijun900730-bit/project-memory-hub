from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from project_memory_hub.domain import DiscoveryResult
from project_memory_hub.storage.database import Database


_ISSUE_CODES = frozenset({"blocked_permission", "missing_root", "scan_error"})
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FINDINGS = 20_000
_MAX_PATH_CHARS = 4096
_MAX_REMEDIATION_CHARS = 1000


@dataclass(frozen=True, slots=True)
class DiscoveryIssueFinding:
    path: Path
    code: str
    affected_capability: str
    remediation: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class DuplicateCandidateFinding:
    fingerprint_kind: str
    fingerprint: str
    candidate_paths: tuple[Path, ...]
    observed_at: str


@dataclass(frozen=True, slots=True)
class DiscoveryFindingSnapshot:
    issues: tuple[DiscoveryIssueFinding, ...]
    duplicates: tuple[DuplicateCandidateFinding, ...]


class DiscoveryFindingRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def sync(self, result: DiscoveryResult) -> None:
        if not isinstance(result, DiscoveryResult):
            raise TypeError("discovery result is required")
        if len(result.issues) > _MAX_FINDINGS or len(result.candidates) > _MAX_FINDINGS:
            raise ValueError("discovery snapshot exceeds limit")
        observed_at = _utc_now()
        issues = tuple(
            (
                _safe_path(issue.path),
                _safe_issue_code(issue.code),
                "project_discovery",
                _safe_remediation(issue.remediation),
                observed_at,
            )
            for issue in result.issues
        )
        duplicates = _duplicate_rows(result, observed_at)

        with self._database.transaction() as connection:
            connection.execute("delete from discovery_issues")
            connection.execute("delete from discovery_duplicate_candidates")
            connection.executemany(
                """
                insert into discovery_issues(
                    path, code, affected_capability, remediation, observed_at
                ) values (?, ?, ?, ?, ?)
                """,
                issues,
            )
            connection.executemany(
                """
                insert into discovery_duplicate_candidates(
                    fingerprint_kind, fingerprint, candidate_path, observed_at
                ) values (?, ?, ?, ?)
                """,
                duplicates,
            )

    def snapshot(self) -> DiscoveryFindingSnapshot:
        with self._database.connect(readonly=True) as connection:
            issue_rows = connection.execute(
                """
                select path, code, affected_capability, remediation, observed_at
                from discovery_issues
                order by path, code
                limit ?
                """,
                (_MAX_FINDINGS,),
            ).fetchall()
            duplicate_rows = connection.execute(
                """
                select fingerprint_kind, fingerprint, candidate_path, observed_at
                from discovery_duplicate_candidates
                order by fingerprint_kind, fingerprint, candidate_path
                limit ?
                """,
                (_MAX_FINDINGS,),
            ).fetchall()

        grouped: dict[tuple[str, str, str], list[Path]] = {}
        for row in duplicate_rows:
            key = (row["fingerprint_kind"], row["fingerprint"], row["observed_at"])
            grouped.setdefault(key, []).append(Path(row["candidate_path"]))
        duplicates = tuple(
            DuplicateCandidateFinding(
                fingerprint_kind=kind,
                fingerprint=fingerprint,
                candidate_paths=tuple(paths),
                observed_at=observed_at,
            )
            for (kind, fingerprint, observed_at), paths in grouped.items()
        )
        return DiscoveryFindingSnapshot(
            issues=tuple(
                DiscoveryIssueFinding(
                    path=Path(row["path"]),
                    code=row["code"],
                    affected_capability=row["affected_capability"],
                    remediation=row["remediation"],
                    observed_at=row["observed_at"],
                )
                for row in issue_rows
            ),
            duplicates=duplicates,
        )


def _duplicate_rows(
    result: DiscoveryResult, observed_at: str
) -> tuple[tuple[str, str, str, str], ...]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for candidate in result.candidates:
        candidate_path = _safe_path(candidate.canonical_path)
        for kind, fingerprint in (
            ("git_remote", candidate.git_remote_fingerprint),
            ("manifest", candidate.manifest_fingerprint),
        ):
            if fingerprint is None:
                continue
            if _FINGERPRINT.fullmatch(fingerprint) is None:
                raise ValueError("discovery fingerprint is invalid")
            grouped.setdefault((kind, fingerprint), set()).add(candidate_path)
    return tuple(
        (kind, fingerprint, path, observed_at)
        for (kind, fingerprint), paths in sorted(grouped.items())
        if len(paths) > 1
        for path in sorted(paths)
    )


def _safe_path(value: Path) -> str:
    path = Path(value)
    rendered = str(path)
    if (
        not path.is_absolute()
        or not rendered
        or len(rendered) > _MAX_PATH_CHARS
        or "\x00" in rendered
    ):
        raise ValueError("discovery path is invalid")
    return rendered


def _safe_issue_code(value: str) -> str:
    if value not in _ISSUE_CODES:
        raise ValueError("discovery issue code is invalid")
    return value


def _safe_remediation(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > _MAX_REMEDIATION_CHARS
        or "\x00" in value
    ):
        raise ValueError("discovery remediation is invalid")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
