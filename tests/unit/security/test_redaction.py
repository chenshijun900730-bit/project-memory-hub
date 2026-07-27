import logging
from pathlib import Path
from time import perf_counter

import pytest
from pydantic import ValidationError

from project_memory_hub.security import Redactor, SensitivePathError


def synthetic_api_key(prefix: str, length: int = 40) -> str:
    return prefix + ("a" * length)


def synthetic_private_key_boundary(kind: str, variant: str = "") -> str:
    label = f"{variant} PRIVATE KEY".strip()
    return f"-----{kind} {label}-----"


def synthetic_private_key_block(body: str, variant: str = "") -> str:
    begin = synthetic_private_key_boundary("BEGIN", variant)
    end = synthetic_private_key_boundary("END", variant)
    return f"{begin}\n{body}\n{end}"


@pytest.mark.parametrize(
    "secret",
    [
        synthetic_api_key("sk-"),
        synthetic_api_key("sk-proj-"),
        synthetic_api_key("sk-ant-api03-"),
        synthetic_api_key("ghp_"),
        "xoxb-" + ("1" * 12) + "-" + ("a" * 24),
        synthetic_api_key("AIza", 35),
        "AKIA" + ("A1" * 8),
    ],
)
def test_redactor_replaces_provider_api_keys_without_returning_values(
    secret: str,
) -> None:
    result = Redactor().redact(f"token={secret}")

    assert secret not in result.text
    assert result.text == "token=[REDACTED:api_key]"
    assert result.findings == ("api_key",)


def test_redactor_replaces_generic_bearer_credentials() -> None:
    secret = "synthetic." + ("b" * 32) + ".credential"

    result = Redactor().redact(f"Authorization: Bearer {secret}")

    assert secret not in result.text
    assert result.text == "Authorization: [REDACTED:bearer_token]"
    assert result.findings == ("bearer_token",)


def test_redactor_replaces_complete_private_key_block() -> None:
    secret_body = "c" * 96
    private_key = synthetic_private_key_block(secret_body)

    result = Redactor().redact(f"before\n{private_key}\nafter")

    assert secret_body not in result.text
    assert result.text == "before\n[REDACTED:private_key]\nafter"
    assert result.findings == ("private_key",)


@pytest.mark.parametrize("field", ["password", "PASSWD", "pwd", "client_secret"])
def test_redactor_replaces_assigned_password_values(field: str) -> None:
    secret = "synthetic-password-value"

    result = Redactor().redact(f'{field} = "{secret}"')

    assert secret not in result.text
    assert "[REDACTED:password]" in result.text
    assert result.findings == ("password",)


@pytest.mark.parametrize(
    "sensitive_path",
    [
        ".env",
        ".envrc",
        ".ENV.local",
        "certificates/server.PEM",
        "keys/client.key",
        "certificates/client.p12",
        "certificates/client.PFX",
        "home/.ssh/id_rsa",
        "home/.ssh/id_dsa",
        "home/.ssh/id_ecdsa",
        "home/.ssh/id_ed25519",
        "home/.ssh/config",
        "config/credentials.json",
        "config/client_secrets.toml",
        "config/access-token.txt",
    ],
)
def test_redactor_replaces_sensitive_path_tokens_case_insensitively(
    sensitive_path: str,
) -> None:
    result = Redactor().redact(f"read {sensitive_path} then public/readme.md")

    assert sensitive_path.lower() not in result.text.lower()
    assert "public/readme.md" in result.text
    assert "[REDACTED:sensitive_path]" in result.text
    assert result.findings == ("sensitive_path",)


def test_redactor_returns_canonical_deduplicated_finding_order() -> None:
    api_key = synthetic_api_key("sk-")
    bearer = "bearer." + ("d" * 32)
    private_key = synthetic_private_key_block("e" * 64, "RSA")
    text = (
        f"config/.env password=first password=second {private_key} "
        f"Bearer {bearer} {api_key} {api_key}"
    )

    result = Redactor(max_input_chars=len(text) - 1).redact(text)

    assert result.findings == (
        "api_key",
        "bearer_token",
        "private_key",
        "password",
        "sensitive_path",
        "input_truncated",
    )
    assert result.text.count("[REDACTED:api_key]") == 2


def test_redactor_consumes_an_oversized_bearer_token_without_returning_a_tail() -> None:
    token = "A" * 5000

    result = Redactor().redact("Bearer " + token)

    assert token not in result.text
    assert "A" * 8 not in result.text
    assert result.text == "[REDACTED:bearer_token]"
    assert result.findings == ("bearer_token",)


