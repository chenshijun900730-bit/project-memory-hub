from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import platformdirs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_IDENTITY_SCRIPT = (
    'import json,sys;print(json.dumps({"major":sys.version_info.major,'
    '"minor":sys.version_info.minor},sort_keys=True))'
)
_MAX_HTTP_BODY_BYTES = 512 * 1024


class ArtifactSmokeError(RuntimeError):
    """Raised when an installed release artifact fails its isolated smoke test."""


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return ((".", "missing"),)
    if stat.S_ISLNK(root_metadata.st_mode):
        return ((".", "symlink", os.readlink(root)),)
    if not stat.S_ISDIR(root_metadata.st_mode):
        return ((".", "other", stat.S_IFMT(root_metadata.st_mode)),)

    entries: list[tuple[object, ...]] = [
        (".", "directory", stat.S_IMODE(root_metadata.st_mode), root_metadata.st_mtime_ns)
    ]

    def visit(directory: Path, relative: Path) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            child_relative = relative / child.name
            name = child_relative.as_posix()
            metadata = child.stat(follow_symlinks=False)
            mode = metadata.st_mode
            if stat.S_ISDIR(mode):
                entries.append((name, "directory", stat.S_IMODE(mode), metadata.st_mtime_ns))
                visit(Path(child.path), child_relative)
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with open(child.path, "rb") as file:
                    for block in iter(lambda: file.read(1024 * 1024), b""):
                        digest.update(block)
                entries.append(
                    (
                        name,
                        "file",
                        stat.S_IMODE(mode),
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        digest.hexdigest(),
                    )
                )
            elif stat.S_ISLNK(mode):
                entries.append((name, "symlink", os.readlink(child.path)))
            else:
                entries.append((name, "other", stat.S_IFMT(mode)))

    visit(root, Path())
    return tuple(entries)


def _isolated_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    data = root / "data"
    cache = root / "cache"
    temporary = root / "tmp"
    uv_cache = root / "uv-cache"
    for directory in (home, config, data, cache, temporary, uv_cache):
        directory.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PYTHONUTF8": "1",
        "UV_CACHE_DIR": str(uv_cache),
        "UV_NO_CONFIG": "1",
        "NO_COLOR": "1",
    }


def _run_checked(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ArtifactSmokeError(f"artifact smoke command failed: {command[1:2]}") from error
    if result.returncode not in allowed_returncodes:
        raise ArtifactSmokeError(
            f"artifact smoke command failed: {command[1:2]}: {result.stderr.strip()}"
        )
    return result


def _json_object(output: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ArtifactSmokeError(f"artifact smoke {label} output invalid") from error
    if type(value) is not dict:
        raise ArtifactSmokeError(f"artifact smoke {label} output invalid")
    return value


def _validate_python_version_result(
    result: subprocess.CompletedProcess[str],
    expected_version: str,
) -> None:
    try:
        major_text, minor_text = expected_version.split(".", 1)
        expected = (int(major_text), int(minor_text))
        identity = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError, UnicodeError) as error:
        raise ArtifactSmokeError("venv Python version mismatch") from error
    if (
        result.returncode != 0
        or type(identity) is not dict
        or type(identity.get("major")) is not int
        or type(identity.get("minor")) is not int
        or (identity["major"], identity["minor"]) != expected
    ):
        raise ArtifactSmokeError("venv Python version mismatch")


def _validate_doctor_result(result: subprocess.CompletedProcess[str]) -> None:
    doctor = _json_object(result.stdout, "doctor")
    checks = doctor.get("checks")
    if (
        result.returncode != 0
        or doctor.get("status") not in {"pass", "warn"}
        or type(checks) is not list
        or not checks
        or any(
            type(check) is not dict or check.get("status") not in {"pass", "warn"}
            for check in checks
        )
    ):
        raise ArtifactSmokeError("artifact smoke doctor output invalid")


def _validate_setup_result(result: subprocess.CompletedProcess[str]) -> None:
    setup = _json_object(result.stdout, "setup")
    if (
        result.returncode != 0
        or setup.get("status") != "ok"
        or setup.get("setup_status") != "inspected"
        or setup.get("setup_completed") is not False
    ):
        raise ArtifactSmokeError("artifact smoke setup output invalid")


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _LoopbackNotReady(RuntimeError):
    pass


def _load_access_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise _LoopbackNotReady from None
    except OSError as error:
        raise ArtifactSmokeError("artifact loopback access token rejected") from error
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size != 43:
            raise _LoopbackNotReady
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ArtifactSmokeError("artifact loopback access token rejected")
        data = os.read(descriptor, 44)
    except OSError as error:
        raise ArtifactSmokeError("artifact loopback access token rejected") from error
    finally:
        os.close(descriptor)
    try:
        token = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise ArtifactSmokeError("artifact loopback access token rejected") from error
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise ArtifactSmokeError("artifact loopback access token rejected")
    return token


def _loopback_request(
    port: int,
    path: str,
    *,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str, str, str, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type") or ""
        location = response.getheader("Location") or ""
        set_cookie = response.getheader("Set-Cookie") or ""
        body = response.read(_MAX_HTTP_BODY_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise _LoopbackNotReady from error
    finally:
        connection.close()
    if len(body) > _MAX_HTTP_BODY_BYTES:
        raise ArtifactSmokeError("artifact loopback serve identity mismatch")
    return (response.status, content_type, location, set_cookie, body)


def _request_loopback_identity(
    port: int,
    access_token_path: Path,
    *,
    timeout: float,
) -> None:
    status, _content_type, _location, set_cookie, _body = _loopback_request(
        port,
        "/",
        headers={"Accept": "text/html", "Connection": "close"},
        timeout=timeout,
    )
    if status != 401 or set_cookie:
        raise ArtifactSmokeError("artifact loopback serve identity mismatch")
    token = _load_access_token(access_token_path)
    replacement = "A" if token[0] != "A" else "B"
    invalid_token = replacement + token[1:]
    status, _content_type, _location, set_cookie, _body = _loopback_request(
        port,
        "/",
        headers={
            "Accept": "text/html",
            "Connection": "close",
            "Cookie": f"pmh_session={invalid_token}",
        },
        timeout=timeout,
    )
    if status not in {401, 403} or set_cookie:
        raise ArtifactSmokeError("artifact loopback serve identity mismatch")
    status, _content_type, _location, set_cookie, _body = _loopback_request(
        port,
        f"/?{urlencode({'token': invalid_token})}",
        headers={"Accept": "text/html", "Connection": "close"},
        timeout=timeout,
    )
    if status not in {401, 403} or set_cookie:
        raise ArtifactSmokeError("artifact loopback serve identity mismatch")
    status, _content_type, location, set_cookie, _body = _loopback_request(
        port,
        f"/?{urlencode({'token': token})}",
        headers={"Accept": "text/html", "Connection": "close"},
        timeout=timeout,
    )
    cookie = set_cookie.split(";", 1)[0].strip()
    if (
        status != 303
        or location != "/"
        or re.fullmatch(r"pmh_session=[A-Za-z0-9_-]{43}", cookie) is None
    ):
        raise ArtifactSmokeError("artifact loopback serve identity mismatch")
    status, content_type, _location, _set_cookie, body = _loopback_request(
        port,
        "/",
        headers={
            "Accept": "text/html",
            "Connection": "close",
            "Cookie": cookie,
        },
        timeout=timeout,
    )
    if (
        status != 200
        or not content_type.lower().startswith("text/html")
        or b"Project Memory Hub" not in body
    ):
        raise ArtifactSmokeError("artifact loopback serve identity mismatch")
    status, content_type, _location, _set_cookie, body = _loopback_request(
        port,
        "/setup",
        headers={
            "Accept": "text/html",
            "Connection": "close",
            "Cookie": cookie,
        },
        timeout=timeout,
    )
    if (
        status != 200
        or not content_type.lower().startswith("text/html")
        or b"data-setup" not in body
        or b'action="/setup/configure"' not in body
    ):
        raise ArtifactSmokeError("artifact loopback setup route mismatch")


def _signal_process_group(
    process: subprocess.Popen[bytes], selected_signal: signal.Signals
) -> None:
    try:
        os.killpg(process.pid, selected_signal)
    except ProcessLookupError:
        return
    except OSError as error:
        raise ArtifactSmokeError("artifact loopback process group cleanup failed") from error


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError as error:
        raise ArtifactSmokeError("artifact loopback process group cleanup failed") from error
    return True


def _wait_for_process_group_exit(process_group_id: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group_id):
        if time.monotonic() >= deadline:
            raise ArtifactSmokeError("artifact loopback process group cleanup failed")
        time.sleep(0.05)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    _signal_process_group(process, signal.SIGTERM)
    leader_exited = False
    try:
        process.wait(timeout=5.0)
        leader_exited = True
    except subprocess.TimeoutExpired:
        pass
    if not leader_exited or _process_group_exists(process.pid):
        _signal_process_group(process, signal.SIGKILL)
        if not leader_exited:
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired as error:
                raise ArtifactSmokeError("artifact loopback serve did not exit") from error
        _wait_for_process_group_exit(process.pid, timeout=5.0)


def _smoke_loopback_serve(
    executable: Path,
    *,
    env: dict[str, str],
    cwd: Path,
    config_path: Path,
    access_token_path: Path,
    startup_timeout: float = 10.0,
) -> None:
    port = _reserve_loopback_port()
    command = [
        str(executable),
        "--config",
        str(config_path),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise ArtifactSmokeError("artifact loopback serve failed to start") from error
    try:
        deadline = time.monotonic() + startup_timeout
        while True:
            if process.poll() is not None:
                raise ArtifactSmokeError("artifact loopback serve exited before startup")
            try:
                _request_loopback_identity(port, access_token_path, timeout=0.2)
                break
            except _LoopbackNotReady:
                if time.monotonic() >= deadline:
                    raise ArtifactSmokeError("artifact loopback serve startup timed out") from None
                time.sleep(0.05)
    finally:
        _terminate_process_group(process)


def _validate_executable(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ArtifactSmokeError(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ArtifactSmokeError(f"{label} is unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ArtifactSmokeError(f"{label} is unavailable")
    return resolved


def _reject_runtime_symlink(path: Path) -> None:
    selected = path.absolute()
    current = Path(selected.anchor)
    for part in selected.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ArtifactSmokeError("runtime root is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            if current == selected:
                raise ArtifactSmokeError("runtime root must not be a symlink")
            raise ArtifactSmokeError("runtime root must not use symlinks")
        if current != selected and not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactSmokeError("runtime root is unavailable")


def _copy_bound_wheel(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_identity: tuple[int, int],
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ArtifactSmokeError("expected wheel digest is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, flags)
    except OSError as error:
        raise ArtifactSmokeError("wheel is unavailable") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != expected_identity:
            raise ArtifactSmokeError("wheel changed before artifact smoke")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(source_descriptor, "rb", closefd=False) as source_file:
            with destination.open("xb") as destination_file:
                for block in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(block)
                    destination_file.write(block)
        after = os.fstat(source_descriptor)
    except ArtifactSmokeError:
        raise
    except OSError as error:
        raise ArtifactSmokeError("wheel staging failed") from error
    finally:
        os.close(source_descriptor)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) or digest.hexdigest() != expected_sha256:
        raise ArtifactSmokeError("wheel changed during artifact smoke")
    try:
        current = source.lstat()
    except OSError as error:
        raise ArtifactSmokeError("wheel changed during artifact smoke") from error
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != expected_identity:
        raise ArtifactSmokeError("wheel changed during artifact smoke")


def _assert_bound_wheel(
    source: Path,
    *,
    expected_sha256: str,
    expected_identity: tuple[int, int],
) -> None:
    with tempfile.TemporaryDirectory(prefix="pmh-wheel-recheck-") as temporary_name:
        destination = Path(temporary_name) / source.name
        _copy_bound_wheel(
            source,
            destination,
            expected_sha256=expected_sha256,
            expected_identity=expected_identity,
        )


def smoke_install_artifact(
    wheel: Path,
    system_python: Path,
    *,
    expected_version: str,
    expected_python_version: str,
    expected_wheel_sha256: str,
    expected_wheel_identity: tuple[int, int],
    repository_root: Path = PROJECT_ROOT,
    default_runtime_root: Path | None = None,
    temporary_parent: Path | None = None,
    uv_executable: str = "uv",
    command_timeout: float = 300.0,
    startup_timeout: float = 10.0,
) -> None:
    """Install a wheel into a new venv and smoke it without touching host state."""

    repository = Path(repository_root).resolve(strict=True)
    selected_wheel = Path(wheel)
    if not selected_wheel.is_absolute():
        selected_wheel = selected_wheel.absolute()
    try:
        wheel_metadata = selected_wheel.lstat()
        selected_wheel = selected_wheel.resolve(strict=True)
    except OSError as error:
        raise ArtifactSmokeError("wheel is unavailable") from error
    if (
        stat.S_ISLNK(wheel_metadata.st_mode)
        or not selected_wheel.is_file()
        or selected_wheel.suffix != ".whl"
    ):
        raise ArtifactSmokeError("wheel is unavailable")
    selected_python_input = Path(system_python)
    selected_python = _validate_executable(selected_python_input, "system interpreter")
    if _is_within(selected_python, repository):
        raise ArtifactSmokeError("system interpreter must be outside repository")
    if not expected_version.strip():
        raise ArtifactSmokeError("expected version is invalid")
    try:
        major, minor = expected_python_version.split(".", 1)
        if int(major) != 3 or int(minor) not in {11, 12}:
            raise ValueError
    except ValueError as error:
        raise ArtifactSmokeError("expected Python version is invalid") from error
    uv = _validate_executable(Path(uv_executable), "uv executable")

    default_runtime = (
        Path(default_runtime_root).absolute()
        if default_runtime_root is not None
        else Path(platformdirs.user_data_path("Project Memory Hub", appauthor=False)).absolute()
    )
    _reject_runtime_symlink(default_runtime)
    repository_before = _tree_snapshot(repository)
    runtime_before = _tree_snapshot(default_runtime)
    failure: BaseException | None = None

    try:
        with tempfile.TemporaryDirectory(
            prefix="pmh-artifact-smoke-",
            dir=temporary_parent,
        ) as temporary_name:
            root = Path(temporary_name).resolve(strict=True)
            if _is_within(root, repository) or _is_within(root, default_runtime):
                raise ArtifactSmokeError("artifact smoke temporary path is not isolated")
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            environment = _isolated_environment(root)
            venv = root / "venv"
            runtime_config = root / "runtime" / "config.toml"
            access_token = runtime_config.parent / "access-token"
            staged_wheel = root / "artifact" / selected_wheel.name
            _copy_bound_wheel(
                selected_wheel,
                staged_wheel,
                expected_sha256=expected_wheel_sha256,
                expected_identity=expected_wheel_identity,
            )

            _run_checked(
                [
                    str(uv),
                    "venv",
                    "--python",
                    str(selected_python),
                    str(venv),
                ],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            venv_python = venv / "bin" / "python"
            executable = venv / "bin" / "memory-hub"
            if not venv_python.is_absolute() or not venv_python.exists():
                raise ArtifactSmokeError("new venv Python is unavailable")
            python_version_result = _run_checked(
                [str(venv_python), "-I", "-c", _PYTHON_IDENTITY_SCRIPT],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            _validate_python_version_result(
                python_version_result,
                expected_python_version,
            )

            _run_checked(
                [
                    str(uv),
                    "pip",
                    "install",
                    "--python",
                    str(venv_python),
                    str(staged_wheel),
                ],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            if not executable.is_absolute() or not executable.exists():
                raise ArtifactSmokeError("installed memory-hub executable is unavailable")

            help_result = _run_checked(
                [str(executable), "--help"],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            if not help_result.stdout.strip():
                raise ArtifactSmokeError("artifact smoke help output invalid")
            version_result = _run_checked(
                [str(executable), "version"],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            if version_result.stdout.strip() != expected_version:
                raise ArtifactSmokeError("artifact smoke version mismatch")
            init_result = _run_checked(
                [
                    str(executable),
                    "--config",
                    str(runtime_config),
                    "init",
                    "--format",
                    "json",
                ],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            if _json_object(init_result.stdout, "init").get("status") != "initialized":
                raise ArtifactSmokeError("artifact smoke init output invalid")
            setup_result = _run_checked(
                [
                    str(executable),
                    "--config",
                    str(runtime_config),
                    "setup",
                    "--format",
                    "json",
                ],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            _validate_setup_result(setup_result)
            doctor_result = _run_checked(
                [
                    str(executable),
                    "--config",
                    str(runtime_config),
                    "doctor",
                    "--format",
                    "json",
                ],
                env=environment,
                cwd=workspace,
                timeout=command_timeout,
            )
            _validate_doctor_result(doctor_result)

            _smoke_loopback_serve(
                executable,
                env=environment,
                cwd=workspace,
                config_path=runtime_config,
                access_token_path=access_token,
                startup_timeout=startup_timeout,
            )
    except BaseException as error:
        failure = error

    repository_after = _tree_snapshot(repository)
    runtime_after = _tree_snapshot(default_runtime)
    wheel_failure: ArtifactSmokeError | None = None
    try:
        _assert_bound_wheel(
            selected_wheel,
            expected_sha256=expected_wheel_sha256,
            expected_identity=expected_wheel_identity,
        )
    except ArtifactSmokeError as error:
        wheel_failure = error
    if repository_after != repository_before:
        raise ArtifactSmokeError("repository changed during artifact smoke") from failure
    if runtime_after != runtime_before:
        raise ArtifactSmokeError("runtime changed during artifact smoke") from failure
    if wheel_failure is not None:
        raise wheel_failure from failure
    if failure is not None:
        if isinstance(failure, ArtifactSmokeError):
            raise failure
        raise ArtifactSmokeError("artifact smoke failed") from failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke a clean wheel installation")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", dest="system_python", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-python-version", required=True)
    parser.add_argument("--repository-root", type=Path, default=PROJECT_ROOT)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is unavailable")
    try:
        selected_wheel = Path(arguments.wheel).absolute()
        selected_metadata = selected_wheel.lstat()
        if stat.S_ISLNK(selected_metadata.st_mode) or not stat.S_ISREG(selected_metadata.st_mode):
            raise OSError
        wheel = selected_wheel.resolve(strict=True)
        wheel_metadata = wheel.stat()
        digest = hashlib.sha256()
        with wheel.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        wheel_digest = digest.hexdigest()
    except OSError as error:
        raise SystemExit("wheel is unavailable") from error
    try:
        smoke_install_artifact(
            wheel,
            arguments.system_python,
            expected_version=arguments.expected_version,
            expected_python_version=arguments.expected_python_version,
            expected_wheel_sha256=wheel_digest,
            expected_wheel_identity=(wheel_metadata.st_dev, wheel_metadata.st_ino),
            repository_root=arguments.repository_root,
            uv_executable=str(Path(uv).resolve()),
        )
    except ArtifactSmokeError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
