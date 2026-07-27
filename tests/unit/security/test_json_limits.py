from __future__ import annotations

import io
import json

import pytest

import project_memory_hub.security.json_limits as json_limits
from project_memory_hub.security.json_limits import (
    MAX_JSON_NESTING_DEPTH,
    JsonNestingError,
    load_json_bounded,
    loads_json_bounded,
)


def _nested_array(depth: int) -> str:
    return ("[" * depth) + "0" + ("]" * depth)


def _mixed_nesting(depth: int) -> str:
    openings = ('{"value":' if index % 2 == 0 else "[" for index in range(depth))
    closings = ("}" if index % 2 == 0 else "]" for index in reversed(range(depth)))
    return "".join(openings) + "0" + "".join(closings)


def test_json_nesting_boundary_is_explicit() -> None:
    accepted = loads_json_bounded(_nested_array(MAX_JSON_NESTING_DEPTH))

    assert isinstance(accepted, list)
    with pytest.raises(JsonNestingError):
        loads_json_bounded(_nested_array(MAX_JSON_NESTING_DEPTH + 1))


def test_json_nesting_limit_counts_mixed_objects_and_arrays() -> None:
    assert loads_json_bounded(_mixed_nesting(MAX_JSON_NESTING_DEPTH)) is not None
    with pytest.raises(JsonNestingError):
        loads_json_bounded(_mixed_nesting(MAX_JSON_NESTING_DEPTH + 1))


def test_json_nesting_scan_ignores_brackets_inside_escaped_strings() -> None:
    document = json.dumps({"text": '[{\\"}]' * 512})

    assert loads_json_bounded(document) == {"text": '[{\\"}]' * 512}


@pytest.mark.parametrize(
    "load",
    [
        lambda document: loads_json_bounded(document),
        lambda document: load_json_bounded(io.StringIO(document)),
    ],
)
def test_excessive_nesting_is_rejected_before_json_decoder(
    load,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoder_called = False

    def decode(_document: str) -> object:
        nonlocal decoder_called
        decoder_called = True
        return None

    monkeypatch.setattr(json_limits.json, "loads", decode)

    with pytest.raises(JsonNestingError):
        load(_nested_array(MAX_JSON_NESTING_DEPTH + 1))

    assert decoder_called is False