def test_reviewer_redactor_preserves_labels_and_recovers_findings() -> None:
    original = (
        "[REDACTED:api_key] [REDACTED:bearer_token] "
        "[REDACTED:private_key] [REDACTED:password] "
        "[REDACTED:sensitive_path] [TRUNCATED:redaction_input]"
    )

    first = Redactor().redact(original)
    second = Redactor().redact(first.text)

    if first.text != original or second != first:
        pytest.fail("stable redaction labels were not idempotent", pytrace=False)
    assert first.findings == (
        "api_key",
        "bearer_token",
        "private_key",
        "password",
        "sensitive_path",
        "input_truncated",
    )


def test_redactor_discards_unexamined_tail_and_marks_truncation() -> None:
    prefix = "safe-prefix:"
    secret_tail = synthetic_api_key("sk-")

    result = Redactor(max_input_chars=len(prefix)).redact(prefix + secret_tail)

    assert secret_tail not in result.text
    assert result.text == prefix + "[TRUNCATED:redaction_input]"
    assert result.findings == ("input_truncated",)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10"])
def test_redactor_rejects_invalid_limits_without_input_data(limit: object) -> None:
    with pytest.raises((TypeError, ValueError)) as exc_info:
        Redactor(max_input_chars=limit)  # type: ignore[arg-type]

    assert "synthetic" not in str(exc_info.value)


def test_redactor_type_error_does_not_echo_supplied_object() -> None:
    marker = "synthetic-object-marker"

    class UnsafeValue:
        def __repr__(self) -> str:
            return marker

        def __str__(self) -> str:
            return marker

    with pytest.raises(TypeError) as exc_info:
        Redactor().redact(UnsafeValue())  # type: ignore[arg-type]

    assert marker not in str(exc_info.value)


def test_assert_safe_path_checks_lexical_components_without_resolving(
    tmp_path: Path,
) -> None:
    safe_path = tmp_path / "missing" / "public.txt"

    assert Redactor().assert_safe_path(safe_path) is None
    assert not safe_path.exists()


@pytest.mark.parametrize(
    "sensitive_component",
    [".env.production", "SERVER.KEY", "id_ed25519", ".SSH", "credentials"],
)
def test_assert_safe_path_raises_stable_non_leaking_error(
    sensitive_component: str,
) -> None:
    path = Path("safe") / sensitive_component / "synthetic-private-name"

    with pytest.raises(SensitivePathError) as exc_info:
        Redactor().assert_safe_path(path)

    assert str(exc_info.value) == "sensitive path rejected: sensitive_path"
    assert sensitive_component not in str(exc_info.value)
    assert str(path) not in str(exc_info.value)


def test_redaction_result_is_frozen_model() -> None:
    result = Redactor().redact("public")

    with pytest.raises(ValidationError):
        result.text = "changed"  # type: ignore[misc]


