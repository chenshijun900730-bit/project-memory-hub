from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from project_memory_hub.domain import AdapterCheckpoint, SourceAgent
from project_memory_hub.storage.database import (
    Database,
    ReadonlyDatabaseSnapshot,
    _active_receipt_mutation_count,
    _active_transaction_token,
    strict_utc_epoch_us,
)


class CheckpointConflictError(RuntimeError):
    """The persisted checkpoint changed after the adapter read it."""


_PRIOR_CODEX_RECEIPT_PROOF_SEAL = object()
_MAX_PRIOR_CODEX_RECEIPT_PROOFS = 10_000
_MAX_PRIOR_CODEX_RECEIPT_ROWS = 100_000


@dataclass(frozen=True, slots=True)
class _PriorCodexReceiptProof:
    source_agent: SourceAgent
    source_record_id: str
    _source_hash: str = field(repr=False)
    _imported_at: str = field(repr=False)
    _connection: sqlite3.Connection = field(repr=False, compare=False)
    _transaction_token: object = field(repr=False, compare=False)
    _receipt_mutation_count: int = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.source_agent is not SourceAgent.CODEX
            or self._seal is not _PRIOR_CODEX_RECEIPT_PROOF_SEAL
            or not self._connection.in_transaction
            or _active_transaction_token(self._connection) is not self._transaction_token
            or _active_receipt_mutation_count(self._connection) != self._receipt_mutation_count
        ):
            raise ValueError("invalid prior Codex receipt proof")

    def matches(
        self,
        connection: sqlite3.Connection,
        source_agent: SourceAgent,
        source_record_id: str,
    ) -> bool:
        if not (
            self._connection is connection
            and connection.in_transaction
            and _active_transaction_token(connection) is self._transaction_token
            and _active_receipt_mutation_count(connection) == self._receipt_mutation_count
            and source_agent is self.source_agent
            and source_record_id == self.source_record_id
        ):
            return False
        row = connection.execute(
            """
            select 1 from import_receipts
            where source_hash = ? and source_record_id = ?
              and source_agent = ? and imported_at = ?
            """,
            (
                self._source_hash,
                self.source_record_id,
                self.source_agent.value,
                self._imported_at,
            ),
        ).fetchone()
        return row is not None


