from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from project_memory_hub.domain import AdapterCheckpoint, SourceAgent
from project_memory_hub.security.identifiers import safe_persisted_identifier
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.utf8 import contains_unsafe_text_control


_MAX_PENDING_PER_SCOPE = 256
_MAX_PENDING_GLOBAL = 10_000
_MAX_RECOVERY_LOCATORS = 512
_MAX_SQLITE_INTEGER = 2**63 - 1
_CODEX_DEFERRED_PARSER_VERSION = "codex-v3"


class DeferredRecordCapacityError(RuntimeError):
    """The bounded Codex deferred-record quarantine is full."""


class DeferredRecoveryError(RuntimeError):
    """A deferred source could not be replayed without weakening provenance."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodexDeferredLocator:
    scope: str
    source_record_id: str
    parser_version: str
    source_device: int
    source_inode: int
    prefix_length: int
    prefix_sha256: str

    @classmethod
    def from_checkpoint(
        cls,
        scope: str,
        source_record_id: str,
        checkpoint: AdapterCheckpoint,
    ) -> CodexDeferredLocator:
        prepared_scope = _bounded_text(scope, "scope", max_chars=4096, max_bytes=16384)
        scope_path = PurePosixPath(prepared_scope)
        scope_parts = scope_path.parts
        if (
            "\\" in prepared_scope
            or scope_path.is_absolute()
            or not scope_parts
            or any(part in {"", ".", ".."} for part in scope_parts)
            or not scope_parts[-1].endswith(".jsonl")
            or ".git" in scope_parts
            or scope_path.as_posix() != prepared_scope
            or contains_unsafe_text_control(prepared_scope)
        ):
            raise ValueError("deferred locator scope is invalid")
        try:
            prepared_record_id = safe_persisted_identifier(
                source_record_id,
                "source_record_id",
                Redactor(),
            )
        except ValueError:
            raise ValueError("deferred locator source_record_id is invalid") from None
        if checkpoint.adapter is not SourceAgent.CODEX or checkpoint.scope != prepared_scope:
            raise ValueError("deferred locator checkpoint namespace mismatch")
        parser_version = _bounded_text(
            checkpoint.parser_version,
            "parser_version",
            max_chars=128,
            max_bytes=512,
        )
        if parser_version != _CODEX_DEFERRED_PARSER_VERSION:
            raise ValueError("deferred locator parser version is unsupported")
        cursor = checkpoint.cursor
        relative_path = cursor.get("relative_path")
        device = cursor.get("device")
        inode = cursor.get("inode")
        observed_size = cursor.get("observed_size")
        offset = cursor.get("offset")
        prefix_length = cursor.get("prefix_length")
        prefix_sha256 = cursor.get("prefix_sha256")
        if relative_path != prepared_scope:
            raise ValueError("deferred locator relative path mismatch")
        if type(device) is not int or not 0 <= device <= _MAX_SQLITE_INTEGER:
            raise ValueError("deferred locator device is invalid")
        if type(inode) is not int or not 0 <= inode <= _MAX_SQLITE_INTEGER:
            raise ValueError("deferred locator inode is invalid")
        if (
            type(offset) is not int
            or not 0 < offset <= _MAX_SQLITE_INTEGER
            or type(prefix_length) is not int
            or prefix_length != offset
            or type(observed_size) is not int
            or not offset <= observed_size <= _MAX_SQLITE_INTEGER
        ):
            raise ValueError("deferred locator prefix length is invalid")
        if (
            not isinstance(prefix_sha256, str)
            or len(prefix_sha256) != 64
            or any(character not in "0123456789abcdef" for character in prefix_sha256)
        ):
            raise ValueError("deferred locator prefix hash is invalid")
        return cls(
            scope=prepared_scope,
            source_record_id=prepared_record_id,
            parser_version=parser_version,
            source_device=device,
            source_inode=inode,
            prefix_length=prefix_length,
            prefix_sha256=prefix_sha256,
        )


@dataclass(frozen=True, slots=True)
class CodexDeferredRecord:
    deferred_id: str
    locator: CodexDeferredLocator
    state: str


class CodexDeferredRecordRepository:
    def defer_on_connection(
        self,
        connection: sqlite3.Connection,
        locator: CodexDeferredLocator,
    ) -> bool:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        if type(locator) is not CodexDeferredLocator:
            raise TypeError("locator must be a CodexDeferredLocator")
        timestamp = _utc_now()
        cursor = connection.execute(
            """
            insert into codex_deferred_records(
                deferred_id, source_agent, scope, source_record_id,
                parser_version, source_device, source_inode,
                prefix_length, prefix_sha256, reason_code, state,
                first_seen_at, last_attempt_at, attempt_count,
                last_error_code, recovered_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(
                source_agent, scope, source_device, source_inode,
                parser_version, source_record_id
            ) do nothing
            """,
            (
                str(uuid4()).lower(),
                SourceAgent.CODEX.value,
                locator.scope,
                locator.source_record_id,
                locator.parser_version,
                locator.source_device,
                locator.source_inode,
                locator.prefix_length,
                locator.prefix_sha256,
                "project_not_found",
                "pending",
                timestamp,
                timestamp,
                1,
                "project_not_found",
                None,
            ),
        )
        if cursor.rowcount not in {0, 1}:
            raise RuntimeError("deferred record insert was not deterministic")
        pending_in_scope = connection.execute(
            """
            select count(*) from codex_deferred_records
            where source_agent = ? and scope = ? and state = 'pending'
            """,
            (SourceAgent.CODEX.value, locator.scope),
        ).fetchone()[0]
        pending_global = connection.execute(
            """
            select count(*) from codex_deferred_records
            where source_agent = ? and state = 'pending'
            """,
            (SourceAgent.CODEX.value,),
        ).fetchone()[0]
        if (
            type(pending_in_scope) is not int
            or type(pending_global) is not int
            or pending_in_scope > _MAX_PENDING_PER_SCOPE
            or pending_global > _MAX_PENDING_GLOBAL
        ):
            raise DeferredRecordCapacityError("deferred capacity exceeded")
        return cursor.rowcount == 1

    def records_for_source(
        self,
        database: object,
        source_record_id: str,
    ) -> tuple[CodexDeferredRecord, ...]:
        from project_memory_hub.storage.database import Database

        if not isinstance(database, Database):
            raise TypeError("deferred recovery requires a writable database")
        prepared_id = _source_record_id(source_record_id)
        with database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select deferred_id, scope, source_record_id, parser_version,
                       source_device, source_inode, prefix_length,
                       prefix_sha256, state
                from codex_deferred_records
                where source_agent = ? and source_record_id = ?
                order by first_seen_at, deferred_id
                limit ?
                """,
                (
                    SourceAgent.CODEX.value,
                    prepared_id,
                    _MAX_RECOVERY_LOCATORS + 1,
                ),
            ).fetchall()
        if len(rows) > _MAX_RECOVERY_LOCATORS:
            raise DeferredRecoveryError("replay_limit")
        return tuple(_stored_record(row) for row in rows)

    def record_attempt(
        self,
        database: object,
        records: tuple[CodexDeferredRecord, ...],
        error_code: str,
    ) -> None:
        from project_memory_hub.storage.database import Database

        if not isinstance(database, Database):
            raise TypeError("deferred recovery requires a writable database")
        if error_code not in {
            "project_not_found",
            "source_unavailable",
            "source_changed",
            "replay_limit",
            "ambiguous_source",
            "rejected",
        }:
            raise ValueError("invalid deferred recovery error")
        pending_ids = tuple(record.deferred_id for record in records if record.state == "pending")
        if not pending_ids:
            return
        timestamp = _utc_now()
        with database.transaction() as connection:
            for deferred_id in pending_ids:
                connection.execute(
                    """
                    update codex_deferred_records
                    set last_attempt_at = ?,
                        attempt_count = min(attempt_count + 1, 2147483647),
                        last_error_code = ?
                    where deferred_id = ? and state = 'pending'
                    """,
                    (timestamp, error_code, deferred_id),
                )

    @staticmethod
    def mark_recovered_on_connection(
        connection: sqlite3.Connection,
        records: tuple[CodexDeferredRecord, ...],
        *,
        recovered_at: str,
    ) -> int:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        pending_ids = tuple(record.deferred_id for record in records if record.state == "pending")
        changed = 0
        for deferred_id in pending_ids:
            cursor = connection.execute(
                """
                update codex_deferred_records
                set state = 'recovered', recovered_at = ?,
                    last_attempt_at = ?,
                    attempt_count = min(attempt_count + 1, 2147483647),
                    last_error_code = null
                where deferred_id = ? and state = 'pending'
                """,
                (recovered_at, recovered_at, deferred_id),
            )
            changed += cursor.rowcount
        if changed != len(pending_ids):
            raise DeferredRecoveryError("ambiguous_source")
        return changed