def test_redactor_never_logs_or_prints_fixture_values(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = synthetic_api_key("sk-")

    result = Redactor().redact(f"token={secret}")
    with pytest.raises(SensitivePathError) as exc_info:
        Redactor().assert_safe_path(Path("safe") / ".env" / secret)
    captured = capsys.readouterr()

    assert secret not in result.text
    assert secret not in result.findings
    assert secret not in str(exc_info.value)
    assert secret not in captured.out
    assert secret not in captured.err
    assert not caplog.records


def test_reviewer_repeated_unterminated_pem_is_conservative_and_near_linear() -> None:
    begin = synthetic_private_key_boundary("BEGIN") + "\n"

    def measure(repetitions: int) -> float:
        text = "public\n" + ((begin + ("x" * 16) + "\n") * repetitions)
        started = perf_counter()
        result = Redactor().redact(text)
        elapsed = perf_counter() - started
        if result.text != "public\n[REDACTED:private_key]":
            pytest.fail("unterminated private-key material was returned", pytrace=False)
        assert result.findings == ("private_key",)
        return elapsed

    small_elapsed = measure(400)
    large_elapsed = measure(1_600)

    if large_elapsed > (small_elapsed * 8) + 0.2:
        pytest.fail("unterminated PEM processing was not near-linear", pytrace=False)


def test_reviewer_truncation_through_pem_redacts_examined_private_material() -> None:
    public = "public\n"
    begin = synthetic_private_key_boundary("BEGIN") + "\n"
    body = "private-material-fragment-" + ("x" * 80)
    complete = public + begin + body + "\n" + synthetic_private_key_boundary("END")
    limit = len(public + begin) + 40

    result = Redactor(max_input_chars=limit).redact(complete)

    if result.text != (public + "[REDACTED:private_key][TRUNCATED:redaction_input]"):
        pytest.fail("truncated private-key material was returned", pytrace=False)
    assert result.findings == ("private_key", "input_truncated")


@pytest.mark.parametrize(("quote"), ["'", '"'], ids=["single", "double"])
def test_reviewer_password_assignment_redacts_quoted_whitespace_value(
    quote: str,
) -> None:
    value = "synthetic value with whitespace"

    result = Redactor().redact(f"password={quote}{value}{quote}")

    expected = f"password={quote}[REDACTED:password]{quote}"
    if result.text != expected:
        pytest.fail("quoted password was not fully redacted", pytrace=False)
    assert result.findings == ("password",)


def test_reviewer_complete_redaction_result_is_idempotent() -> None:
    api_key = synthetic_api_key("sk-")
    private_key = synthetic_private_key_block("y" * 64)
    source = (
        f"{api_key} Bearer synthetic.bearer.credential {private_key} "
        'password="synthetic value" config/.env benign-tail'
    )

    first = Redactor(max_input_chars=len(source) - 1).redact(source)
    second = Redactor().redact(first.text)

    if second != first:
        pytest.fail("complete redaction result was not idempotent", pytrace=False)


def test_reviewer_ordinary_sensitive_words_remain_unchanged() -> None:
    prose = "A secret can become secrets, while token is an ordinary concept."

    result = Redactor().redact(prose)

    assert result.text == prose
    assert result.findings == ()


def test_reviewer_sensitive_filenames_require_context_and_env_assignment_redacts() -> None:
    text = (
        "secret secrets token client_secret credentials.json access-token.txt .envrc .env = enabled"
    )

    result = Redactor().redact(text)

    assert result.text.startswith("secret secrets token ")
    assert result.text.count("[REDACTED:sensitive_path]") == 5
    assert ".env" not in result.text
    assert result.findings == ("sensitive_path",)


def test_second_reviewer_same_instance_api_expansion_is_idempotent() -> None:
    redactor = Redactor(max_input_chars=20)
    source = synthetic_api_key("sk-", 16) + " trailing raw text"

    first = redactor.redact(source)
    second = redactor.redact(first.text)

    if second != first:
        pytest.fail("expanded API-key result was not idempotent", pytrace=False)
    assert first.text.endswith("[TRUNCATED:redaction_input]")
    assert first.text.count("[TRUNCATED:redaction_input]") == 1
    assert first.findings == ("api_key", "input_truncated")


def test_second_reviewer_same_instance_path_expansion_is_idempotent() -> None:
    redactor = Redactor(max_input_chars=5)

    first = redactor.redact(".env trailing raw text")
    second = redactor.redact(first.text)

    if second != first:
        pytest.fail("expanded sensitive-path result was not idempotent", pytrace=False)
    assert first.text.endswith("[TRUNCATED:redaction_input]")
    assert first.text.count("[TRUNCATED:redaction_input]") == 1
    assert first.findings == ("sensitive_path", "input_truncated")


def test_second_reviewer_password_truncation_never_returns_quoted_fragment() -> None:
    prefix = 'password="'
    source = prefix + "private value continues beyond boundary" + '"'
    redactor = Redactor(max_input_chars=len(prefix) + 12)

    result = redactor.redact(source)

    expected = 'password="[REDACTED:password]"[TRUNCATED:redaction_input]'
    if result.text != expected:
        pytest.fail("truncated quoted password fragment was returned", pytrace=False)
    assert result.findings == ("password", "input_truncated")


def test_second_reviewer_password_scanner_honors_escaped_quote_suffix() -> None:
    source = 'password="private value with \\" quoted suffix" public'

    result = Redactor().redact(source)

    if result.text != 'password="[REDACTED:password]" public':
        pytest.fail("escaped quote ended password scanning early", pytrace=False)
    assert result.findings == ("password",)


def test_second_reviewer_path_core_redaction_preserves_delimiters_and_punctuation() -> None:
    source = ".env: enabled .env=enabled server.pem. server.pem: secrets."

    result = Redactor().redact(source)

    expected = (
        "[REDACTED:sensitive_path]: enabled "
        "[REDACTED:sensitive_path]=enabled "
        "[REDACTED:sensitive_path]. "
        "[REDACTED:sensitive_path]: secrets."
    )
    if result.text != expected:
        pytest.fail("sensitive path core boundaries were not preserved", pytrace=False)
    assert result.findings == ("sensitive_path",)


def test_final_reviewer_repeated_stable_labels_have_positive_bounded_cost() -> None:
    stable_label = "[REDACTED:api_key]"
    marker = "[TRUNCATED:redaction_input]"
    redactor = Redactor(max_input_chars=1)

    result = redactor.redact(stable_label * 10_000)

    if len(result.text) > len(stable_label) + len(marker):
        pytest.fail("stable labels exceeded the bounded output budget", pytrace=False)
    assert result.text == stable_label + marker
    assert result.findings == ("api_key", "input_truncated")
    assert redactor.redact(result.text) == result


def test_final_reviewer_password_label_prefix_does_not_expose_bare_suffix() -> None:
    source = "password=[REDACTED:password]private_suffix"

    result = Redactor().redact(source)

    if result.text != "password=[REDACTED:password]":
        pytest.fail("password suffix after stable label was returned", pytrace=False)
    assert result.findings == ("password",)
