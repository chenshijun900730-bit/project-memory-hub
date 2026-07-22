from __future__ import annotations

import json
from collections.abc import Iterator
from typing import IO, Final


MAX_JSON_NESTING_DEPTH: Final = 128


class JsonNestingError(ValueError):
    """Raised when a decoded JSON document exceeds the safe nesting limit."""


def _validate_document_nesting(
    document: str,
    *,
    max_depth: int = MAX_JSON_NESTING_DEPTH,
) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > max_depth:
                raise JsonNestingError("json nesting limit exceeded")
        elif character in "]}" and depth > 0:
            depth -= 1


def _validate_json_nesting(
    value: object,
    *,
    max_depth: int = MAX_JSON_NESTING_DEPTH,
) -> object:
    stack: list[tuple[Iterator[object], int]] = [(iter((value,)), 0)]
    while stack:
        iterator, depth = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue

        if isinstance(item, dict):
            children = iter(item.values())
        elif isinstance(item, list):
            children = iter(item)
        else:
            continue

        child_depth = depth + 1
        if child_depth > max_depth:
            raise JsonNestingError("json nesting limit exceeded")
        stack.append((children, child_depth))
    return value


def load_json_bounded(stream: IO[str]) -> object:
    return loads_json_bounded(stream.read())


def loads_json_bounded(document: str) -> object:
    _validate_document_nesting(document)
    return _validate_json_nesting(json.loads(document))
