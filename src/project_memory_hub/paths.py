import os
import stat
from dataclasses import dataclass
from pathlib import Path

import platformdirs


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    database: Path
    imports: Path
    retries: Path
    backups: Path
    logs: Path
    access_token: Path

    @classmethod
    def for_root(cls, root: Path | None = None) -> "RuntimePaths":
        selected_root = (
            platformdirs.user_data_path("Project Memory Hub", appauthor=False)
            if root is None
            else Path(root)
        )
        return cls(
            root=selected_root,
            database=selected_root / "memory.db",
            imports=selected_root / "imports",
            retries=selected_root / "retries",
            backups=selected_root / "backups",
            logs=selected_root / "logs",
            access_token=selected_root / "access-token",
        )

    def ensure(self) -> None:
        for directory in (
            self.root,
            self.imports,
            self.retries,
            self.backups,
            self.logs,
        ):
            _ensure_private_directory(directory)


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise PermissionError("runtime directory must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(path)

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise NotADirectoryError(path)
        current_mode = stat.S_IMODE(opened.st_mode)
        if current_mode & 0o700 != 0o700:
            raise PermissionError("runtime directory owner access is insufficient")
        if current_mode != 0o700:
            os.fchmod(descriptor, current_mode & 0o700)
    finally:
        os.close(descriptor)
