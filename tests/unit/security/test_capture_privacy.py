import hashlib
import json
import time
from pathlib import Path

import pytest

from project_memory_hub.domain import CapturePayload, Namespace, SourceAgent
from project_memory_hub.security.capture_privacy import CapturePrivacyCanonicalizer
from project_memory_hub.security.redaction import Redactor


@pytest.fixture
def canonicalizer() -> CapturePrivacyCanonicalizer:
    return CapturePrivacyCanonicalizer(Redactor())


@pytest.mark.parametrize(
    ("value", "private_marker"),
    (
        (r"Open \Users\ROOT_MARKER\project\file.py", "ROOT_MARKER"),
        (r"Open \custom\CUSTOM_ROOT_MARKER\project\file.py", "CUSTOM_ROOT_MARKER"),
        (r"Open C:Users\DRIVE_RELATIVE_MARKER\project\file.py", "DRIVE_RELATIVE_MARKER"),
        (r"Open C:DRIVE_FILE_MARKER", "DRIVE_FILE_MARKER"),
        (r"Open C:.DRIVE_DOT_MARKER", "DRIVE_DOT_MARKER"),
        (r"Found D:.DRIVE_DOT_WITHOUT_INTENT", "DRIVE_DOT_WITHOUT_INTENT"),
        (r"Found E:private-drive-file.env", "private-drive-file"),
        (r"Found C:PRIVATE_FILE", "PRIVATE_FILE"),
        ('Found "C:PRIVATE DRIVE FILE"', "PRIVATE DRIVE FILE"),
        (r"Type C:PRIVATE_TYPE_FILE", "PRIVATE_TYPE_FILE"),
        (r"Run C:PRIVATE_RUN_FILE", "PRIVATE_RUN_FILE"),
        (r"Project C:PRIVATE_PROJECT_FILE", "PRIVATE_PROJECT_FILE"),
        (r"Type E:PRIVATE_TYPE_FILE", "PRIVATE_TYPE_FILE"),
        (r"Run Z:PRIVATE_RUN_FILE", "PRIVATE_RUN_FILE"),
        (r"Project Z:PRIVATE_PROJECT_FILE", "PRIVATE_PROJECT_FILE"),
        ('Run "E:PRIVATE SCRIPT"', "PRIVATE SCRIPT"),
        ('Run "E:PRIVATE"', "PRIVATE"),
        (r"Project Z:client-data", "client-data"),
        ('Open "C:DRIVE SPACE_MARKER\\file.py"', "SPACE_MARKER"),
        ("Open ~privatehome/project/HOME_MARKER/file.py", "HOME_MARKER"),
        ("Open /custom/CLIENT ALPHA_MARKER/project", "ALPHA_MARKER"),
    ),
    ids=(
        "windows-rooted",
        "custom-windows-rooted",
        "drive-relative",
        "drive-relative-file",
        "drive-relative-dot-file",
        "drive-relative-dot-without-path-intent",
        "drive-relative-extension-without-path-intent",
        "drive-relative-common-drive",
        "quoted-drive-relative-common-drive",
        "type-common-drive",
        "run-common-drive",
        "project-common-drive",
        "type-non-common-drive",
        "run-non-common-drive",
        "project-non-common-drive",
        "quoted-non-common-drive-space",
        "quoted-non-common-drive-simple",
        "project-non-common-drive-simple",
        "quoted-drive-relative-space",
        "named-home",
        "spaced-posix",
    ),
)
def test_private_text_fingerprints_unambiguous_local_paths(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
    private_marker: str,
) -> None:
    result = canonicalizer.private_text(value)

    assert private_marker not in result
    assert "[REDACTED:absolute_path:" in result


@pytest.mark.parametrize(
    ("value", "private_marker"),
    (
        ("Clone intranet:ORG_MARKER/REPO_MARKER", "ORG_MARKER"),
        ("Remote origin intranet:BARE_REMOTE_MARKER", "BARE_REMOTE_MARKER"),
        ("git clone 'intranet:QUOTED_REMOTE_MARKER'", "QUOTED_REMOTE_MARKER"),
        ("Remote: intranet:LABELED_REMOTE_MARKER", "LABELED_REMOTE_MARKER"),
        ("origin=intranet:EQUALS_REMOTE_MARKER", "EQUALS_REMOTE_MARKER"),
        ("Sync s3://BUCKET_MARKER/OBJECT_MARKER", "BUCKET_MARKER"),
        ("Connect wss://SOCKET_MARKER/CHANNEL_MARKER", "SOCKET_MARKER"),
    ),
    ids=(
        "single-host-scp",
        "bare-single-host-scp",
        "quoted-single-host-scp",
        "labeled-single-host-scp",
        "equals-single-host-scp",
        "s3",
        "wss",
    ),
)
def test_private_text_fingerprints_remote_shapes_fail_closed(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
    private_marker: str,
) -> None:
    result = canonicalizer.private_text(value)

    assert private_marker not in result
    assert "[REDACTED:remote" in result


