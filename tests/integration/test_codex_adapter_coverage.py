import hashlib
import json
import os
from pathlib import Path

import pytest

import project_memory_hub.adapters.codex as codex_module
from project_memory_hub.adapters.codex import (
    CAPTURE_END,
    CAPTURE_START,
    CodexAdapter,
    CodexContextUnavailable,
)
from project_memory_hub.security.redaction import Redactor


THREAD_ID = "70000000-0000-4000-8000-00000000000a"
RUNTIME_CWD = "/fixture/repo"


def _json_line(record: dict[str, object]) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def _runtime_snapshot(*, final_newline: bool = True) -> bytes:
    records = (
        {
            "type": "session_meta",
            "payload": {"id": THREAD_ID, "session_id": THREAD_ID},
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-current",
                "cwd": RUNTIME_CWD,
                "model": "gpt-5.6-sol",
            },
        },
    )
    snapshot = "".join(_json_line(record) for record in records).encode()
    return snapshot if final_newline else snapshot[:-1]


def _read_runtime_model(
    source: Path,
    *,
    size: int | None = None,
    max_record_bytes: int = 4096,
    max_records: int = 2,
) -> tuple[str, str]:
    descriptor = os.open(source, os.O_RDONLY)
    try:
        return codex_module._runtime_model_from_descriptor(
            descriptor,
            source.stat().st_size if size is None else size,
            THREAD_ID,
            RUNTIME_CWD,
            redactor=Redactor(),
            max_line_bytes=max_record_bytes,
            max_record_bytes=max_record_bytes,
            max_nonsemantic_record_bytes=max_record_bytes,
            max_records=max_records,
        )
    finally:
        os.close(descriptor)


def _citation_suffix(
    *,
    citation_lines: tuple[str, ...] = ("MEMORY.md:1-2|note=[safe note]",),
    rollout_ids: tuple[str, ...] = (THREAD_ID,),
) -> list[str]:
    return [
        "<oai-mem-citation>",
        "<citation_entries>",
        *citation_lines,
        "</citation_entries>",
        "<rollout_ids>",
        *rollout_ids,
        "</rollout_ids>",
        "</oai-mem-citation>",
    ]


@pytest.mark.parametrize(
    "suffix",
    (
        ["<oai-mem-citation>"],
        _citation_suffix()[:-1],
        [
            "<oai-mem-citation>",
            "<citation_entries>",
            "MEMORY.md:1-2|note=[safe note]",
            "<rollout_ids>",
            THREAD_ID,
            "</rollout_ids>",
            "</oai-mem-citation>",
        ],
        [
            "<oai-mem-citation>",
            "<citation_entries>",
            "</citation_entries>",
        ],
        [
            "<oai-mem-citation>",
            "<citation_entries>",
            "</citation_entries>",
            "<unexpected_ids>",
            "</rollout_ids>",
            "</oai-mem-citation>",
        ],
        [
            "<oai-mem-citation>",
            "<citation_entries>",
            "</citation_entries>",
            "<rollout_ids>",
            "</unexpected_ids>",
            "</oai-mem-citation>",
        ],
    ),
    ids=(
        "short-fragment",
        "missing-outer-close",
        "missing-citation-close",
        "truncated-after-citations",
        "wrong-rollout-open",
        "wrong-rollout-close",
    ),
)
def test_capture_suffix_rejects_truncated_or_malformed_directives(suffix):
    assert not codex_module._valid_capture_suffix(suffix)


@pytest.mark.parametrize(
    "citation",
    (
        "",
        "MEMORY.md:1-2|note=[unsafe\x00note]",
        "MEMORY.md:1-2|note=[unsafe\x1fnote]",
        "MEMORY.md:1-2|note=[unsafe\x7fnote]",
        "\ud800",
        "x" * 4097,
    ),
    ids=("empty", "nul", "unit-separator", "delete", "invalid-utf8", "oversized"),
)
def test_capture_suffix_rejects_unsafe_citation_entries(citation):
    assert not codex_module._valid_capture_suffix(_citation_suffix(citation_lines=(citation,)))


@pytest.mark.parametrize(
    "rollout_id",
    (
        THREAD_ID.upper(),
        "{" + THREAD_ID + "}",
        "urn:uuid:" + THREAD_ID,
        "not-a-uuid",
        "",
    ),
    ids=("uppercase", "braced", "urn", "malformed", "empty"),
)
def test_capture_suffix_rejects_noncanonical_rollout_ids(rollout_id):
    assert not codex_module._valid_capture_suffix(_citation_suffix(rollout_ids=(rollout_id,)))


