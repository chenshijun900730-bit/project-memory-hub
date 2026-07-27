import pytest

from project_memory_hub.security.identifiers import (
    safe_model_identifier,
    safe_persisted_identifier,
    safe_provenance_component,
)
from project_memory_hub.security.redaction import Redactor


def test_provenance_components_are_unambiguous_but_composite_ids_allow_colons() -> None:
    redactor = Redactor()

    assert safe_provenance_component("session-safe_1", "session_id", redactor) == ("session-safe_1")
    assert safe_persisted_identifier("session-safe_1:turn-safe", "source_record_id", redactor) == (
        "session-safe_1:turn-safe"
    )
    with pytest.raises(ValueError, match="invalid session_id"):
        safe_provenance_component("session:ambiguous", "session_id", redactor)


def test_path_like_generic_words_in_ascii_source_ids_are_not_treated_as_secrets() -> None:
    redactor = Redactor()

    assert safe_provenance_component("conv-secret", "session_id", redactor) == "conv-secret"
    assert (
        safe_persisted_identifier(
            "conv-model-secret:turn-1",
            "source_record_id",
            redactor,
        )
        == "conv-model-secret:turn-1"
    )


@pytest.mark.parametrize(
    "value",
    (
        "intranet:PRIVATE_REPO.git",
        ".env",
        ".env.local",
        "id_rsa",
        "identity.pem",
    ),
)
def test_persisted_identifiers_reject_remote_and_hard_sensitive_shapes(value: str) -> None:
    with pytest.raises(ValueError, match="invalid source_record_id"):
        safe_persisted_identifier(value, "source_record_id", Redactor())


@pytest.mark.parametrize("value", (".env", ".env.local", "id_rsa", "identity.pem"))
def test_provenance_components_reject_hard_sensitive_basenames(value: str) -> None:
    with pytest.raises(ValueError, match="invalid turn_id"):
        safe_provenance_component(value, "turn_id", Redactor())


@pytest.mark.parametrize(
    "value",
    ("turn\nINJECTED", "password=RAW_ID_SECRET", "x" * 257),
)
def test_provenance_components_reject_control_secret_and_oversized_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid turn_id"):
        safe_provenance_component(value, "turn_id", Redactor())


def test_model_identifier_preserves_valid_unicode_revision_exactly() -> None:
    model_id = "提供商/model@rev+β"

    assert safe_model_identifier(model_id, Redactor()) == model_id


def test_model_identifier_preserves_provider_scoped_colon_tag() -> None:
    model_id = "ollama/llama3:8b"

    assert safe_model_identifier(model_id, Redactor()) == model_id


def test_model_identifier_preserves_bare_colon_tag_for_legacy_namespaces() -> None:
    model_id = "llama3:8b"

    assert safe_model_identifier(model_id, Redactor()) == model_id


@pytest.mark.parametrize(
    "value",
    (
        "provider/model name",
        "provider/model\tname",
        "provider/model\u00a0name",
        "provider/model\u2003name",
    ),
)
def test_model_identifier_rejects_whitespace_anywhere(value: str) -> None:
    with pytest.raises(ValueError, match="invalid model_id"):
        safe_model_identifier(value, Redactor())


@pytest.mark.parametrize(
    "value",
    (
        "s3://private-bucket/model",
        "custom+model://host/path",
        "model /Users/alice/private-model",
        "provider=/Users/alice/private-model",
        r"provider=C:\Users\alice\private-model",
        r"provider=\Users\alice\private-model",
        "provider=~/private-model",
        "provider=~PRIVATE_HOME/private-model",
        "intranet:models/private-model",
        "prefix=intranet:private-model",
        "model@/Users/alice/private-model",
        "prefix-/etc/passwd",
        "model@/data/alice/private-model",
        "prefix-/bin/sh",
    ),
)
def test_model_identifier_rejects_schemes_rooted_paths_and_single_label_remotes(value: str) -> None:
    with pytest.raises(ValueError, match="invalid model_id"):
        safe_model_identifier(value, Redactor())


@pytest.mark.parametrize(
    "value",
    (
        " gpt-5",
        "gpt-5\nINJECTED",
        "provider/password=RAW_MODEL_CREDENTIAL",
        "provider/model?token=private",
        r"\Users\alice\private-model",
        r"~\Users\alice\private-model",
    ),
)
def test_model_identifier_rejects_normalized_control_and_private_shapes(value: str) -> None:
    with pytest.raises(ValueError, match="invalid model_id"):
        safe_model_identifier(value, Redactor())
