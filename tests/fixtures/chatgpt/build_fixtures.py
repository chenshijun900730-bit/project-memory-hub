from __future__ import annotations

import json
import zipfile
from pathlib import Path


def conversation(
    conversation_id: str,
    *,
    user_text: str,
    assistant_text: str,
    model_slug: str | None = "gpt-5",
    title: str = "Synthetic coding task",
) -> dict:
    metadata = {} if model_slug is None else {"model_slug": model_slug}
    return {
        "id": conversation_id,
        "title": title,
        "mapping": {
            "u1": {
                "id": "u1",
                "parent": None,
                "children": ["a1"],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": [user_text]},
                    "metadata": {},
                    "create_time": 1,
                },
            },
            "a1": {
                "id": "a1",
                "parent": "u1",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": [assistant_text]},
                    "metadata": metadata,
                    "create_time": 2,
                },
            },
        },
    }


def build_export(path: Path, members: dict[str, list[dict]]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, conversations in members.items():
            archive.writestr(
                name,
                json.dumps(conversations, ensure_ascii=False, separators=(",", ":")),
            )
    return path


def build_traversal_export(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../conversations.json", "[]")
        archive.writestr("conversations.json", "[]")
    return path