@pytest.mark.parametrize(
    "value",
    (
        "Sync user@[fd00::1]:PRIVATE_IPV6_REPO",
        "Sync [fd00::1]:PRIVATE_BARE_IPV6_REPO",
        "Sync user@[fe80::1%en0]:PRIVATE_ZONE_IPV6_REPO",
        "Sync user@[fe80::1%25en0]:PRIVATE_ENCODED_ZONE_IPV6_REPO",
    ),
)
def test_private_text_fingerprints_ipv6_scp_remotes(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "rsync intranet:/PRIVATE_FOLDER ./dest",
        "scp intranet:PRIVATE_FILE ./dest",
        "Please run rsync -av intranet:PRIVATE_TREE ./dest",
        "sudo scp intranet:PRIVATE_CONFIG ./dest",
        "Sync intranet:PRIVATE_SINGLE_HOST/repo",
    ),
)
def test_private_text_fingerprints_file_transfer_remote_operands(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "Uploaded artifacts to intranet:PRIVATE_UPLOAD/repo",
        "Copied artifacts from intranet:PRIVATE_COPY/repo",
        "Connected to intranet:PRIVATE_CONNECT/repo",
        "Deployed from intranet:PRIVATE_DEPLOY/repo",
        "Source is intranet:PRIVATE_SOURCE/repo",
        "Use sftp intranet:PRIVATE_SFTP/repo",
        "sftp intranet:PRIVATE_SFTP_COMMAND/repo",
        "curl intranet:PRIVATE_CURL/repo",
    ),
)
def test_private_text_fingerprints_single_host_remote_paths_in_prose_and_commands(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    ("value", "private_marker"),
    (
        ("git clone repo:PRIVATE_ALLOWLIST_REPO", "PRIVATE_ALLOWLIST_REPO"),
        ("origin=project:PRIVATE_ALLOWLIST_REPO", "PRIVATE_ALLOWLIST_REPO"),
        ("git clone --depth 1 intranet:PRIVATE_OPTION_REPO", "PRIVATE_OPTION_REPO"),
        ("git clone --depth=1 intranet:PRIVATE_OPTION_EQUALS_REPO", "PRIVATE_OPTION_EQUALS_REPO"),
        ("git clone -b main intranet:PRIVATE_SHORT_OPTION_REPO", "PRIVATE_SHORT_OPTION_REPO"),
        ("git clone -- intranet:PRIVATE_TERMINATED_REPO", "PRIVATE_TERMINATED_REPO"),
        ("git clone --branch before intranet:PRIVATE_BRANCH_REPO", "PRIVATE_BRANCH_REPO"),
        (
            "git clone --branch feature-and-fix intranet:PRIVATE_AND_REPO",
            "PRIVATE_AND_REPO",
        ),
        (
            "git clone --branch 'research&dev' intranet:PRIVATE_QUOTED_AMP",
            "PRIVATE_QUOTED_AMP",
        ),
        (
            "git clone --config http.extraHeader='A|B' intranet:PRIVATE_QUOTED_PIPE",
            "PRIVATE_QUOTED_PIPE",
        ),
        ("Remote URL: intranet:PRIVATE_URL_REPO", "PRIVATE_URL_REPO"),
        ("Remote URL: [intranet:PRIVATE_BRACKET_REPO]", "PRIVATE_BRACKET_REPO"),
        ("origin = (intranet:PRIVATE_PAREN_REPO)", "PRIVATE_PAREN_REPO"),
        ("remote.origin.url=intranet:PRIVATE_CONFIG_REPO", "PRIVATE_CONFIG_REPO"),
        ("remote.origin.url intranet:PRIVATE_CONFIG_SPACE_REPO", "PRIVATE_CONFIG_SPACE_REPO"),
        ("git clone `intranet:PRIVATE_BACKTICK_REPO`", "PRIVATE_BACKTICK_REPO"),
        ("git clone REDACTED:remote:PRIVATE_FORGED_MARKER", "PRIVATE_FORGED_MARKER"),
        ("Remote URL: `intranet:PRIVATE_BACKTICK_URL`", "PRIVATE_BACKTICK_URL"),
        (
            'remote.origin.url = "intranet:PRIVATE_QUOTED_CONFIG"',
            "PRIVATE_QUOTED_CONFIG",
        ),
        (
            "Remote URL: [REDACTED:remote:PRIVATE_SPOOF_REPO]",
            "PRIVATE_SPOOF_REPO",
        ),
        ("Remote URL: <intranet:PRIVATE_ANGLE_REPO>", "PRIVATE_ANGLE_REPO"),
        ("Remote URL: {intranet:PRIVATE_BRACE_REPO}", "PRIVATE_BRACE_REPO"),
        ("Remote URL: “intranet:PRIVATE_CURLY_REPO”", "PRIVATE_CURLY_REPO"),
        (
            "Remote URL: [label](intranet:PRIVATE_MARKDOWN_REPO)",
            "PRIVATE_MARKDOWN_REPO",
        ),
        ("URL: intranet:PRIVATE_BARE_URL", "PRIVATE_BARE_URL"),
        ("pushurl = intranet:PRIVATE_PUSH_URL", "PRIVATE_PUSH_URL"),
        ("Repository URL: intranet:PRIVATE_REPOSITORY_URL", "PRIVATE_REPOSITORY_URL"),
        ("Git URL: intranet:PRIVATE_GIT_URL", "PRIVATE_GIT_URL"),
        ("SSH URL: intranet:PRIVATE_SSH_URL", "PRIVATE_SSH_URL"),
        ("Submodule foo URL: intranet:PRIVATE_SUBMODULE_URL", "PRIVATE_SUBMODULE_URL"),
        (
            "git config submodule.foo.url intranet:PRIVATE_SUBMODULE_CONFIG",
            "PRIVATE_SUBMODULE_CONFIG",
        ),
        ("submodule.foo.url=intranet:PRIVATE_SUBMODULE_KEY", "PRIVATE_SUBMODULE_KEY"),
        ("remote.origin.pushurl=intranet:PRIVATE_PUSHURL_KEY", "PRIVATE_PUSHURL_KEY"),
        ("branch.main.remote intranet:PRIVATE_BRANCH_REMOTE", "PRIVATE_BRANCH_REMOTE"),
        ("branch.main.pushRemote = intranet:PRIVATE_BRANCH_PUSH", "PRIVATE_BRANCH_PUSH"),
        ("remote.pushDefault intranet:PRIVATE_PUSH_DEFAULT", "PRIVATE_PUSH_DEFAULT"),
        ("checkout.defaultRemote=intranet:PRIVATE_DEFAULT_REMOTE", "PRIVATE_DEFAULT_REMOTE"),
    ),
    ids=(
        "git-command-code-label-alias",
        "assignment-code-label-alias",
        "git-command-options",
        "git-command-option-equals",
        "git-command-short-option",
        "git-command-option-terminator",
        "git-command-branch-before",
        "git-command-branch-and",
        "git-command-quoted-ampersand",
        "git-command-quoted-pipe",
        "remote-url-label",
        "remote-url-bracket-wrapper",
        "origin-parenthesis-wrapper",
        "git-config-key",
        "git-config-key-space",
        "backtick-quoted",
        "forged-redaction-marker",
        "remote-url-backtick-quoted",
        "git-config-double-quoted",
        "spoofed-marker-bracket-wrapper",
        "remote-url-angle-wrapper",
        "remote-url-brace-wrapper",
        "remote-url-curly-quote",
        "remote-url-markdown-link",
        "bare-url-label",
        "pushurl-label",
        "repository-url-label",
        "git-url-label",
        "ssh-url-label",
        "submodule-url-label",
        "git-submodule-config",
        "submodule-config-key",
        "remote-pushurl-key",
        "branch-remote-key",
        "branch-push-remote-key",
        "remote-push-default-key",
        "checkout-default-remote-key",
    ),
)
def test_private_text_fingerprints_ambiguous_aliases_in_strong_remote_context(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
    private_marker: str,
) -> None:
    result = canonicalizer.private_text(value)

    assert private_marker not in result
    assert "[REDACTED:remote" in result


