import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from threading import Lock
from uuid import uuid4


_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]+)_.+[.]sql$")
_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_TIMESTAMP_CHARS = 128


@dataclass(slots=True)
class _ManagedTransactionToken:
    receipt_mutation_count: int = 0


_TRANSACTION_TOKENS: dict[sqlite3.Connection, _ManagedTransactionToken] = {}
_TRANSACTION_TOKENS_LOCK = Lock()


class ReadonlySnapshotChangedError(RuntimeError):
    """The backing database changed while an immutable read was active."""


class SchemaUpgradeRequiredError(RuntimeError):
    """The runtime schema is not the exact schema packaged with this build."""


def _active_transaction_token(connection: sqlite3.Connection) -> object | None:
    with _TRANSACTION_TOKENS_LOCK:
        return _TRANSACTION_TOKENS.get(connection)


def _active_receipt_mutation_count(connection: sqlite3.Connection) -> int | None:
    with _TRANSACTION_TOKENS_LOCK:
        token = _TRANSACTION_TOKENS.get(connection)
        return None if token is None else token.receipt_mutation_count


@contextmanager
def _managed_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise ValueError("connection already has an active transaction")
    token = _ManagedTransactionToken()
    tracker_id = uuid4().hex
    function_name = f"_pmh_note_import_receipt_mutation_{tracker_id}"
    trigger_specs = (
        (f"_pmh_import_receipts_insert_{tracker_id}", "insert"),
        (f"_pmh_import_receipts_update_{tracker_id}", "update"),
        (f"_pmh_import_receipts_delete_{tracker_id}", "delete"),
    )
    installed_triggers: list[str] = []

    def note_receipt_mutation() -> int:
        token.receipt_mutation_count += 1
        return 0

    def cleanup_tracker() -> None:
        cleanup_succeeded = True
        for trigger_name in reversed(installed_triggers):
            try:
                connection.execute(f'drop trigger if exists temp."{trigger_name}"')
            except sqlite3.Error:
                cleanup_succeeded = False
        if cleanup_succeeded:
            try:
                remaining = connection.execute(
                    """
                    select count(*) from sqlite_temp_master
                    where type = 'trigger' and name in (?, ?, ?)
                    """,
                    tuple(name for name, _operation in trigger_specs),
                ).fetchone()[0]
            except sqlite3.Error:
                cleanup_succeeded = False
            else:
                cleanup_succeeded = remaining == 0
        if cleanup_succeeded:
            connection.create_function(function_name, 0, None)

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.create_function(function_name, 0, note_receipt_mutation)
        for trigger_name, operation in trigger_specs:
            connection.execute(
                f"""
                create temp trigger "{trigger_name}"
                after {operation} on main.import_receipts
                begin
                    select {function_name}();
                end
                """
            )
            installed_triggers.append(trigger_name)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        cleanup_tracker()
        raise
    with _TRANSACTION_TOKENS_LOCK:
        if connection in _TRANSACTION_TOKENS:
            connection.rollback()
            cleanup_tracker()
            raise RuntimeError("managed transaction already registered")
        _TRANSACTION_TOKENS[connection] = token
    try:
        yield
        if not connection.in_transaction:
            raise RuntimeError("managed transaction ended unexpectedly")
        for trigger_name in reversed(installed_triggers):
            connection.execute(f'drop trigger temp."{trigger_name}"')
        installed_triggers.clear()
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        try:
            cleanup_tracker()
        finally:
            with _TRANSACTION_TOKENS_LOCK:
                if _TRANSACTION_TOKENS.get(connection) is token:
                    del _TRANSACTION_TOKENS[connection]


def strict_utc_epoch_us(value: object) -> int | None:
    """Return an exact UTC epoch for strict, timezone-aware ISO text."""
    if not isinstance(value, str) or not value or len(value) > _MAX_TIMESTAMP_CHARS:
        return None
    try:
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None
        delta = parsed.astimezone(timezone.utc) - _UNIX_EPOCH
    except (OverflowError, TypeError, ValueError):
        return None
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _register_sql_functions(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "strict_utc_epoch_us",
        1,
        strict_utc_epoch_us,
        deterministic=True,
    )


