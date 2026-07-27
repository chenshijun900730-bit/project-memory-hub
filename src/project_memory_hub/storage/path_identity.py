import os
import sys
from pathlib import Path


PathIdentity = tuple[int, int]
PathIdentitySnapshot = tuple[PathIdentity, ...]

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def snapshot_path_identity(path: Path) -> PathIdentitySnapshot | None:
    """Snapshot existing lexical directory components without following symlinks."""
    absolute = Path(os.path.abspath(path))
    descriptor = -1
    identities: list[PathIdentity] = []
    try:
        descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
        metadata = os.fstat(descriptor)
        identities.append((int(metadata.st_dev), int(metadata.st_ino)))
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                break
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            identities.append((int(metadata.st_dev), int(metadata.st_ino)))
        return tuple(identities)
    except (OSError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def complete_directory_identity(path: Path) -> PathIdentity | None:
    absolute = Path(os.path.abspath(path))
    snapshot = snapshot_path_identity(absolute)
    if snapshot is None or len(snapshot) != len(absolute.parts):
        return None
    return snapshot[-1]


def stored_path_identity(device: object, inode: object) -> PathIdentity | None:
    if type(device) is not int or type(inode) is not int or device < 0 or inode < 0:
        return None
    return (device, inode)


def persisted_identity_matches_at_same_path(
    stored: PathIdentity,
    live: PathIdentity,
) -> bool:
    """Compare a persisted directory identity at one proven canonical path.

    APFS device numbers can be renumbered across macOS boots while the directory
    inode remains stable. The relaxed branch is deliberately macOS-only and must
    only be used after the caller has proven that the lexical canonical path is
    unchanged. In-process snapshots continue to require the exact full tuple.
    """
    return stored == live or (sys.platform == "darwin" and stored[1] == live[1])


def validated_persisted_directory_identity(
    path: Path,
    device: object,
    inode: object,
) -> PathIdentity | None:
    """Return the live identity when a persisted same-path record is trusted."""
    stored = stored_path_identity(device, inode)
    live = complete_directory_identity(path)
    if stored is None or live is None or not persisted_identity_matches_at_same_path(stored, live):
        return None
    return live


def path_identity_is_current(path: Path, device: object, inode: object) -> bool:
    return validated_persisted_directory_identity(path, device, inode) is not None