@pytest.mark.parametrize(
    "value",
    (
        "Clone" + (" " * 200) + "intranet:PRIVATE_SPACED_REPO",
        '{"origin":"intranet:PRIVATE_JSON_REPO"}',
        "Remote URL intranet:PRIVATE_NO_SEPARATOR_REPO",
        'Remote URL: "intranet:PRIVATE_ORG;PRIVATE_SUFFIX"',
        "Remote URL: https://example.invalid/PRIVATE_ORG;PRIVATE_SUFFIX",
        "Remote URL: [label](intranet:PRIVATE_ORG;PRIVATE_SUFFIX)",
        "Clone [intranet:PRIVATE_WEAK_BRACKET]",
        "Remote origin [intranet:PRIVATE_WEAK_ORIGIN]",
        "Clone [REDACTED:remote:0123456789abcdef]PRIVATE_SUFFIX:SECRET",
    ),
)
def test_private_text_fingerprints_remote_context_boundaries_idempotently(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "git config url.intranet:PRIVATE_BASE.insteadOf gh:",
        "git config url.intranet:PRIVATE_PUSH.pushInsteadOf gh:",
        "git config 'url.intranet:PRIVATE_QUOTED.insteadOf' gh:",
        "git config url.intranet:PRIVATE_FIRST.insteadOf gh:\nDone",
        "url.intranet:PRIVATE_BARE.insteadOf = gh:",
    ),
)
def test_private_text_fingerprints_git_url_rewrite_keys_idempotently(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        '{"url.intranet:PRIVATE_JSON.insteadOf":"gh:"}',
        "git config 'url.intranet:PRIVATE WHOLE SPACE.insteadOf' gh:",
        "git config url.intranet:PRIVATE=BASE.insteadOf gh:",
        "git config url.intranet:PUBLIC.insteadOf corp:PRIVATE_ALIAS",
        "git config url.intranet:PUBLIC.pushInsteadOf corp:PRIVATE_PUSH_ALIAS",
        '[url "public"]\n    insteadOf = corp:PRIVATE_SECTION_ALIAS',
        ('[url "public"]\n    insteadOf = corp:PRIVATE_MIDLINE\n[core]\n    editor = vim'),
        "git config url.intranet:PRIVATE_SEMI.insteadOf;",
        "Key url.intranet:PRIVATE_COMMA.insteadOf, was set",
        "Key (url.intranet:PRIVATE_PAREN.insteadOf)",
        "Key 'url.intranet:PRIVATE_PERIOD.insteadOf'.",
    ),
)
def test_private_text_fingerprints_git_url_rewrite_keys_and_operands(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


def test_git_url_rewrite_redaction_preserves_following_behavior_facts(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    command = "Decision: set url.public.insteadOf gh:; keep retry enabled"
    config = '[url "public"]\n    insteadOf = corp:PRIVATE_MIDLINE\n[core]\n    editor = vim'

    private_command = canonicalizer.private_text(command)
    private_config = canonicalizer.private_text(config)

    assert "keep retry enabled" in private_command
    assert "[core] editor = vim" in private_config
    assert "PRIVATE_MIDLINE" not in private_config


@pytest.mark.parametrize(
    "value",
    (
        "Remote URL is intranet:PRIVATE_IS",
        "Remote URL changed from public to intranet:PRIVATE_CHANGED",
        "Repository URL points to intranet:PRIVATE_POINTS",
        "Git URL now uses org/PRIVATE_RELATIVE",
        "remote.origin.url is intranet:PRIVATE_CONFIG_PROSE",
    ),
)
def test_private_text_fingerprints_natural_language_remote_assignments(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


def test_changed_paths_rejects_windows_rooted_and_parent_traversal(
    canonicalizer: CapturePrivacyCanonicalizer,
    tmp_path: Path,
) -> None:
    assert (
        canonicalizer.changed_paths(
            [
                r"\Users\ROOT_MARKER\outside.py",
                r"src\..\TRAVERSAL_MARKER\outside.py",
                r"C:Users\DRIVE_RELATIVE_MARKER\outside.py",
                r"D:..\DRIVE_TRAVERSAL_MARKER\outside.py",
            ],
            tmp_path,
        )
        == []
    )


@pytest.mark.parametrize(
    "code_fact",
    (
        "GET /users/123 returned 200",
        "Call /api/v1/users",
        "path:src/app.py model:provider/gpt-5 route:/api/v1",
        "Failure at app.py:42 in package.module",
        "package.sub.module:Class",
        r"Regex \d+\w+ matched",
        r"Use replacement \1 in parser",
        r"Python newline escape \n is intentional",
        "Call module:Class/method failed",
        "pytest:tests/unit/test_capture.py",
        "Remote module:Class/method",
        "Call origin module:Class/method failed",
        "Run clone module:Class/method",
        "Remote pytest:tests/unit/test_capture.py",
    ),
)
def test_private_text_preserves_api_routes_and_code_diagnostics(
    canonicalizer: CapturePrivacyCanonicalizer,
    code_fact: str,
) -> None:
    assert canonicalizer.private_text(code_fact) == code_fact


@pytest.mark.parametrize(
    "value",
    (
        "Open /api/PRIVATE_CLIENT/file.py",
        "Inspect /metrics/PRIVATE.log",
    ),
)
def test_private_text_fingerprints_route_like_paths_in_local_file_context(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:absolute_path:" in private


@pytest.mark.parametrize(
    "ordinary_fact",
    (
        "Choice A:enabled",
        "Status X:FAILED",
        "Coordinates x:10 y:20",
        "remoteWorker:Class/method",
        "cloneFactory:Class/method",
        "originFactory:Class/method",
        "git clone-factory module:Class/method",
        "foo.git clone module:Class/method",
        "not-origin=module:Class/method",
        "Type T:Value is invalid",
        "Point x:10 y:20",
        "Variant A:enabled",
        "Choice: A:enabled",
        "Choice C:enabled",
        "Choice A:enabled.",
        "Status X:FAILED!",
        "Coordinates x:10.",
        "echo git clone module:Class/method",
        "python tool.py git clone module:Class/method",
        "git status --porcelain git clone module:Class/method",
        "printf %s git clone Choice:A",
        "sudo echo git clone module:Class/method",
        "sudo -u git clone module:Class/method",
        "command -v git clone module:Class/method",
        "command -V git clone module:Class/method",
        "node tool.js git clone module:Class/method",
        "echo 'literal `git clone module:Class/method`'",
        "echo '$(git clone module:Class/method)'",
        "Remote URL parser now preserves module:Class/method",
        "remote.url validation succeeded",
        "URL parser result X:FAILED",
        "Repository URL parser result X:FAILED",
        "Git URL validation status X:FAILED",
        "SSH URL parser coordinates x:10",
        "Submodule foo URL parser result code:200",
        "Fetch coordinates x:10 y:20",
        "Push status X:FAILED",
        "Pull config A:enabled",
        "Fetch metric p95:10ms",
        "Push result code:200",
        "Pull request id:123",
        "Fetch coordinates: x:10 y:20",
        "Fetch HTTP status X:FAILED",
        "Push response code:200",
        "Pull model provider:gpt-5",
        "Clone option A:enabled",
        "Repository URL status:OK",
        "Remote URL validator:ERROR",
        "remote.origin.url parser:FAILED",
        "Git command: git clone module:Class/method",
        "Git command was git clone module:Class/method",
        "Git example: git fetch module:Class/method",
        "Git invocation: git ${ACTION} module:Class/method",
        "Git URL command: git $(printf clone) module:Class/method",
        "Uploaded diagnostic module:Class/method",
        "Copied status:ok/path",
    ),
)
def test_private_text_preserves_non_path_colon_tokens_and_keyword_prefixes(
    canonicalizer: CapturePrivacyCanonicalizer,
    ordinary_fact: str,
) -> None:
    assert canonicalizer.private_text(ordinary_fact) == ordinary_fact


@pytest.mark.parametrize(
    "value",
    (
        "sudo -l git clone module:Class/method",
        "sudo -v git clone module:Class/method",
        "sudo -K git clone module:Class/method",
        "sudo --help git clone module:Class/method",
        "sudo --validate git clone module:Class/method",
        "sudo --list git clone module:Class/method",
        "xcrun --help git clone module:Class/method",
        "xcrun --version git clone module:Class/method",
        "git --help clone module:Class/method",
        "git -h clone module:Class/method",
        "git --version clone module:Class/method",
        "git -v clone module:Class/method",
        "git --html-path clone module:Class/method",
        "git --man-path clone module:Class/method",
        "git --info-path clone module:Class/method",
        "env --help git clone module:Class/method",
        "env --version git clone module:Class/method",
        "nohup --help git clone module:Class/method",
        "nohup --version git clone module:Class/method",
        "time --help git clone module:Class/method",
        "nice --help git clone module:Class/method",
        "caffeinate -h git clone module:Class/method",
        "xargs --help git clone module:Class/method",
    ),
)
def test_private_text_preserves_nonexecuting_query_mode_code_references(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    assert canonicalizer.private_text(value) == value


@pytest.mark.parametrize(
    "value",
    (
        "sudo -l git clone intranet:PRIVATE_QUERY_REMOTE",
        "git --help clone intranet:PRIVATE_GIT_QUERY_REMOTE",
        "env --help git clone intranet:PRIVATE_ENV_QUERY_REMOTE",
        "sudo -k git clone intranet:PRIVATE_EXECUTING_SUDO_REMOTE",
        "git --help clone repo:PRIVATE_ALLOWLIST_QUERY_REMOTE",
        "sudo -l git clone project:PRIVATE_ALLOWLIST_SUDO_REMOTE",
        "command -v git clone intranet:PRIVATE_COMMAND_V_REMOTE",
        "command -V git clone repo:PRIVATE_COMMAND_CAP_V_REMOTE",
        "command --help git clone project:PRIVATE_COMMAND_HELP_REMOTE",
        "echo git clone intranet:PRIVATE_ECHO_REMOTE",
        "python tool.py git fetch repo:PRIVATE_PYTHON_REMOTE",
        "printf %s git pull project:PRIVATE_PRINTF_REMOTE",
        "sudo echo git push intranet:PRIVATE_SUDO_ECHO_REMOTE",
        "Git command: git clone intranet:PRIVATE_LITERAL_REMOTE",
        "Git command was git clone intranet:PRIVATE_WAS_REMOTE",
        "Git example: git fetch intranet:PRIVATE_FETCH_REMOTE",
        "Git invocation: git ${ACTION} intranet:PRIVATE_DYNAMIC_PROSE_REMOTE",
        "Git URL command: git $(printf clone) intranet:PRIVATE_SUBST_REMOTE",
    ),
)
def test_private_text_still_fingerprints_private_operands_in_query_like_commands(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote" in private
    assert canonicalizer.private_text(private) == private


def test_private_text_keeps_the_following_semicolon_command_outside_git_remote_redaction(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = "git clone;echo intranet:NOT_A_REMOTE"

    private = canonicalizer.private_text(value)

    assert private.startswith("git clone [REDACTED:remote_command:")
    assert "intranet:NOT_A_REMOTE" in private
    assert canonicalizer.private_text(private) == private


def test_private_text_treats_a_newline_as_canonical_whitespace_before_command_redaction(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    private = canonicalizer.private_text("git clone\necho intranet:PRIVATE_NEXT_LINE")

    assert "PRIVATE_NEXT_LINE" not in private
    assert private.startswith("git clone [REDACTED:remote_command:")
    assert canonicalizer.private_text(private) == private


def test_private_text_is_idempotent_after_remote_and_path_fingerprinting(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = (
        "Inspect /Users/PRIVATE_IDEMPOTENT/file.py; "
        "git clone --depth 1 intranet:PRIVATE_IDEMPOTENT_REPO; "
        "Choice A:enabled"
    )

    private = canonicalizer.private_text(value)

    assert canonicalizer.private_text(private) == private


def test_private_text_is_idempotent_after_general_secret_redaction(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = "Use sk-proj-abcdefghijklmnop"

    private = canonicalizer.private_text(value)

    assert private == "Use [REDACTED:api_key]"
    assert canonicalizer.private_text(private) == private


def test_private_text_fingerprints_git_remote_after_a_long_bounded_option_list(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = "git clone " + ("--config=value " * 80) + "intranet:PRIVATE_LONG_OPTIONS_REPO"

    private = canonicalizer.private_text(value)

    assert "PRIVATE_LONG_OPTIONS_REPO" not in private
    assert "[REDACTED:remote_command:" in private


@pytest.mark.parametrize(
    "filter_argument",
    (
        "--filter=blob:none",
        "--filter=tree:0",
        "--filter blob:none",
    ),
)
def test_private_text_fingerprints_git_remote_command_without_parsing_filter_spec(
    canonicalizer: CapturePrivacyCanonicalizer,
    filter_argument: str,
) -> None:
    value = f"git clone {filter_argument} intranet:PRIVATE_FILTER_REPO"

    private = canonicalizer.private_text(value)

    assert filter_argument not in private
    assert "PRIVATE_FILTER_REPO" not in private
    assert private.startswith("git clone [REDACTED:remote_command:")


@pytest.mark.parametrize(
    "value",
    (
        "git fetch --deepen 10 intranet:PRIVATE_DEEPEN_REPO",
        "git pull --strategy ours intranet:PRIVATE_STRATEGY_REPO",
        "git push --receive-pack custom intranet:PRIVATE_RECEIVE_REPO",
        "git -c protocol.version=2 clone repo:PRIVATE_GLOBAL_CONFIG_REPO",
        "git -C repo clone intranet:PRIVATE_GLOBAL_C_REPO",
        "git --no-pager clone intranet:PRIVATE_GLOBAL_FLAG_REPO",
        "./git clone repo:PRIVATE_RELATIVE_EXEC_REPO",
        "`git clone repo:PRIVATE_BACKTICK_COMMAND_REPO`",
        "git clone \\\nintranet:PRIVATE_CONTINUED_REPO",
        "git clone 'intra'net:PRIVATE_SPLIT_REPO",
        'git clone intra"net:PRIVATE_SPLIT_DOUBLE_REPO"',
        r"git clone intranet\:PRIVATE_ESCAPED_COLON_REPO",
        "git clone intranet':'PRIVATE_QUOTED_COLON_REPO",
        "git clone $'intranet:PRIVATE_ANSI_REPO'",
        'git clone "${HOST}:PRIVATE_VARIABLE_REPO"',
        "git clone user@[fd00::1]:PRIVATE_IPV6_REPO",
        "git clone 'intranet:PRIVATE QUOTED SPACE REPO'",
        "git clone >log intranet:PRIVATE_STDOUT_REPO",
        "git clone 2>log intranet:PRIVATE_STDERR_REPO",
        "git clone <input intranet:PRIVATE_STDIN_REPO",
        "git clone 2>&1 intranet:PRIVATE_DUP_REPO",
        "git clone -vb main intranet:PRIVATE_SHORT_CLUSTER_REPO",
        "git clone --bran main intranet:PRIVATE_ABBREV_REPO",
    ),
)
def test_private_text_fingerprints_the_whole_git_remote_command_linearly(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote_command:" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "$ git clone intranet:PRIVATE_PROMPT_REPO",
        "GIT_SSH_COMMAND=ssh git clone intranet:PRIVATE_ASSIGNMENT_REPO",
        "(git clone intranet:PRIVATE_SUBSHELL_REPO)",
        "{ git clone intranet:PRIVATE_GROUP_REPO; }",
        "echo $(git clone intranet:PRIVATE_SUBSTITUTION_REPO)",
        "echo `git clone intranet:PRIVATE_NESTED_BACKTICK_REPO`",
        "! git clone intranet:PRIVATE_NEGATED_REPO",
        "if git clone intranet:PRIVATE_IF_REPO",
        "xcrun git clone intranet:PRIVATE_XCRUN_REPO",
        "Please run git clone intranet:PRIVATE_PROSE_REPO",
        "I ran git clone intranet:PRIVATE_I_RAN_REPO",
        "Codex ran git clone intranet:PRIVATE_CODEX_RAN_REPO",
        "Command: git clone intranet:PRIVATE_LOG_REPO",
        "- Ran git clone intranet:PRIVATE_MARKDOWN_LOG_REPO",
        "执行 git clone intranet:PRIVATE_CHINESE_REPO",
        "运行了 `git clone intranet:PRIVATE_CHINESE_MARKDOWN_REPO`",
        "bash -c 'git clone intranet:PRIVATE_BASH_REPO'",
        "bash --norc -c 'git clone intranet\\:PRIVATE_BASH_NORC_REPO'",
        "bash --rcfile cfg -c 'git clone intranet\\:PRIVATE_BASH_RCFILE_REPO'",
        "bash -O extglob -c 'git clone intranet\\:PRIVATE_BASH_O_REPO'",
        "zsh -lc 'git clone intranet:PRIVATE_ZSH_REPO'",
        "sh -o posix -c 'git clone intranet\\:PRIVATE_SH_OPTION_REPO'",
        "fish -c 'git clone intranet\\:PRIVATE_FISH_REPO'",
        "fish -C 'git clone intranet\\:PRIVATE_FISH_INIT_REPO'",
        "eval 'git clone intranet\\:PRIVATE_EVAL_REPO'",
        "env -u HOME git clone intranet:PRIVATE_ENV_REPO",
        "time -p git clone intranet:PRIVATE_TIME_REPO",
        "command -p git clone intranet:PRIVATE_COMMAND_REPO",
        "nohup -- git clone intranet:PRIVATE_NOHUP_REPO",
        "exec git clone intranet:PRIVATE_EXEC_REPO",
        "git clone 2<&1 intranet:PRIVATE_DUP_INPUT_REPO",
        "git clone $(printf foo; printf bar):PRIVATE_COMMAND_SUB_REPO",
        "git clone $(printf foo | printf bar):PRIVATE_PIPE_SUB_REPO",
        "echo prefix$(git clone intranet\\:PRIVATE_PREFIX_SUB_REPO)suffix",
        "echo x$(git fetch user@[fd00::1]:PRIVATE_IPV6_SUB_REPO)y",
        "git $(printf clone) intranet:PRIVATE_DYNAMIC_REPO",
        "git ${ACTION} intranet:PRIVATE_ACTION_REPO",
        "bash -c 'git ${ACTION:-clone} \"$1\"' _ intranet:PRIVATE_BASH_ACTION_REPO",
    ),
)
def test_private_text_fingerprints_real_git_command_launch_forms(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote_command:" in private
    assert canonicalizer.private_text(private) == private


def test_private_text_stabilizes_a_remote_marker_with_trailing_command_prose(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = "git clone [REDACTED:remote:0123456789abcdef]: failed"

    private = canonicalizer.private_text(value)

    assert private.startswith("git clone [REDACTED:remote_command:")
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "Pull request:123; git fetch origin",
        "Push result:200; git pull origin",
        "Fetch metric:p95; git push origin main",
        "Clone status:FAILED; git fetch origin",
        "Pull module:Class/method; git fetch origin",
        "Pull request:123; /Users/alice/repo",
    ),
)
def test_private_text_shields_existing_private_markers_during_weak_context_analysis(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "Clone [REDACTED:remote:0123456789abcdef]; repo:PRIVATE_TAIL; [REDACTED:api_key]",
        "Pull [REDACTED:absolute_path:0123456789abcdef]; project:PRIVATE_PROJECT_TAIL",
    ),
)
def test_private_text_fingerprints_contextual_remote_aliases_after_private_markers(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "Remote URL [REDACTED:remote:0123456789abcdef] [REDACTED:api_key]",
        "Remote URL sk-proj-abcdefghijklmnop ghp_abcdefghijklmnopqrstuvwxyz123456",
        "Repository URL url.intranet:PRIVATE.insteadOf sk-proj-abcdefghijklmnop",
    ),
)
def test_private_text_handles_remote_context_that_normalizes_to_only_private_markers(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE" not in private
    assert canonicalizer.private_text(private) == private


@pytest.mark.parametrize(
    "value",
    (
        "Remote URL: [intranet:PRIVATE_REPO]",
        "origin = (intranet:PRIVATE_REPO)",
    ),
)
def test_private_text_replaces_wrapped_remote_value_idempotently(
    canonicalizer: CapturePrivacyCanonicalizer,
    value: str,
) -> None:
    private = canonicalizer.private_text(value)

    assert "PRIVATE_REPO" not in private
    assert "[REDACTED:remote:" in private
    assert canonicalizer.private_text(private) == private


def test_private_text_processes_a_maximum_colon_dense_field_in_linear_time(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = ("a:x " * 8191).strip()

    started = time.perf_counter()
    canonicalizer.private_text(value)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0


def test_private_text_collapses_safe_fingerprint_expansion_to_a_bounded_marker(
    canonicalizer: CapturePrivacyCanonicalizer,
) -> None:
    value = ("Clone x:y;" * 800).strip()

    private = canonicalizer.private_text(value)

    assert len(private.encode("utf-8")) <= 32 * 1024
    assert "x:y" not in private
    assert canonicalizer.private_text(private) == private


def test_structure_rejects_an_aggregate_capture_payload_over_the_bound(
    canonicalizer: CapturePrivacyCanonicalizer,
    tmp_path: Path,
) -> None:
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="provider/gpt-5"),
        source_record_id="aggregate-bound",
        objective="objective",
        outcome="outcome",
        decisions=["x" * (32 * 1024)] * 17,
    )

    with pytest.raises(ValueError, match="capture payload exceeds bound"):
        canonicalizer.structure(payload, tmp_path)


def test_empty_resolution_list_preserves_legacy_private_structure(tmp_path: Path) -> None:
    canonicalizer = CapturePrivacyCanonicalizer(Redactor())
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        source_record_id="compatibility-record",
        objective="objective",
        outcome="outcome",
        decisions=["decision"],
        resolved_open_issues=[],
    )
    structure = canonicalizer.structure(payload, tmp_path)
    expected = {
        "objective": "objective",
        "outcome": "outcome",
        "decisions": ["decision"],
        "failed_attempts": [],
        "verified_commands": [],
        "changed_paths": [],
        "preferences": [],
        "risks": [],
        "open_issues": [],
        "reusable_lessons": [],
    }
    assert "resolved_open_issues" not in structure
    assert structure == expected
    canonical_json = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical_json.encode()).hexdigest() == (
        "f214abfb70fffe382ef096ed63c2baa496b81db6bd8f40e4b8ab46f7c368c175"
    )


def test_nonempty_resolution_list_is_canonicalized_and_counted(tmp_path: Path) -> None:
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        source_record_id="resolution-record",
        objective="objective",
        outcome="outcome",
        resolved_open_issues=["  exact   issue  "],
    )
    structure = CapturePrivacyCanonicalizer(Redactor()).structure(payload, tmp_path)
    assert structure["resolved_open_issues"] == ["exact issue"]


def test_portable_structure_rejects_non_utf8_text_as_a_controlled_value_error(
    canonicalizer: CapturePrivacyCanonicalizer,
    tmp_path: Path,
) -> None:
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="provider/gpt-5"),
        source_record_id="invalid-unicode",
        objective="objective",
        outcome="bad-\ud800-text",
    )

    with pytest.raises(ValueError, match="valid UTF-8"):
        canonicalizer.portable_structure(payload)


@pytest.mark.parametrize(
    "unsafe_text",
    ("safe\x1b]0;PMH-PWN\x07tail", "safe\x00tail", "safe\u202etail"),
)
def test_portable_structure_rejects_terminal_and_bidi_controls(
    canonicalizer: CapturePrivacyCanonicalizer,
    tmp_path: Path,
    unsafe_text: str,
) -> None:
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="provider/gpt-5"),
        source_record_id="unsafe-control",
        objective="objective",
        outcome=unsafe_text,
    )

    with pytest.raises(ValueError, match="capture field exceeds bound"):
        canonicalizer.portable_structure(payload)
