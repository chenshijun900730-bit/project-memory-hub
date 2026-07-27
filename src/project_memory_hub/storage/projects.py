import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import local
from uuid import UUID, uuid4

from project_memory_hub.domain import ProjectCandidate, ProjectRecord
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot
from project_memory_hub.storage.path_identity import (
    PathIdentity,
    PathIdentitySnapshot,
    complete_directory_identity,
    persisted_identity_matches_at_same_path,
    snapshot_path_identity,
    stored_path_identity,
)


_path_identity = snapshot_path_identity


class _LegacyBatchState(local):
    def __init__(self) -> None:
        self.active = False
        self.cache: dict[PathIdentity, tuple[str, ...]] | None = None


@dataclass(frozen=True, slots=True)
class ProjectControlRecord:
    project_id: UUID
    canonical_path: Path
    display_name: str
    discovery_status: str
    permission_status: str
    inactivity_state: str
    enabled: bool
    last_observed_change: str | None


class ProjectRegistryChangedError(RuntimeError):
    """The trusted project registry changed during a guarded transaction."""


class ProjectRepository:
    def __init__(self, database: Database | ReadonlyDatabaseSnapshot) -> None:
        self._database = database
        self._legacy_batch = _LegacyBatchState()

    @contextmanager
    def discovery_batch(self) -> Iterator[None]:
        previous_active = self._legacy_batch.active
        previous_cache = self._legacy_batch.cache
        self._legacy_batch.active = True
        self._legacy_batch.cache = None
        try:
            yield
        finally:
            self._legacy_batch.active = previous_active
            self._legacy_batch.cache = previous_cache

    def register(self, candidate: ProjectCandidate) -> ProjectRecord:
        canonical_path = _existing_directory(candidate.canonical_path)
        path_identity = complete_directory_identity(canonical_path)
        if path_identity is None:
            raise ValueError("project path is unsafe or changed")
        git_root = (
            str(candidate.git_root.resolve(strict=False))
            if candidate.git_root is not None
            else None
        )
        now = _utc_now()
        with self._database.transaction() as connection:
            existing = _project_owner(
                connection,
                canonical_path,
                path_identity,
                self._legacy_project_ids(connection, path_identity),
            )
            if existing is None:
                project_id = str(uuid4()).lower()
                connection.execute(
                    """
                    insert into projects(
                        project_id, canonical_path, display_name, git_root,
                        git_remote_fingerprint, manifest_fingerprint,
                        discovery_status, permission_status, inactivity_state,
                        enabled, created_at, updated_at, path_device, path_inode
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        str(canonical_path),
                        candidate.display_name,
                        git_root,
                        candidate.git_remote_fingerprint,
                        candidate.manifest_fingerprint,
                        "active",
                        "ok",
                        "active",
                        1,
                        now,
                        now,
                        path_identity[0],
                        path_identity[1],
                    ),
                )
            else:
                project_id = existing["project_id"]
                stored_identity = _stored_path_identity(existing)
                existing_path = Path(existing["canonical_path"])
                identity_matches = (
                    stored_identity == path_identity
                    or (
                        stored_identity is not None
                        and existing_path == canonical_path
                        and persisted_identity_matches_at_same_path(
                            stored_identity,
                            path_identity,
                        )
                    )
                    or (
                        existing_path != canonical_path
                        and complete_directory_identity(existing_path) == path_identity
                    )
                )
                if stored_identity is not None and not identity_matches:
                    raise ValueError("registered project path identity changed")
                if (
                    stored_identity == path_identity
                    and existing_path != canonical_path
                    and complete_directory_identity(existing_path) != path_identity
                ):
                    raise ValueError("project move requires explicit relink")
                connection.execute(
                    """
                    update projects
                    set display_name = ?, git_root = ?,
                        git_remote_fingerprint = ?, manifest_fingerprint = ?,
                        discovery_status = ?, permission_status = ?, updated_at = ?,
                        path_device = ?, path_inode = ?
                    where project_id = ?
                    """,
                    (
                        candidate.display_name,
                        git_root,
                        candidate.git_remote_fingerprint,
                        candidate.manifest_fingerprint,
                        "active",
                        "ok",
                        now,
                        path_identity[0],
                        path_identity[1],
                        project_id,
                    ),
                )
            row = _select_record(connection, project_id)
            record = _to_record(row)
            if complete_directory_identity(canonical_path) != path_identity:
                raise ValueError("project path is unsafe or changed")
            if complete_directory_identity(record.canonical_path) != path_identity:
                raise ValueError("project path is unsafe or changed")
        return record

    def find_by_cwd(self, cwd: Path) -> ProjectRecord | None:
        lexical_cwd = Path(cwd)
        lexical_text = str(lexical_cwd)
        if not lexical_cwd.is_absolute() or os.path.normpath(lexical_text) != lexical_text:
            return None
        before = _path_identity(lexical_cwd)
        if before is None:
            return None
        with self._database.connect(readonly=True) as connection:
            exact = connection.execute(
                """
                select project_id, canonical_path, display_name,
                       discovery_status, permission_status, last_observed_change,
                       enabled, path_device, path_inode
                from projects
                where canonical_path = ?
                """,
                (lexical_text,),
            ).fetchone()
            rows = (
                ()
                if exact is not None
                else connection.execute(
                    """
                select project_id, canonical_path, display_name,
                       discovery_status, permission_status, last_observed_change,
                       enabled, path_device, path_inode
                from projects
                order by canonical_path, project_id
                """
                ).fetchall()
            )

        if exact is not None:
            if not bool(exact["enabled"]):
                return None
            exact_identity = _stored_path_identity(exact)
            live_identity = complete_directory_identity(lexical_cwd)
            if (
                exact_identity is None
                or live_identity is None
                or not before
                or before[-1] != live_identity
                or not persisted_identity_matches_at_same_path(
                    exact_identity,
                    live_identity,
                )
            ):
                return None
            if _path_identity(lexical_cwd) != before:
                return None
            final_identity = complete_directory_identity(lexical_cwd)
            if final_identity is None or not persisted_identity_matches_at_same_path(
                exact_identity,
                final_identity,
            ):
                return None
            return _to_record(exact)

        matches: list[tuple[sqlite3.Row, int]] = []
        for row in rows:
            stored_identity = _stored_path_identity(row)
            stored_path = Path(row["canonical_path"])
            live_identity = complete_directory_identity(stored_path)
            lexical_depth = (
                len(stored_path.parts) - 1 if _is_component_prefix(stored_path, lexical_cwd) else -1
            )
            identity_depth = -1
            if stored_identity is not None:
                try:
                    identity_depth = len(before) - 1 - before[::-1].index(stored_identity)
                except ValueError:
                    pass
            live_depth = -1
            if live_identity is not None:
                try:
                    live_depth = len(before) - 1 - before[::-1].index(live_identity)
                except ValueError:
                    pass
            depth = max(lexical_depth, identity_depth, live_depth)
            if depth >= 0:
                matches.append((row, depth))
        if not matches:
            return None
        selected, _depth = max(
            matches,
            key=lambda item: (
                item[1],
                item[0]["canonical_path"],
                item[0]["project_id"],
            ),
        )
        if not bool(selected["enabled"]):
            return None
        selected_identity = _stored_path_identity(selected)
        if selected_identity is None:
            return None
        selected_path = Path(selected["canonical_path"])
        live_identity = complete_directory_identity(selected_path)
        if live_identity is None or not persisted_identity_matches_at_same_path(
            selected_identity,
            live_identity,
        ):
            return None
        if live_identity not in before:
            return None
        if _path_identity(lexical_cwd) != before:
            return None
        final_identity = complete_directory_identity(selected_path)
        if final_identity is None or not persisted_identity_matches_at_same_path(
            selected_identity,
            final_identity,
        ):
            return None
        return _to_record(selected)

    def get(self, project_id: UUID) -> ProjectRecord:
        with self._database.connect(readonly=True) as connection:
            return _to_record(_select_record(connection, str(project_id).lower()))

    @staticmethod
    def registry_generation_on_connection(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()
        if row is None or type(row["generation"]) is not int or row["generation"] < 0:
            raise ProjectRegistryChangedError("project registry generation is untrusted")
        return row["generation"]

    @classmethod
    def require_records_current_on_connection(
        cls,
        connection: sqlite3.Connection,
        expected_generation: int,
        records: tuple[ProjectRecord, ...],
    ) -> None:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        if type(expected_generation) is not int or expected_generation < 0:
            raise ProjectRegistryChangedError("project registry generation is untrusted")
        if cls.registry_generation_on_connection(connection) != expected_generation:
            raise ProjectRegistryChangedError("project registry changed")

        unique_records: dict[str, ProjectRecord] = {}
        for record in records:
            if not isinstance(record, ProjectRecord):
                raise TypeError("records must contain ProjectRecord values")
            project_id = str(record.project_id).lower()
            previous = unique_records.setdefault(project_id, record)
            if previous.canonical_path != record.canonical_path:
                raise ProjectRegistryChangedError("project registry changed")

        for project_id, record in unique_records.items():
            row = connection.execute(
                """
                select project_id, canonical_path, enabled, path_device, path_inode
                from projects where project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if (
                row is None
                or row["project_id"] != project_id
                or row["canonical_path"] != str(record.canonical_path)
                or type(row["enabled"]) is not int
                or row["enabled"] != 1
            ):
                raise ProjectRegistryChangedError("project registry changed")
            stored_identity = _stored_path_identity(row)
            live_identity = complete_directory_identity(record.canonical_path)
            if (
                stored_identity is None
                or live_identity is None
                or not persisted_identity_matches_at_same_path(
                    stored_identity,
                    live_identity,
                )
            ):
                raise ProjectRegistryChangedError("project registry changed")

        if cls.registry_generation_on_connection(connection) != expected_generation:
            raise ProjectRegistryChangedError("project registry changed")

    def record_matches_identity(
        self,
        project: ProjectRecord,
        identity: PathIdentity,
    ) -> bool:
        with self._database.connect(readonly=True) as connection:
            return self._record_matches_identity_on_connection(
                connection,
                project.project_id,
                project.canonical_path,
                identity,
            )

    def record_is_current(self, project: ProjectRecord) -> bool:
        with self._database.connect(readonly=True) as connection:
            return self._record_is_current_on_connection(connection, project)

    def record_live_identity(self, project: ProjectRecord) -> PathIdentity | None:
        with self._database.connect(readonly=True) as connection:
            return self._record_live_identity_on_connection(connection, project)

    @staticmethod
    def _record_is_current_on_connection(
        connection: sqlite3.Connection,
        project: ProjectRecord,
    ) -> bool:
        return (
            ProjectRepository._record_live_identity_on_connection(
                connection,
                project,
            )
            is not None
        )

    @staticmethod
    def _record_live_identity_on_connection(
        connection: sqlite3.Connection,
        project: ProjectRecord,
    ) -> PathIdentity | None:
        row = connection.execute(
            """
            select canonical_path, enabled, path_device, path_inode
            from projects where project_id = ?
            """,
            (str(project.project_id).lower(),),
        ).fetchone()
        if (
            row is None
            or not bool(row["enabled"])
            or row["canonical_path"] != str(project.canonical_path)
        ):
            return None
        identity = _stored_path_identity(row)
        live_identity = complete_directory_identity(project.canonical_path)
        if not (
            identity is not None
            and live_identity is not None
            and persisted_identity_matches_at_same_path(identity, live_identity)
        ):
            return None
        return live_identity

    @staticmethod
    def _record_matches_identity_on_connection(
        connection: sqlite3.Connection,
        project_id: UUID,
        canonical_path: Path,
        identity: PathIdentity,
    ) -> bool:
        row = connection.execute(
            """
            select canonical_path, enabled, path_device, path_inode
            from projects where project_id = ?
            """,
            (str(project_id).lower(),),
        ).fetchone()
        stored_identity = None if row is None else _stored_path_identity(row)
        return bool(
            row is not None
            and bool(row["enabled"])
            and row["canonical_path"] == str(canonical_path)
            and stored_identity is not None
            and persisted_identity_matches_at_same_path(stored_identity, identity)
        )

    def list_control(self) -> tuple[ProjectControlRecord, ...]:
        with self._database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select project_id, canonical_path, display_name,
                       discovery_status, permission_status, inactivity_state,
                       enabled, last_observed_change
                from projects
                order by display_name collate nocase, project_id
                """
            ).fetchall()
        return tuple(
            ProjectControlRecord(
                project_id=UUID(row["project_id"]),
                canonical_path=Path(row["canonical_path"]),
                display_name=row["display_name"],
                discovery_status=row["discovery_status"],
                permission_status=row["permission_status"],
                inactivity_state=row["inactivity_state"],
                enabled=bool(row["enabled"]),
                last_observed_change=row["last_observed_change"],
            )
            for row in rows
        )

    def set_enabled(self, project_id: UUID, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        now = _utc_now()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                update projects set enabled = ?, updated_at = ?
                where project_id = ?
                """,
                (int(enabled), now, str(project_id).lower()),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
            if enabled:
                connection.execute(
                    """
                    insert into app_state(name, value_json, updated_at)
                    values ('reconcile_catchup_required', '{"required":true}', ?)
                    on conflict(name) do update set
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (now,),
                )

    def advance_last_observed_change(
        self,
        project_id: UUID,
        observed_at: datetime,
        *,
        as_of: datetime | None = None,
    ) -> bool:
        selected_now = datetime.now(timezone.utc) if as_of is None else as_of
        with self._database.transaction() as connection:
            return self._advance_last_observed_change_on_connection(
                connection,
                project_id,
                observed_at,
                as_of=selected_now,
            )

    @staticmethod
    def _advance_last_observed_change_on_connection(
        connection: sqlite3.Connection,
        project_id: UUID,
        observed_at: datetime,
        *,
        as_of: datetime,
    ) -> bool:
        try:
            selected_now = _strict_utc(as_of)
            candidate = _strict_utc(observed_at)
        except (TypeError, ValueError):
            return False
        if candidate > selected_now:
            return False

        project_id_text = str(project_id).lower()
        row = connection.execute(
            """
            select last_observed_change, inactivity_state
            from projects where project_id = ?
            """,
            (project_id_text,),
        ).fetchone()
        if row is None:
            raise KeyError(project_id)

        current_text = row["last_observed_change"]
        current = None
        if current_text is not None:
            try:
                current = _parse_strict_utc(current_text)
            except (TypeError, ValueError):
                return False
            if current > selected_now:
                return False
        if current is not None:
            if candidate < current:
                return False
            if candidate == current and row["inactivity_state"] == "active":
                return False

        cursor = connection.execute(
            """
            update projects
            set last_observed_change = ?, inactivity_state = 'active', updated_at = ?
            where project_id = ?
            """,
            (
                _utc_iso(candidate),
                _utc_iso(selected_now),
                project_id_text,
            ),
        )
        return bool(cursor.rowcount == 1)

    def relink(self, project_id: UUID, new_path: Path) -> ProjectRecord:
        canonical_path, destination_identity = _validated_relink_destination(new_path)
        physical_identity = destination_identity[-1]
        project_id_text = str(project_id).lower()
        now = _utc_now()
        with self._database.transaction() as connection:
            existing = connection.execute(
                "select project_id from projects where project_id = ?",
                (project_id_text,),
            ).fetchone()
            if existing is None:
                raise KeyError(project_id)
            owner = _project_owner(
                connection,
                canonical_path,
                physical_identity,
                self._legacy_project_ids(connection, physical_identity),
            )
            if owner is not None and owner["project_id"] != project_id_text:
                raise ValueError(
                    f"destination is already assigned to project {owner['project_id']}"
                )
            current = _select_record_with_identity(connection, project_id_text)
            current_identity = _stored_path_identity(current)
            current_path = Path(current["canonical_path"])
            preserve_current_path = (
                current_path == canonical_path
                and current_identity == physical_identity
                and complete_directory_identity(current_path) == physical_identity
            )
            if preserve_current_path:
                record = _to_record(current)
            else:
                connection.execute(
                    """
                    update projects
                    set canonical_path = ?, updated_at = ?, path_device = ?, path_inode = ?
                    where project_id = ?
                    """,
                    (
                        str(canonical_path),
                        now,
                        physical_identity[0],
                        physical_identity[1],
                        project_id_text,
                    ),
                )
                row = _select_record(connection, project_id_text)
                record = _to_record(row)
            if _path_identity(canonical_path) != destination_identity:
                raise ValueError("relink destination is unsafe or changed")
            if complete_directory_identity(record.canonical_path) != physical_identity:
                raise ValueError("relink destination is unsafe or changed")
        return record

    def _legacy_project_ids(
        self,
        connection: sqlite3.Connection,
        path_identity: PathIdentity,
    ) -> tuple[str, ...]:
        if not self._legacy_batch.active:
            return _legacy_identity_cache(connection).get(path_identity, ())
        cache = self._legacy_batch.cache
        if cache is None:
            cache = _legacy_identity_cache(connection)
            self._legacy_batch.cache = cache
        return cache.get(path_identity, ())


def _existing_directory(path: Path) -> Path:
    canonical_path = Path(path).resolve(strict=True)
    if not canonical_path.is_dir():
        raise NotADirectoryError(canonical_path)
    return canonical_path


def _validated_relink_destination(path: Path) -> tuple[Path, PathIdentitySnapshot]:
    selected = Path(path)
    selected_text = str(selected)
    if not selected.is_absolute() or os.path.normpath(selected_text) != selected_text:
        raise ValueError("relink destination is unsafe or changed")

    destination_identity = _path_identity(selected)
    if destination_identity is None:
        try:
            metadata = selected.lstat()
        except FileNotFoundError:
            raise
        if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            raise NotADirectoryError(selected)
        raise ValueError("relink destination is unsafe or changed")
    if len(destination_identity) != len(selected.parts):
        _existing_directory(selected)
        raise ValueError("relink destination is unsafe or changed")

    canonical_path = _existing_directory(selected)
    if canonical_path != selected:
        raise ValueError("relink destination is unsafe or changed")
    return canonical_path, destination_identity


def _project_owner(
    connection: sqlite3.Connection,
    canonical_path: Path,
    path_identity: PathIdentity,
    legacy_project_ids: tuple[str, ...],
) -> sqlite3.Row | None:
    rows = connection.execute(
        """
        select project_id, canonical_path, path_device, path_inode
        from projects
        where canonical_path = ?
           or (path_device = ? and path_inode = ?)
           or path_inode = ?
        order by case when path_device = ? and path_inode = ? then 0 else 1 end,
                 project_id
        """,
        (
            str(canonical_path),
            path_identity[0],
            path_identity[1],
            path_identity[1],
            path_identity[0],
            path_identity[1],
        ),
    ).fetchall()
    matches = {}
    for row in rows:
        stored_identity = _stored_path_identity(row)
        if row["canonical_path"] == str(canonical_path) or stored_identity == path_identity:
            matches[row["project_id"]] = row
            continue
        if complete_directory_identity(Path(row["canonical_path"])) == path_identity:
            matches[row["project_id"]] = row
    for project_id in legacy_project_ids:
        row = connection.execute(
            """
            select project_id, canonical_path, path_device, path_inode
            from projects
            where project_id = ?
              and (
                  path_device is null or path_inode is null
                  or typeof(path_device) <> 'integer'
                  or typeof(path_inode) <> 'integer'
                  or path_device < 0 or path_inode < 0
              )
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            continue
        if complete_directory_identity(Path(row["canonical_path"])) == path_identity:
            matches[row["project_id"]] = row
    if len(matches) > 1:
        raise ValueError("multiple projects share the same physical path")
    return next(iter(matches.values())) if matches else None


def _legacy_identity_cache(
    connection: sqlite3.Connection,
) -> dict[PathIdentity, tuple[str, ...]]:
    rows = connection.execute(
        """
        select project_id, canonical_path
        from projects
        where path_device is null or path_inode is null
           or typeof(path_device) <> 'integer'
           or typeof(path_inode) <> 'integer'
           or path_device < 0 or path_inode < 0
        order by project_id
        """
    ).fetchall()
    grouped: dict[PathIdentity, list[str]] = {}
    for row in rows:
        identity = complete_directory_identity(Path(row["canonical_path"]))
        if identity is not None:
            grouped.setdefault(identity, []).append(row["project_id"])
    return {identity: tuple(project_ids) for identity, project_ids in grouped.items()}


def _stored_path_identity(row: sqlite3.Row) -> PathIdentity | None:
    return stored_path_identity(row["path_device"], row["path_inode"])


def _select_record_with_identity(
    connection: sqlite3.Connection,
    project_id: str,
) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        """
        select project_id, canonical_path, display_name,
               discovery_status, permission_status, last_observed_change,
               path_device, path_inode
        from projects
        where project_id = ?
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise KeyError(project_id)
    return row


def _is_component_prefix(project_path: Path, cwd: Path) -> bool:
    return project_path == cwd or project_path in cwd.parents


def _select_record(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        """
        select project_id, canonical_path, display_name,
               discovery_status, permission_status, last_observed_change
        from projects
        where project_id = ?
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise KeyError(project_id)
    return row


def _to_record(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord.model_validate(
        {
            "project_id": row["project_id"],
            "canonical_path": row["canonical_path"],
            "display_name": row["display_name"],
            "discovery_status": row["discovery_status"],
            "permission_status": row["permission_status"],
            "last_observed_change": row["last_observed_change"],
        }
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_strict_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _strict_utc(parsed)


def _utc_iso(value: datetime) -> str:
    return _strict_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