def test_capture_suffix_enforces_inner_entry_budgets_and_accepts_blank_padding():
    assert codex_module._valid_capture_suffix(["", *_citation_suffix(), ""])
    assert not codex_module._valid_capture_suffix(
        _citation_suffix(citation_lines=tuple("entry" for _ in range(101)))
    )
    assert not codex_module._valid_capture_suffix(
        _citation_suffix(rollout_ids=tuple(THREAD_ID for _ in range(101)))
    )


@pytest.mark.parametrize(
    "suffix",
    (
        _citation_suffix()[:-1],
        _citation_suffix(citation_lines=("MEMORY.md:1-2|note=[unsafe\x00note]",)),
        _citation_suffix(rollout_ids=(THREAD_ID.upper(),)),
    ),
    ids=("truncated", "control-character", "noncanonical-uuid"),
)
def test_adapter_fails_closed_for_untrusted_capture_suffixes(tmp_path, suffix):
    message = "\n".join(
        (
            CAPTURE_START,
            "Objective: exact objective",
            "Outcome: exact outcome",
            CAPTURE_END,
            *suffix,
        )
    )
    source = tmp_path / "session.jsonl"
    source.write_text(
        _json_line(
            {
                "timestamp": "2026-07-18T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-coverage", "session_id": "session-coverage"},
            }
        )
        + _json_line(
            {
                "timestamp": "2026-07-18T00:00:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-current",
                    "cwd": RUNTIME_CWD,
                    "model": "gpt-5.6-sol",
                },
            }
        )
        + _json_line(
            {
                "timestamp": "2026-07-18T00:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-current",
                    "last_agent_message": message,
                },
            }
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings


def test_runtime_descriptor_accepts_exact_snapshot_and_hashes_only_that_snapshot(tmp_path):
    snapshot = _runtime_snapshot()
    source = tmp_path / "runtime.jsonl"
    source.write_bytes(snapshot)

    model_id, digest = _read_runtime_model(source)

    assert model_id == "gpt-5.6-sol"
    assert digest == hashlib.sha256(snapshot).hexdigest()


def test_runtime_descriptor_rejects_short_pread(tmp_path, monkeypatch):
    source = tmp_path / "runtime.jsonl"
    source.write_bytes(_runtime_snapshot())
    real_pread = os.pread

    def short_pread(descriptor: int, length: int, offset: int) -> bytes:
        chunk = real_pread(descriptor, length, offset)
        return chunk[:-1] if offset == 0 else chunk

    monkeypatch.setattr(codex_module.os, "pread", short_pread)

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        _read_runtime_model(source)


@pytest.mark.parametrize("size_delta", (-1, 1), ids=("truncated-size", "oversized-size"))
def test_runtime_descriptor_rejects_size_mismatch(tmp_path, size_delta):
    source = tmp_path / "runtime.jsonl"
    source.write_bytes(_runtime_snapshot())

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        _read_runtime_model(source, size=source.stat().st_size + size_delta)


def test_runtime_descriptor_rejects_incomplete_trailing_record(tmp_path):
    source = tmp_path / "runtime.jsonl"
    source.write_bytes(_runtime_snapshot(final_newline=False))

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        _read_runtime_model(source)


def test_runtime_descriptor_enforces_exact_record_and_record_count_boundaries(tmp_path):
    snapshot = _runtime_snapshot()
    source = tmp_path / "runtime.jsonl"
    source.write_bytes(snapshot)
    longest_record = max(len(line) for line in snapshot.splitlines(keepends=True))

    assert _read_runtime_model(source, max_record_bytes=longest_record)[0] == "gpt-5.6-sol"
    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        _read_runtime_model(source, max_record_bytes=longest_record - 1)
    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        _read_runtime_model(source, max_records=1)


def test_runtime_descriptor_rejects_oversized_unterminated_buffer(tmp_path):
    source = tmp_path / "runtime.jsonl"
    source.write_bytes(b"x" * 65)

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        _read_runtime_model(source, max_record_bytes=64)


@pytest.mark.parametrize(
    "thread_id", (object(), THREAD_ID.upper()), ids=("non-string", "uppercase")
)
def test_runtime_thread_id_must_be_canonical(thread_id):
    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        codex_module._canonical_thread_id(thread_id)


@pytest.mark.parametrize(
    "cwd",
    (
        RUNTIME_CWD,
        Path("relative/repo"),
        Path("/fixture/../repo"),
        Path("/fixture/\ud800"),
    ),
    ids=("non-path", "relative", "non-normalized", "invalid-utf8"),
)
def test_runtime_cwd_must_be_canonical(cwd):
    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        codex_module._canonical_runtime_cwd(cwd)