class CheckpointRepository:
    def __init__(self, database: Database | ReadonlyDatabaseSnapshot) -> None:
        self._database = database

    @property
    def database(self) -> Database | ReadonlyDatabaseSnapshot:
        return self._database

    def get(self, adapter: SourceAgent, scope: str) -> AdapterCheckpoint | None:
        source_agent = SourceAgent(adapter)
        normalized_scope = _scope(scope)
        with self._database.connect(readonly=True) as connection:
            return self._get_on_connection(connection, source_agent, normalized_scope)

    def commit(
        self,
        adapter: SourceAgent,
        scope: str,
        checkpoint: AdapterCheckpoint,
        *,
        source_record_ids: tuple[str, ...] = (),
    ) -> None:
        with self._database.transaction() as connection:
            source_agent = SourceAgent(adapter)
            normalized_scope = _scope(scope)
            expected_checkpoint = self._get_on_connection(
                connection,
                source_agent,
                normalized_scope,
            )
            self.commit_on_connection(
                connection,
                source_agent,
                normalized_scope,
                expected_checkpoint=expected_checkpoint,
                next_checkpoint=checkpoint,
                source_record_ids=source_record_ids,
            )

    def commit_on_connection(
        self,
        connection: sqlite3.Connection,
        adapter: SourceAgent,
        scope: str,
        *,
        expected_checkpoint: AdapterCheckpoint | None,
        next_checkpoint: AdapterCheckpoint,
        source_record_ids: tuple[str, ...] = (),
    ) -> None:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        source_agent = SourceAgent(adapter)
        normalized_scope = _scope(scope)
        _validate_checkpoint(next_checkpoint, source_agent, normalized_scope)
        if expected_checkpoint is not None:
            _validate_checkpoint(expected_checkpoint, source_agent, normalized_scope)
        prepared_ids = tuple(_source_record_id(value) for value in source_record_ids)
        try:
            current_checkpoint = self._get_on_connection(
                connection,
                source_agent,
                normalized_scope,
            )
        except ValueError:
            raise CheckpointConflictError("checkpoint conflict") from None
        if not _checkpoints_match(current_checkpoint, expected_checkpoint):
            raise CheckpointConflictError("checkpoint conflict")

        imported_at = _utc_now()
        source_hash = _checkpoint_source_hash(
            source_agent,
            normalized_scope,
            next_checkpoint,
        )
        for record_id in prepared_ids:
            if self.receipt_exists_on_connection(
                connection,
                source_hash,
                record_id,
                source_agent,
            ):
                continue
            try:
                cursor = connection.execute(
                    """
                    insert into import_receipts(
                        source_hash, source_record_id, source_agent, imported_at
                    ) values (?, ?, ?, ?)
                    """,
                    (source_hash, record_id, source_agent.value, imported_at),
                )
            except sqlite3.IntegrityError:
                if self.receipt_exists_on_connection(
                    connection,
                    source_hash,
                    record_id,
                    source_agent,
                ):
                    raise CheckpointConflictError("checkpoint conflict") from None
                raise
            if cursor.rowcount != 1:
                raise CheckpointConflictError("checkpoint conflict")

        cursor = connection.execute(
            """
            insert into checkpoints(
                adapter, scope, cursor_json, parser_version, updated_at
            ) values (?, ?, ?, ?, ?)
            on conflict(adapter, scope) do update set
                cursor_json = excluded.cursor_json,
                parser_version = excluded.parser_version,
                updated_at = excluded.updated_at
            """,
            (
                source_agent.value,
                normalized_scope,
                _canonical_json(next_checkpoint.cursor),
                next_checkpoint.parser_version,
                imported_at,
            ),
        )
        if cursor.rowcount != 1:
            raise CheckpointConflictError("checkpoint conflict")

    def receipt_exists(
        self,
        source_hash: str,
        source_record_id: str,
        source_agent: SourceAgent,
    ) -> bool:
        with self._database.connect(readonly=True) as connection:
            return self.receipt_exists_on_connection(
                connection,
                source_hash,
                source_record_id,
                source_agent,
            )

    def receipt_exists_on_connection(
        self,
        connection: sqlite3.Connection,
        source_hash: str,
        source_record_id: str,
        source_agent: SourceAgent,
    ) -> bool:
        prepared_hash = _source_hash(source_hash)
        prepared_id = _source_record_id(source_record_id)
        agent = SourceAgent(source_agent)
        row = connection.execute(
            """
            select 1 from import_receipts
            where source_hash = ? and source_record_id = ? and source_agent = ?
            """,
            (prepared_hash, prepared_id, agent.value),
        ).fetchone()
        return row is not None

    def prior_codex_receipt_proof_on_connection(
        self,
        connection: sqlite3.Connection,
        source_record_id: str,
    ) -> _PriorCodexReceiptProof | None:
        return self.prior_codex_receipt_proofs_on_connection(
            connection,
            (source_record_id,),
        )[0]

    def prior_codex_receipt_proofs_on_connection(
        self,
        connection: sqlite3.Connection,
        source_record_ids: tuple[str, ...],
    ) -> tuple[_PriorCodexReceiptProof | None, ...]:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        transaction_token = _active_transaction_token(connection)
        receipt_mutation_count = _active_receipt_mutation_count(connection)
        if transaction_token is None or receipt_mutation_count is None:
            raise ValueError("database managed transaction required")
        if connection.total_changes != 0:
            raise ValueError("prior committed receipt proof requires a pristine transaction")
        if len(source_record_ids) > _MAX_PRIOR_CODEX_RECEIPT_PROOFS:
            raise ValueError("too many prior Codex receipt proofs requested")
        prepared_ids = tuple(_source_record_id(value) for value in source_record_ids)
        if not prepared_ids:
            return ()
        # One scan serves the bounded production Codex batch. The schema has no
        # reverse receipt index, so never repeat this query once per record.
        cursor = connection.execute(
            """
            select source_hash, source_record_id, imported_at
            from import_receipts
            where source_agent = ?
              and source_record_id in (select value from json_each(?))
            """,
            (SourceAgent.CODEX.value, _canonical_json(prepared_ids)),
        )
        rows = cursor.fetchmany(_MAX_PRIOR_CODEX_RECEIPT_ROWS + 1)
        if len(rows) > _MAX_PRIOR_CODEX_RECEIPT_ROWS:
            raise ValueError("too many prior Codex receipt rows")
        requested_id_set = frozenset(prepared_ids)
        receipts_by_id: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            record_id = row["source_record_id"]
            source_hash = row["source_hash"]
            imported_at = row["imported_at"]
            try:
                prepared_record_id = _source_record_id(record_id)
                prepared_source_hash = _source_hash(source_hash)
            except (TypeError, ValueError):
                raise ValueError("malformed prior Codex receipt") from None
            if (
                prepared_record_id not in requested_id_set
                or type(imported_at) is not str
                or strict_utc_epoch_us(imported_at) is None
            ):
                raise ValueError("malformed prior Codex receipt")
            receipts_by_id.setdefault(prepared_record_id, []).append(
                (prepared_source_hash, imported_at)
            )
        witness_by_id = {record_id: min(receipts) for record_id, receipts in receipts_by_id.items()}
        return tuple(
            None
            if (witness := witness_by_id.get(prepared_id)) is None
            else _PriorCodexReceiptProof(
                source_agent=SourceAgent.CODEX,
                source_record_id=prepared_id,
                _source_hash=witness[0],
                _imported_at=witness[1],
                _connection=connection,
                _transaction_token=transaction_token,
                _receipt_mutation_count=receipt_mutation_count,
                _seal=_PRIOR_CODEX_RECEIPT_PROOF_SEAL,
            )
            for prepared_id in prepared_ids
        )

    def commit_import_receipt(
        self,
        source_hash: str,
        source_record_id: str,
        source_agent: SourceAgent,
        *,
        confirmation: dict[str, object] | None = None,
        transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        with self._database.transaction() as connection:
            if transaction_guard is not None:
                transaction_guard(connection)
            if self.receipt_exists_on_connection(
                connection,
                source_hash,
                source_record_id,
                source_agent,
            ):
                if transaction_guard is not None:
                    transaction_guard(connection)
                return
            self.commit_import_receipt_on_connection(
                connection,
                source_hash,
                source_record_id,
                source_agent,
                confirmation=confirmation,
            )
            if transaction_guard is not None:
                transaction_guard(connection)

    def commit_import_receipt_on_connection(
        self,
        connection: sqlite3.Connection,
        source_hash: str,
        source_record_id: str,
        source_agent: SourceAgent,
        *,
        confirmation: dict[str, object] | None = None,
    ) -> None:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        prepared_hash = _source_hash(source_hash)
        prepared_id = _source_record_id(source_record_id)
        agent = SourceAgent(source_agent)
        imported_at = _utc_now()
        try:
            cursor = connection.execute(
                """
                insert into import_receipts(
                    source_hash, source_record_id, source_agent, imported_at
                ) values (?, ?, ?, ?)
                """,
                (prepared_hash, prepared_id, agent.value, imported_at),
            )
        except sqlite3.IntegrityError:
            if self.receipt_exists_on_connection(
                connection,
                prepared_hash,
                prepared_id,
                agent,
            ):
                raise CheckpointConflictError("checkpoint conflict") from None
            raise
        if cursor.rowcount != 1:
            raise CheckpointConflictError("checkpoint conflict")
        if confirmation is None:
            return
        confirmation_key = (
            "chatgpt_confirmation:"
            + hashlib.sha256(f"{prepared_hash}:{prepared_id}".encode("utf-8")).hexdigest()
        )
        confirmation_cursor = connection.execute(
            """
            insert into app_state(name, value_json, updated_at)
            values (?, ?, ?)
            on conflict(name) do update set
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (confirmation_key, _canonical_json(confirmation), imported_at),
        )
        if confirmation_cursor.rowcount != 1:
            raise CheckpointConflictError("checkpoint conflict")

    @staticmethod
    def _get_on_connection(
        connection: sqlite3.Connection,
        adapter: SourceAgent,
        scope: str,
    ) -> AdapterCheckpoint | None:
        row = connection.execute(
            """
            select cursor_json, parser_version
            from checkpoints
            where adapter = ? and scope = ?
            """,
            (adapter.value, scope),
        ).fetchone()
        if row is None:
            return None
        try:
            cursor_document: object = json.loads(row["cursor_json"])
        except (TypeError, ValueError):
            raise ValueError("checkpoint cursor must be valid JSON") from None
        cursor = _strict_checkpoint_cursor(cursor_document)
        return AdapterCheckpoint(
            adapter=adapter,
            scope=scope,
            cursor=cursor,
            parser_version=row["parser_version"],
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_checkpoint(
    checkpoint: AdapterCheckpoint,
    adapter: SourceAgent,
    scope: str,
) -> None:
    if checkpoint.adapter != adapter or checkpoint.scope != scope:
        raise ValueError("checkpoint namespace mismatch")
    _strict_checkpoint_cursor(checkpoint.cursor)


def _strict_checkpoint_cursor(value: object) -> dict[str, str | int]:
    if not isinstance(value, dict):
        raise ValueError("checkpoint cursor must be an object")
    cursor: dict[str, str | int] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise ValueError("checkpoint cursor keys must be strings")
        if type(item) is str:
            cursor[key] = item
        elif type(item) is int:
            cursor[key] = item
        else:
            raise ValueError("checkpoint cursor values must be strings or integers")
    return cursor


def _checkpoints_match(
    current: AdapterCheckpoint | None,
    expected: AdapterCheckpoint | None,
) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    return (
        current.adapter == expected.adapter
        and current.scope == expected.scope
        and current.parser_version == expected.parser_version
        and _canonical_json(current.cursor) == _canonical_json(expected.cursor)
    )


def _checkpoint_source_hash(
    adapter: SourceAgent,
    scope: str,
    checkpoint: AdapterCheckpoint,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "adapter": adapter.value,
                "device": checkpoint.cursor.get("device", ""),
                "inode": checkpoint.cursor.get("inode", ""),
                "scope": scope,
            }
        ).encode("utf-8")
    ).hexdigest()


def _scope(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("scope must be non-empty")
    return value


def _source_record_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 513
        or len(value.encode("utf-8")) > 2049
    ):
        raise ValueError("source_record_id must be non-empty")
    return value


def _source_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("source_hash must be lowercase sha256")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