@dataclass(frozen=True, slots=True)
class Database:
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))

    @contextmanager
    def connect(self, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        readonly_state = None
        if readonly:
            readonly_state = _strict_readonly_state(self.path)
            database = f"{self.path.resolve().as_uri()}?mode=ro&immutable=1"
            connection = sqlite3.connect(database, uri=True)
        else:
            connection = sqlite3.connect(self.path)

        completed = False
        try:
            _register_sql_functions(connection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if readonly:
                connection.execute("PRAGMA query_only=ON")
            else:
                _chmod_database_files(self.path)
                connection.execute("PRAGMA journal_mode=WAL").fetchone()
                _chmod_database_files(self.path)
            yield connection
            completed = True
        finally:
            connection.close()
            if not readonly:
                _chmod_database_files(self.path)
            elif completed and _readonly_file_state(self.path) != readonly_state:
                raise ReadonlySnapshotChangedError("read-only snapshot unavailable")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            with _managed_transaction(connection):
                yield connection

    def initialize(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            raise FileNotFoundError(parent)
        if not parent.is_dir():
            raise NotADirectoryError(parent)

        migrations = _load_migrations()
        with self.connect() as connection:
            connection.execute("PRAGMA temp_store=MEMORY")
            _create_migration_table(connection)
            _validate_migration_history(connection, migrations)
            for version, sql in migrations:
                _apply_migration(connection, version, sql)
            _validate_migration_history(connection, migrations)
            _chmod_database_files(self.path)

    def require_current_schema_readonly(self) -> None:
        try:
            with self.connect(readonly=True) as connection:
                _require_current_schema_on_connection(connection)
        except (sqlite3.DatabaseError, sqlite3.OperationalError):
            raise SchemaUpgradeRequiredError("schema upgrade required") from None

    def backup(self, destination: Path) -> Path:
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(destination)

        destination_connection: sqlite3.Connection | None = None
        try:
            source_database = f"{self.path.resolve().as_uri()}?mode=ro"
            source_connection = sqlite3.connect(source_database, uri=True)
            try:
                source_connection.execute("PRAGMA busy_timeout=5000")
                destination_connection = sqlite3.connect(destination)
                _chmod_private_file(destination)
                source_connection.backup(destination_connection)
                destination_connection.commit()
            finally:
                source_connection.close()
            destination_connection.close()
            destination_connection = None
            _chmod_private_file(destination)
            return destination
        except BaseException:
            if destination_connection is not None:
                destination_connection.close()
            destination.unlink(missing_ok=True)
            raise


class ReadonlyDatabaseSnapshot:
    _MAX_DATABASE_BYTES = 512 * 1024 * 1024

    def __init__(self, source_path: Path, *, migrate: bool = True) -> None:
        self.path = Path(source_path)
        document = bytearray(_stable_database_document(self.path, self._MAX_DATABASE_BYTES))
        if (
            len(document) < 100
            or document[:16] != b"SQLite format 3\x00"
            or document[18] not in {1, 2}
            or document[19] not in {1, 2}
        ):
            raise RuntimeError("database snapshot rejected")
        document[18] = 1
        document[19] = 1
        self._connection = sqlite3.connect(":memory:")
        try:
            _register_sql_functions(self._connection)
            self._connection.deserialize(document)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA temp_store=MEMORY")
            self._connection.execute("PRAGMA foreign_keys=ON")
            if migrate:
                _migrate_snapshot_connection(self._connection)
            else:
                _require_current_schema_on_connection(self._connection)
            self._connection.execute("PRAGMA query_only=ON")
        except BaseException:
            self._connection.close()
            raise
        self._closed = False

    @contextmanager
    def connect(self, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        if not readonly:
            raise PermissionError("snapshot database is read-only")
        if self._closed:
            raise RuntimeError("snapshot database is closed")
        yield self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        raise PermissionError("snapshot database is read-only")
        yield self._connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


def _load_migrations() -> tuple[tuple[int, str], ...]:
    migration_directory = resources.files("project_memory_hub.storage").joinpath("migrations")
    migrations: list[tuple[int, str]] = []
    versions: set[int] = set()

    for resource in sorted(migration_directory.iterdir(), key=lambda item: item.name):
        match = _MIGRATION_NAME.fullmatch(resource.name)
        if match is None or not resource.is_file():
            continue
        version = int(match.group("version"))
        if version in versions:
            raise RuntimeError(f"duplicate migration version: {version}")
        versions.add(version)
        migrations.append((version, resource.read_text(encoding="utf-8")))

    return tuple(migrations)


def _require_current_schema_on_connection(connection: sqlite3.Connection) -> None:
    migrations = _load_migrations()
    known_versions = tuple(version for version, _sql in migrations)
    if known_versions != tuple(range(1, len(known_versions) + 1)):
        raise SchemaUpgradeRequiredError("schema upgrade required")
    try:
        applied_versions = tuple(
            row[0]
            for row in connection.execute(
                "select version from schema_migrations order by version"
            ).fetchall()
        )
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        raise SchemaUpgradeRequiredError("schema upgrade required") from None
    if applied_versions != known_versions:
        raise SchemaUpgradeRequiredError("schema upgrade required")


def _create_migration_table(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(_MIGRATION_TABLE_SQL)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _apply_migration(connection: sqlite3.Connection, version: int, sql: str) -> None:
    try:
        connection.execute("BEGIN EXCLUSIVE")
        already_applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if already_applied is None:
            _execute_sql_script(connection, sql)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (version, _utc_now()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _migrate_snapshot_connection(connection: sqlite3.Connection) -> None:
    try:
        migrations = _load_migrations()
        _create_migration_table(connection)
        _validate_migration_history(connection, migrations)
        for version, sql in migrations:
            _apply_migration(connection, version, sql)
        _validate_migration_history(connection, migrations)
    except Exception:
        raise RuntimeError("database snapshot migration failed") from None


def _validate_migration_history(
    connection: sqlite3.Connection,
    migrations: tuple[tuple[int, str], ...],
) -> None:
    known_versions = tuple(version for version, _sql in migrations)
    if known_versions != tuple(range(1, len(known_versions) + 1)):
        raise RuntimeError("known migration history is incompatible")
    applied_versions = tuple(
        row[0]
        for row in connection.execute(
            "select version from schema_migrations order by version"
        ).fetchall()
    )
    if applied_versions != known_versions[: len(applied_versions)]:
        raise RuntimeError("database migration history is incompatible")


def _execute_sql_script(connection: sqlite3.Connection, sql: str) -> None:
    buffered_characters: list[str] = []
    for character in sql:
        buffered_characters.append(character)
        if character != ";":
            continue
        statement = "".join(buffered_characters).strip()
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            buffered_characters.clear()

    remainder = "".join(buffered_characters).strip()
    if remainder:
        connection.execute(remainder)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _chmod_database_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            _chmod_private_file(candidate)


def _chmod_private_file(path: Path) -> None:
    path.chmod(0o600)


def _strict_readonly_state(path: Path) -> tuple[object, ...]:
    state = _readonly_file_state(path)
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-journal")):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
            raise ReadonlySnapshotChangedError("read-only snapshot unavailable")
    return state


def _readonly_file_state(path: Path) -> tuple[object, ...]:
    return tuple(
        _optional_file_state(candidate)
        for candidate in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
            Path(f"{path}-journal"),
        )
    )


def _optional_file_state(path: Path) -> tuple[int, ...] | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stable_database_document(path: Path, max_bytes: int) -> bytes:
    if not _database_sidecars_quiescent(path):
        raise RuntimeError("read-only database snapshot unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PermissionError("database snapshot rejected") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or stat.S_IMODE(before.st_mode) & stat.S_IRUSR == 0
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise PermissionError("database snapshot rejected")
        chunks: list[bytes] = []
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
            if not chunk:
                raise RuntimeError("database snapshot changed")
            chunks.append(chunk)
            offset += len(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except FileNotFoundError:
            raise RuntimeError("database snapshot changed") from None
        if (
            not _same_file_snapshot(before, after)
            or not _same_file_snapshot(before, path_after)
            or not _database_sidecars_quiescent(path)
        ):
            raise RuntimeError("database snapshot changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _database_sidecars_quiescent(path: Path) -> bool:
    for suffix, allowed_size in (("-wal", 0), ("-shm", None), ("-journal", 0)):
        try:
            metadata = Path(f"{path}{suffix}").lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or (allowed_size is not None and metadata.st_size != allowed_size)
        ):
            return False
    return True


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )
