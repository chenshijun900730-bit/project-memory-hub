from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path, PurePosixPath

import pytest

from project_memory_hub.benchmark_fixtures import (
    build_fixture_repositories,
    load_task_specs,
)
from project_memory_hub.benchmark_runner import (
    ExecutionConfig,
    _codex_sandbox_profile,
    _run_validator,
    _validator_command,
    _validator_response_matches,
    _validator_sandbox_profile,
    _workspace_snapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"


def _compile_macos_helper(path: Path, source: str) -> Path:
    compiler = subprocess.run(
        ["/usr/bin/xcrun", "--find", "clang"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    clang = Path(compiler.stdout.strip())
    if compiler.returncode != 0 or not clang.is_file():
        pytest.skip("the macOS C compiler is unavailable")
    sdk = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    sdk_path = Path(sdk.stdout.strip())
    if sdk.returncode != 0 or not sdk_path.is_dir():
        pytest.skip("the macOS SDK is unavailable")
    source_path = path.with_suffix(".c")
    source_path.write_text(textwrap.dedent(source), encoding="utf-8")
    compiled = subprocess.run(
        [
            clang,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-isysroot",
            sdk_path,
            source_path,
            "-o",
            path,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    path.chmod(0o700)
    return path


def _macos_python_subject() -> tuple[Path, Path]:
    resolved = subprocess.run(
        ["/usr/bin/xcrun", "--find", "python3"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    executable = Path(resolved.stdout.strip()).resolve()
    code = (
        "import ctypes,os\n"
        "buffer=ctypes.create_string_buffer(4096)\n"
        "libproc=ctypes.CDLL('/usr/lib/libproc.dylib')\n"
        "assert libproc.proc_pidpath(os.getpid(),buffer,len(buffer)) > 0\n"
        "print(buffer.value.decode())\n"
    )
    subject = subprocess.run(
        [executable, "-c", code],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return executable, Path(subject.stdout.strip())


@pytest.mark.macos_only
def test_validator_seatbelt_blocks_protected_reads_and_network(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    _, python = _macos_python_subject()
    if not sandbox.is_file() or not python.is_file():
        pytest.skip("required macOS executables are unavailable")
    workspace = tmp_path / "workspace"
    validator_tmp = tmp_path / "validator-tmp"
    protected_home = tmp_path / "protected-home"
    workspace.mkdir()
    validator_tmp.mkdir()
    protected_home.mkdir()
    workspace_source = workspace / "source.py"
    workspace_source.write_text("VALUE = 1\n", encoding="utf-8")
    secret = protected_home / "private-canary"
    secret.write_text("not-readable", encoding="utf-8")
    profile = _validator_sandbox_profile(
        workspace,
        validator_tmp,
        python,
        sandbox,
        protected_home,
    )
    code = (
        "import errno,os,pathlib,socket,subprocess,tempfile\n"
        "source=pathlib.Path('source.py')\n"
        "assert source.read_text() == 'VALUE = 1\\n'\n"
        "try:\n"
        " source.write_text('bad')\n"
        " raise SystemExit(80)\n"
        "except PermissionError:\n"
        " pass\n"
        "with tempfile.TemporaryDirectory() as first:\n"
        " with tempfile.TemporaryDirectory() as second:\n"
        "  pathlib.Path(first,'linked').symlink_to(second, target_is_directory=True)\n"
        "try:\n"
        " subprocess.run(['/bin/true'], check=False)\n"
        " raise SystemExit(81)\n"
        "except PermissionError:\n"
        " pass\n"
        "try:\n"
        " os.fork()\n"
        " raise SystemExit(82)\n"
        "except PermissionError:\n"
        " pass\n"
        f"secret=pathlib.Path({os.fspath(secret)!r})\n"
        "try:\n"
        " secret.read_text()\n"
        " raise SystemExit(83)\n"
        "except PermissionError:\n"
        " pass\n"
        "connection=socket.socket()\n"
        "connection.settimeout(0.2)\n"
        "try:\n"
        " connection.connect(('1.1.1.1',80))\n"
        " raise SystemExit(84)\n"
        "except OSError as error:\n"
        " assert error.errno in {errno.EPERM,errno.EACCES}\n"
        "print('isolated')\n"
    )
    completed = subprocess.run(
        [sandbox, "-p", profile, python, "-c", code],
        cwd=workspace,
        env={
            "HOME": os.fspath(validator_tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": os.fspath(validator_tmp),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode == 71 and "Operation not permitted" in completed.stderr:
        pytest.skip("the parent process forbids nested Seatbelt profiles")

    assert completed.returncode == 0
    assert completed.stdout.strip() == "isolated"
    assert workspace_source.read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.macos_only
def test_real_validator_oracle_runs_t05_without_mutating_workspace(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    _, python = _macos_python_subject()
    if not sandbox.is_file() or not python.is_file():
        pytest.skip("required macOS executables are unavailable")
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    repository = next(item for item in repositories if item.fixture_id == "release-kit")
    task = next(item for item in load_task_specs(MANIFEST) if item.task_id == "T05")
    implementation = repository.path / "release_kit" / "paths.py"
    implementation.write_text(
        "from pathlib import Path\n\n"
        "def safe_join(root: Path, relative: str) -> Path:\n"
        "    resolved_root = root.resolve(strict=True)\n"
        "    candidate = root / relative\n"
        "    resolved_candidate = candidate.resolve(strict=False)\n"
        "    if resolved_candidate != resolved_root and resolved_root not in "
        "resolved_candidate.parents:\n"
        "        raise ValueError('outside root')\n"
        "    return candidate\n",
        encoding="utf-8",
    )
    protected = tmp_path / "protected" / ".codex"
    protected.mkdir(parents=True)
    auth = protected / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    run_root = tmp_path / "validator-run"
    run_root.mkdir()
    config = ExecutionConfig(
        codex_executable=python,
        codex_cli_version="canary",
        requested_model_id="gpt-5.6-sol",
        reasoning_effort="low",
        timeout_seconds=30,
        validator_sandbox_executable=sandbox,
        validator_python_executable=python,
        codex_auth_file=auth,
        manifest_sha256="a" * 64,
        harness_commit="b" * 40,
        harness_sha256="c" * 64,
    )
    before = _workspace_snapshot(repository.path)

    outcome = _run_validator(task, repository.path, run_root, config)

    if b'"sandbox_apply_denied":true' in outcome.output:
        pytest.skip("the parent process forbids nested Seatbelt profiles")
    assert outcome.return_code == 0
    assert outcome.protocol_valid is True
    assert outcome.tests_run == 2
    assert outcome.failures == outcome.errors == 0
    assert _workspace_snapshot(repository.path) == before


@pytest.mark.macos_only
def test_real_validator_challenge_rejects_import_time_case_stdout_forgery(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    _, python = _macos_python_subject()
    if not sandbox.is_file() or not python.is_file():
        pytest.skip("required macOS executables are unavailable")
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    task = load_task_specs(MANIFEST)[0]
    (repository.path / "ledger_tools" / "records.py").write_text(
        "import json, os, sys\n"
        "observations = {\n"
        "    'preserve-trailing-spaces': {'kind': 'return', 'value': 'alpha  '},\n"
        "    'remove-one-crlf': {'kind': 'return', 'value': 'beta'},\n"
        "}\n"
        "os.write(1, json.dumps(\n"
        "    observations[sys.argv[2]], sort_keys=True, separators=(',', ':'),\n"
        ").encode() + b'\\n')\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    protected = tmp_path / "protected" / ".codex"
    protected.mkdir(parents=True)
    auth = protected / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    validator_tmp = tmp_path / "validator-tmp"
    validator_tmp.mkdir()
    config = ExecutionConfig(
        codex_executable=python,
        codex_cli_version="canary",
        requested_model_id="gpt-5.6-sol",
        reasoning_effort="low",
        timeout_seconds=30,
        validator_sandbox_executable=sandbox,
        validator_python_executable=python,
        codex_auth_file=auth,
        manifest_sha256="a" * 64,
        harness_commit="b" * 40,
        harness_sha256="c" * 64,
    )
    command = _validator_command(
        task,
        "preserve-trailing-spaces",
        repository.path,
        validator_tmp,
        config,
    )

    completed = subprocess.run(
        command,
        cwd=repository.path,
        env={
            "HOME": os.fspath(validator_tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": os.fspath(validator_tmp),
            "TZ": "UTC",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode == 71 and b"Operation not permitted" in completed.stderr:
        pytest.skip("the parent process forbids nested Seatbelt profiles")

    assert completed.returncode == 0
    assert (
        _validator_response_matches(
            completed.stdout,
            {"kind": "return", "value": "alpha  "},
            set(),
        )
        is False
    )


@pytest.mark.macos_only
def test_codex_outer_seatbelt_denies_original_home(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        pytest.skip("required macOS executables are unavailable")
    private_codex = _compile_macos_helper(
        tmp_path / "private-codex",
        r"""
        #include <errno.h>
        #include <fcntl.h>
        #include <limits.h>
        #include <stdio.h>
        #include <string.h>
        #include <sys/file.h>
        #include <sys/stat.h>
        #include <unistd.h>

        int main(int argc, char **argv) {
            if (argc != 3) return 90;
            errno = 0;
            int descriptor = open(argv[1], O_RDONLY);
            if (descriptor >= 0) {
                close(descriptor);
                return 81;
            }
            if (errno != EACCES && errno != EPERM) return 82;
            char target[PATH_MAX];
            int length = snprintf(
                target,
                sizeof(target),
                "%s/installation_id",
                argv[2]
            );
            if (length <= 0 || (size_t)length >= sizeof(target)) return 83;
            descriptor = open(target, O_RDWR | O_CREAT | O_TRUNC, 0600);
            if (descriptor < 0) return 84;
            const char *value = "00000000-0000-0000-0000-000000000000";
            if (
                flock(descriptor, LOCK_EX) != 0
                || fchmod(descriptor, 0644) != 0
                || write(descriptor, value, strlen(value)) < 0
                || fsync(descriptor) != 0
                || close(descriptor) != 0
            ) return 85;
            descriptor = open(
                "workspace-canary",
                O_WRONLY | O_CREAT | O_TRUNC,
                0600
            );
            if (descriptor < 0) return 86;
            if (write(descriptor, "ok", 2) != 2 || close(descriptor) != 0) {
                return 87;
            }
            puts("isolated");
            return 0;
        }
        """,
    )
    protected_home = tmp_path / "protected-home"
    workspace = tmp_path / "workspace"
    protected_home.mkdir()
    workspace.mkdir()
    secret = protected_home / "private-canary"
    secret.write_text("not-readable", encoding="utf-8")
    process_home = tmp_path / "process-home"
    process_tmp = tmp_path / "process-tmp"
    process_home.mkdir()
    process_tmp.mkdir()
    workspace_canary = workspace / "workspace-canary"
    workspace_canary.write_text("before", encoding="utf-8")
    profile = _codex_sandbox_profile(
        workspace,
        process_home,
        process_tmp,
        private_codex,
        sandbox,
        protected_home,
        (PurePosixPath("workspace-canary"),),
    )
    completed = subprocess.run(
        [
            sandbox,
            "-p",
            profile,
            private_codex,
            secret,
            process_home,
        ],
        cwd=workspace,
        env={
            "HOME": os.fspath(process_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": os.fspath(process_tmp),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode == 71 and "Operation not permitted" in completed.stderr:
        pytest.skip("the parent process forbids nested Seatbelt profiles")

    assert completed.returncode == 0
    assert completed.stdout.strip() == "isolated"
    assert (workspace / "workspace-canary").read_text(encoding="utf-8") == "ok"


@pytest.mark.macos_only
def test_codex_outer_seatbelt_allows_only_exact_managed_requirements(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        pytest.skip("required macOS executables are unavailable")
    private_codex = _compile_macos_helper(
        tmp_path / "private-codex",
        r"""
        #include <errno.h>
        #include <fcntl.h>
        #include <unistd.h>

        static int readable_or_absent(const char *path) {
            errno = 0;
            int descriptor = open(path, O_RDONLY);
            if (descriptor >= 0) {
                return close(descriptor) == 0 ? 0 : 1;
            }
            return errno == ENOENT ? 0 : 2;
        }

        int main(void) {
            if (readable_or_absent("/etc/codex/requirements.toml") != 0) return 80;
            if (
                readable_or_absent("/private/etc/codex/requirements.toml") != 0
            ) return 81;
            errno = 0;
            int descriptor = open("/private/etc/passwd", O_RDONLY);
            if (descriptor >= 0) {
                close(descriptor);
                return 82;
            }
            if (errno != EACCES && errno != EPERM) return 83;
            return 0;
        }
        """,
    )
    workspace = tmp_path / "workspace"
    process_home = tmp_path / "process-home"
    process_tmp = tmp_path / "process-tmp"
    protected_home = tmp_path / "protected-home"
    for directory in (workspace, process_home, process_tmp, protected_home):
        directory.mkdir()
    allowed_file = workspace / "allowed.py"
    allowed_file.write_text("VALUE = 1\n", encoding="utf-8")
    profile = _codex_sandbox_profile(
        workspace,
        process_home,
        process_tmp,
        private_codex,
        sandbox,
        protected_home,
        (PurePosixPath("allowed.py"),),
    )

    completed = subprocess.run(
        [sandbox, "-p", profile, private_codex],
        cwd=workspace,
        env={
            "HOME": os.fspath(process_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": os.fspath(process_tmp),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode == 71 and "Operation not permitted" in completed.stderr:
        pytest.skip("the parent process forbids nested Seatbelt profiles")

    assert completed.returncode == 0, completed.stderr


@pytest.mark.macos_only
def test_codex_outer_seatbelt_allows_parent_dns_without_child_network_or_secret_read(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    zsh = Path("/bin/zsh")
    if not sandbox.is_file() or not zsh.is_file():
        pytest.skip("required macOS executables are unavailable")
    try:
        host_addresses = socket.getaddrinfo(
            "chatgpt.com",
            443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        pytest.skip("the host cannot currently resolve chatgpt.com")
    if not host_addresses:
        pytest.skip("the host returned no chatgpt.com addresses")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    host_child_probe = subprocess.run(
        [
            zsh,
            "-fc",
            f"zmodload zsh/net/tcp && ztcp 127.0.0.1 {port}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if host_child_probe.returncode != 0:
        listener.close()
        pytest.skip("the host zsh TCP canary is unavailable")
    accepted, _ = listener.accept()
    accepted.close()

    private_codex = _compile_macos_helper(
        tmp_path / "private-codex",
        r"""
        #include <errno.h>
        #include <fcntl.h>
        #include <netdb.h>
        #include <spawn.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <sys/wait.h>
        #include <unistd.h>

        extern char **environ;

        static int spawn_quiet(const char *path, char *const arguments[]) {
            posix_spawn_file_actions_t actions;
            if (posix_spawn_file_actions_init(&actions) != 0) return -200;
            if (
                posix_spawn_file_actions_addopen(
                    &actions,
                    STDOUT_FILENO,
                    "/dev/null",
                    O_WRONLY,
                    0
                ) != 0
                || posix_spawn_file_actions_addopen(
                    &actions,
                    STDERR_FILENO,
                    "/dev/null",
                    O_WRONLY,
                    0
                ) != 0
            ) return -201;
            pid_t child = 0;
            int result = posix_spawn(
                &child,
                path,
                &actions,
                NULL,
                arguments,
                environ
            );
            posix_spawn_file_actions_destroy(&actions);
            if (result != 0) return -result;
            int status = 0;
            if (waitpid(child, &status, 0) != child) return -202;
            if (!WIFEXITED(status)) return 255;
            return WEXITSTATUS(status);
        }

        int main(int argc, char **argv) {
            if (argc != 3) return 90;
            struct addrinfo hints = {0};
            struct addrinfo *addresses = NULL;
            hints.ai_family = AF_UNSPEC;
            hints.ai_socktype = SOCK_STREAM;
            int lookup = getaddrinfo(
                "chatgpt.com",
                "443",
                &hints,
                &addresses
            );
            if (lookup != 0) {
                fprintf(
                    stderr,
                    "getaddrinfo=%d:%s\n",
                    lookup,
                    gai_strerror(lookup)
                );
                return 80;
            }
            freeaddrinfo(addresses);

            errno = 0;
            int descriptor = open(argv[1], O_RDONLY);
            if (descriptor >= 0) {
                close(descriptor);
                return 81;
            }
            if (errno != EACCES && errno != EPERM) return 82;

            char *child_network[] = {
                "/bin/zsh",
                "-fc",
                "zmodload zsh/net/tcp || exit 90; ztcp chatgpt.com 443",
                NULL
            };
            int child_network_result = spawn_quiet(
                "/bin/zsh",
                child_network
            );
            if (child_network_result != 1) return 83;

            char child_connection_command[256];
            int command_length = snprintf(
                child_connection_command,
                sizeof(child_connection_command),
                "zmodload zsh/net/tcp || exit 90; "
                "ztcp 127.0.0.1 %s",
                argv[2]
            );
            if (
                command_length <= 0
                || (size_t)command_length
                    >= sizeof(child_connection_command)
            ) return 84;
            char *child_connection[] = {
                "/bin/zsh",
                "-fc",
                child_connection_command,
                NULL
            };
            int child_connection_result = spawn_quiet(
                "/bin/zsh",
                child_connection
            );
            if (child_connection_result != 1) return 85;

            char *child_secret[] = {
                "/bin/cat",
                argv[1],
                NULL
            };
            int child_secret_result = spawn_quiet("/bin/cat", child_secret);
            if (child_secret_result <= 0) return 86;
            puts("dns-isolated");
            return 0;
        }
        """,
    )
    workspace = tmp_path / "workspace"
    process_home = tmp_path / "process-home"
    process_tmp = tmp_path / "process-tmp"
    protected_home = tmp_path / "protected-home"
    for directory in (workspace, process_home, process_tmp, protected_home):
        directory.mkdir()
    secret = protected_home / "private-canary"
    secret.write_text("not-readable", encoding="utf-8")
    allowed_file = workspace / "allowed.py"
    allowed_file.write_text("VALUE = 1\n", encoding="utf-8")
    profile = _codex_sandbox_profile(
        workspace,
        process_home,
        process_tmp,
        private_codex,
        sandbox,
        protected_home,
        (PurePosixPath("allowed.py"),),
    )

    try:
        completed = subprocess.run(
            [
                sandbox,
                "-p",
                profile,
                private_codex,
                secret,
                str(port),
            ],
            cwd=workspace,
            env={
                "HOME": os.fspath(process_home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": os.fspath(process_tmp),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    finally:
        listener.close()
    if completed.returncode == 71 and "Operation not permitted" in completed.stderr:
        pytest.skip("the parent process forbids nested Seatbelt profiles")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "dns-isolated"


@pytest.mark.macos_only
def test_codex_outer_seatbelt_separates_parent_and_model_capabilities(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt is unavailable")
    sandbox = Path("/usr/bin/sandbox-exec")
    curl = Path("/usr/bin/curl")
    if not sandbox.is_file() or not curl.is_file():
        pytest.skip("required macOS executables are unavailable")
    private_codex = _compile_macos_helper(
        tmp_path / "private-codex",
        r"""
        #include <arpa/inet.h>
        #include <dirent.h>
        #include <errno.h>
        #include <fcntl.h>
        #include <netinet/in.h>
        #include <spawn.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>
        #include <sys/socket.h>
        #include <sys/stat.h>
        #include <sys/wait.h>
        #include <unistd.h>

        extern char **environ;

        static int spawn_quiet(const char *path, char *const arguments[]) {
            posix_spawn_file_actions_t actions;
            if (posix_spawn_file_actions_init(&actions) != 0) return -200;
            if (
                posix_spawn_file_actions_addopen(
                    &actions,
                    STDOUT_FILENO,
                    "/dev/null",
                    O_WRONLY,
                    0
                ) != 0
                || posix_spawn_file_actions_addopen(
                    &actions,
                    STDERR_FILENO,
                    "/dev/null",
                    O_WRONLY,
                    0
                ) != 0
            ) return -201;
            pid_t child = 0;
            int result = posix_spawn(
                &child,
                path,
                &actions,
                NULL,
                arguments,
                environ
            );
            posix_spawn_file_actions_destroy(&actions);
            if (result != 0) return -result;
            int status = 0;
            if (waitpid(child, &status, 0) != child) return -202;
            if (!WIFEXITED(status)) return 255;
            return WEXITSTATUS(status);
        }

        static int parent_connection(int port) {
            int connection = socket(AF_INET, SOCK_STREAM, 0);
            if (connection < 0) return 1;
            struct sockaddr_in address;
            memset(&address, 0, sizeof(address));
            address.sin_family = AF_INET;
            address.sin_port = htons((uint16_t)port);
            if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1) {
                close(connection);
                return 2;
            }
            if (
                connect(
                    connection,
                    (struct sockaddr *)&address,
                    sizeof(address)
                ) != 0
                || write(connection, "parent", 6) != 6
            ) {
                close(connection);
                return 3;
            }
            char response[3] = {0};
            ssize_t received = read(connection, response, 2);
            close(connection);
            return received == 2 && memcmp(response, "ok", 2) == 0 ? 0 : 4;
        }

        static int auth_is_readable(const char *path) {
            int descriptor = open(path, O_RDONLY);
            if (descriptor < 0) return 0;
            char value[12] = {0};
            ssize_t received = read(descriptor, value, 11);
            close(descriptor);
            return received == 11 && memcmp(value, "parent-only", 11) == 0;
        }

        int main(int argc, char **argv) {
            if (argc == 3 && strcmp(argv[1], "reentered") == 0) {
                int marker = open(
                    argv[2],
                    O_WRONLY | O_CREAT | O_TRUNC,
                    0600
                );
                if (marker >= 0) {
                    write(marker, "bad", 3);
                    close(marker);
                }
                return 0;
            }
            if (argc != 6) return 90;
            const char *auth = argv[1];
            const char *secret = argv[2];
            const char *protected_home = argv[3];
            int port = atoi(argv[4]);
            const char *reentry_marker = argv[5];
            if (!auth_is_readable(auth)) return 81;
            struct stat metadata;
            errno = 0;
            if (stat(secret, &metadata) == 0) return 82;
            if (errno != EACCES && errno != EPERM) return 83;
            errno = 0;
            DIR *directory = opendir(protected_home);
            if (directory != NULL) {
                closedir(directory);
                return 84;
            }
            if (errno != EACCES && errno != EPERM) return 85;
            if (parent_connection(port) != 0) return 86;

            char *private_read[] = {"/bin/cat", (char *)auth, NULL};
            if (spawn_quiet("/bin/cat", private_read) == 0) return 87;
            char *workspace_read[] = {
                "/bin/cat",
                "workspace-input",
                NULL
            };
            if (spawn_quiet("/bin/cat", workspace_read) != 0) return 88;
            char *workspace_write[] = {
                "/bin/zsh",
                "-fc",
                "printf child-ok > child-canary",
                NULL
            };
            if (spawn_quiet("/bin/zsh", workspace_write) != 0) return 89;
            char *sibling_write[] = {
                "/bin/zsh",
                "-fc",
                "printf bad > sibling-canary",
                NULL
            };
            if (spawn_quiet("/bin/zsh", sibling_write) == 0) return 103;
            char *allowed_unlink[] = {
                "/bin/zsh",
                "-fc",
                "rm child-canary",
                NULL
            };
            if (spawn_quiet("/bin/zsh", allowed_unlink) == 0) return 104;
            char *allowed_chmod[] = {
                "/bin/zsh",
                "-fc",
                "chmod 777 child-canary",
                NULL
            };
            if (spawn_quiet("/bin/zsh", allowed_chmod) == 0) return 105;
            char *git_version[] = {
                "/Library/Developer/CommandLineTools/usr/bin/git",
                "--version",
                NULL
            };
            if (
                spawn_quiet(
                    "/Library/Developer/CommandLineTools/usr/bin/git",
                    git_version
                ) != 0
            ) return 102;
            char *shell_git_status[] = {
                "/bin/zsh",
                "-fc",
                "/Library/Developer/CommandLineTools/usr/bin/git "
                "status --porcelain",
                NULL
            };
            if (spawn_quiet("/bin/zsh", shell_git_status) == 0) return 109;
            char *python_detach[] = {
                "/usr/bin/python3",
                "-c",
                "import os,time; os.setsid(); time.sleep(30)",
                NULL
            };
            int python_result = spawn_quiet("/usr/bin/python3", python_detach);
            if (python_result >= 0) return 106;
            if (-python_result != EACCES && -python_result != EPERM) return 107;
            char *child_fork[] = {
                "/bin/zsh",
                "-fc",
                ": & child=$!; wait \"$child\"",
                NULL
            };
            if (spawn_quiet("/bin/zsh", child_fork) == 0) return 108;
            char protected_command[4096];
            int command_length = snprintf(
                protected_command,
                sizeof(protected_command),
                "printf bad > %s/write-canary",
                protected_home
            );
            if (
                command_length <= 0
                || (size_t)command_length >= sizeof(protected_command)
            ) return 91;
            char *protected_write[] = {
                "/bin/zsh",
                "-fc",
                protected_command,
                NULL
            };
            if (spawn_quiet("/bin/zsh", protected_write) == 0) return 92;
            char *git_write[] = {
                "/bin/zsh",
                "-fc",
                "printf bad > .git",
                NULL
            };
            if (spawn_quiet("/bin/zsh", git_write) == 0) return 93;
            char *codex_write[] = {
                "/bin/zsh",
                "-fc",
                "printf bad > .codex/config.toml",
                NULL
            };
            if (spawn_quiet("/bin/zsh", codex_write) == 0) return 94;
            char url[128];
            int url_length = snprintf(
                url,
                sizeof(url),
                "http://127.0.0.1:%d/",
                port
            );
            if (url_length <= 0 || (size_t)url_length >= sizeof(url)) return 95;
            char *network[] = {
                "/usr/bin/curl",
                "--silent",
                "--max-time",
                "2",
                url,
                NULL
            };
            if (spawn_quiet("/usr/bin/curl", network) == 0) return 96;
            char *reentry[] = {
                argv[0],
                "reentered",
                (char *)reentry_marker,
                NULL
            };
            int reentry_result = spawn_quiet(argv[0], reentry);
            if (reentry_result >= 0) return 97;
            if (-reentry_result != EACCES && -reentry_result != EPERM) {
                return 98;
            }

            puts("ready");
            fflush(stdout);
            char release = 0;
            if (read(STDIN_FILENO, &release, 1) != 1) return 99;
            if (!auth_is_readable(auth)) return 100;
            if (parent_connection(port) != 0) return 101;
            puts("isolated");
            return 0;
        }
        """,
    )
    protected_home = tmp_path / "protected-home"
    workspace = tmp_path / "workspace"
    process_home = tmp_path / "process-home"
    process_tmp = tmp_path / "process-tmp"
    for directory in (protected_home, workspace, process_home, process_tmp):
        directory.mkdir()
    (workspace / ".codex").mkdir()
    assert not (workspace / ".git").exists()
    secret = protected_home / "private-canary"
    secret.write_text("not-readable", encoding="utf-8")
    private_auth = process_home / "auth.json"
    private_auth.write_text("parent-only", encoding="utf-8")
    workspace_input = workspace / "workspace-input"
    workspace_input.write_text("child-readable", encoding="utf-8")
    child_canary = workspace / "child-canary"
    child_canary.write_text("before", encoding="utf-8")
    reentry_marker = workspace / "reentry-marker"
    outer_profile = _codex_sandbox_profile(
        workspace,
        process_home,
        process_tmp,
        private_codex,
        sandbox,
        protected_home,
        (PurePosixPath("child-canary"),),
    )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    stop_server = threading.Event()

    def serve() -> None:
        while not stop_server.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    request = connection.recv(1024)
                except OSError:
                    continue
                if request == b"parent":
                    connection.sendall(b"ok")
                else:
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [
                sandbox,
                "-p",
                outer_profile,
                private_codex,
                private_auth,
                secret,
                protected_home,
                str(port),
                reentry_marker,
            ],
            cwd=workspace,
            env={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "HOME": os.fspath(process_home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": ("/Library/Developer/CommandLineTools/usr/bin:/usr/bin:/bin"),
                "TMPDIR": os.fspath(process_tmp),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        ready = process.stdout.readline()
        if ready != "ready\n":
            failed_returncode = process.wait(timeout=10)
            failed_stderr = process.stderr.read()
            if (
                failed_returncode == 71
                and "sandbox_apply: Operation not permitted" in failed_stderr
            ):
                pytest.skip("the parent process forbids nested Seatbelt profiles")
            pytest.fail(f"private canary exited {failed_returncode}: {failed_stderr}")
        private_codex.unlink()
        process.stdin.write("x")
        process.stdin.flush()
        process.stdin.close()
        returncode = process.wait(timeout=30)
        stdout = process.stdout.read()
        stderr = process.stderr.read()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        stop_server.set()
        listener.close()
        server.join(timeout=2)

    assert returncode == 0, stderr
    assert stdout.strip() == "isolated"
    assert not private_codex.exists()
    assert not reentry_marker.exists()
    assert (workspace / "child-canary").read_text(encoding="utf-8") == "child-ok"
    assert not (workspace / "sibling-canary").exists()
    assert (workspace / "child-canary").stat().st_mode & 0o777 != 0o777
    assert not (protected_home / "write-canary").exists()
    assert not (workspace / ".git").exists()
    assert not (workspace / ".codex" / "config.toml").exists()