def _bounded_text(
    value: object,
    name: str,
    *,
    max_chars: int,
    max_bytes: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_chars
        or len(value.encode("utf-8")) > max_bytes
        or "\x00" in value
    ):
        raise ValueError(f"deferred locator {name} is invalid")
    return value


def _source_record_id(value: str) -> str:
    try:
        return safe_persisted_identifier(value, "source_record_id", Redactor())
    except ValueError:
        raise ValueError("deferred source_record_id is invalid") from None


def _stored_record(row: sqlite3.Row) -> CodexDeferredRecord:
    state = row["state"]
    if state not in {"pending", "recovered"}:
        raise DeferredRecoveryError("rejected")
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope=row["scope"],
        cursor={
            "device": row["source_device"],
            "inode": row["source_inode"],
            "observed_size": row["prefix_length"],
            "offset": row["prefix_length"],
            "prefix_length": row["prefix_length"],
            "prefix_sha256": row["prefix_sha256"],
            "relative_path": row["scope"],
        },
        parser_version=row["parser_version"],
    )
    try:
        locator = CodexDeferredLocator.from_checkpoint(
            row["scope"],
            row["source_record_id"],
            checkpoint,
        )
    except (TypeError, ValueError):
        raise DeferredRecoveryError("rejected") from None
    deferred_id = row["deferred_id"]
    if not isinstance(deferred_id, str) or len(deferred_id) != 36:
        raise DeferredRecoveryError("rejected")
    return CodexDeferredRecord(
        deferred_id=deferred_id,
        locator=locator,
        state=state,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
