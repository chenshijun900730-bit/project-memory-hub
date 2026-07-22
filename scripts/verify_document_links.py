from __future__ import annotations

import html
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit


PUBLIC_DOCUMENTS = (
    Path("README.md"),
    Path("README.zh-CN.md"),
    Path("SECURITY.md"),
    Path("CONTRIBUTING.md"),
    Path("CODE_OF_CONDUCT.md"),
    Path("CHANGELOG.md"),
    Path("docs/getting-started.md"),
    Path("docs/architecture.md"),
    Path("docs/security.md"),
    Path("docs/releasing.md"),
    Path("docs/operations.md"),
)

TEMPORARY_MISSING_ASSETS: frozenset[str] = frozenset()

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[([^]\n]+)\]:\s*(.*)$")
_AUTOLINK = re.compile(r"(?<!\\)<([^<>\s]+)>")
_HTML_LINK = re.compile(
    r"<\s*(a|img)\b[^>]*?\b(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    flags=re.IGNORECASE,
)
_VALID_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_CONTROL_CHARACTERS = frozenset(chr(value) for value in (*range(32), 127))


class DocumentLinkError(RuntimeError):
    """Raised when a public document contains an unsafe or broken link."""


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _mask_span(text: str, start: int, end: int) -> str:
    return "".join("\n" if character == "\n" else " " for character in text[start:end])


def _mask_inline_code(text: str) -> str:
    output: list[str] = []
    cursor = 0
    index = 0
    while index < len(text):
        if text[index] != "`" or _is_escaped(text, index):
            index += 1
            continue

        run_end = index + 1
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        delimiter = text[index:run_end]
        close = run_end
        while True:
            close = text.find(delimiter, close)
            if close < 0:
                index = run_end
                break
            before_is_tick = close > 0 and text[close - 1] == "`"
            after = close + len(delimiter)
            after_is_tick = after < len(text) and text[after] == "`"
            if not before_is_tick and not after_is_tick and not _is_escaped(text, close):
                output.append(text[cursor:index])
                output.append(_mask_span(text, index, after))
                cursor = after
                index = after
                break
            close = after
    output.append(text[cursor:])
    return "".join(output)


def _markdown_without_code(document: str) -> str:
    output: list[str] = []
    fence_character: str | None = None
    fence_length = 0

    for line in document.splitlines(keepends=True):
        logical_line = line.rstrip("\r\n")
        if fence_character is not None:
            closing = re.match(
                rf"^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$",
                logical_line,
            )
            output.append(_mask_span(line, 0, len(line)))
            if closing:
                fence_character = None
                fence_length = 0
            continue

        opening = _FENCE_OPEN.match(logical_line)
        if opening:
            fence = opening.group(1)
            fence_character = fence[0]
            fence_length = len(fence)
            output.append(_mask_span(line, 0, len(line)))
            continue

        if line.startswith("\t") or line.startswith("    "):
            output.append(_mask_span(line, 0, len(line)))
            continue
        output.append(line)

    return _mask_inline_code("".join(output))


def _find_closing(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 1
    index = start + 1
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _markdown_unescape(value: str) -> str:
    return re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", value)


def _destination(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("<"):
        closing = value.find(">", 1)
        if closing < 0:
            raise DocumentLinkError("unterminated angle-bracket link target")
        target = value[1:closing]
        remainder = value[closing + 1 :].strip()
    else:
        index = 0
        parenthesis_depth = 0
        while index < len(value):
            character = value[index]
            if character == "\\":
                index += 2
                continue
            if character == "(":
                parenthesis_depth += 1
            elif character == ")" and parenthesis_depth:
                parenthesis_depth -= 1
            elif character.isspace() and parenthesis_depth == 0:
                break
            index += 1
        target = value[:index]
        remainder = value[index:].strip()

    if remainder and not (
        (remainder.startswith('"') and remainder.endswith('"'))
        or (remainder.startswith("'") and remainder.endswith("'"))
        or (remainder.startswith("(") and remainder.endswith(")"))
    ):
        raise DocumentLinkError("invalid Markdown link title")
    return html.unescape(_markdown_unescape(target))


def _reference_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _inline_links(
    document: str,
    references: dict[str, str],
) -> Iterable[tuple[bool, str]]:
    index = 0
    while index < len(document):
        if document[index] != "[" or _is_escaped(document, index):
            index += 1
            continue

        label_end = _find_closing(document, index, "[", "]")
        if label_end is None:
            index += 1
            continue
        image = index > 0 and document[index - 1] == "!" and not _is_escaped(document, index - 1)
        following = label_end + 1
        if following < len(document) and document[following] == "(":
            target_end = _find_closing(document, following, "(", ")")
            if target_end is None:
                raise DocumentLinkError("unterminated Markdown link target")
            yield image, _destination(document[following + 1 : target_end])
            index = target_end + 1
            continue

        if following < len(document) and document[following] == "[":
            reference_end = _find_closing(document, following, "[", "]")
            if reference_end is None:
                raise DocumentLinkError("unterminated Markdown reference link")
            reference = document[following + 1 : reference_end]
            if not reference:
                reference = document[index + 1 : label_end]
            key = _reference_key(reference)
            if key not in references:
                raise DocumentLinkError(f"undefined Markdown link reference: {reference}")
            yield image, references[key]
            index = reference_end + 1
            continue
        shortcut_key = _reference_key(document[index + 1 : label_end])
        if shortcut_key in references:
            yield image, references[shortcut_key]
        index = label_end + 1


def _links(document: str) -> Iterable[tuple[bool, str]]:
    visible = _markdown_without_code(document)
    references: dict[str, str] = {}
    for line in visible.splitlines():
        match = _REFERENCE_DEFINITION.match(line)
        if not match:
            continue
        key = _reference_key(match.group(1))
        if key in references:
            raise DocumentLinkError(f"duplicate Markdown link reference: {match.group(1)}")
        references[key] = _destination(match.group(2))

    yield from _inline_links(visible, references)

    for match in _AUTOLINK.finditer(visible):
        target = html.unescape(match.group(1))
        if ":" in target or target.startswith(("/", "\\")):
            yield False, target

    for match in _HTML_LINK.finditer(visible):
        tag = match.group(1).casefold()
        target = next(value for value in match.groups()[1:] if value is not None)
        yield tag == "img", html.unescape(target)


def _decode_target(target: str) -> str:
    decoded = target
    for _ in range(6):
        invalid_escape = re.search(r"%(?![0-9A-Fa-f]{2})", decoded)
        if invalid_escape:
            raise DocumentLinkError("invalid percent escape in link target")
        try:
            next_value = unquote(decoded, encoding="utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise DocumentLinkError("link target is not valid UTF-8") from error
        if next_value == decoded:
            return decoded
        decoded = next_value
    if _VALID_PERCENT_ESCAPE.search(decoded):
        raise DocumentLinkError("link target uses excessive percent encoding")
    return decoded


def _display_path(path: Path) -> str:
    return path.as_posix() or "."


def _ensure_no_symlink(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise DocumentLinkError(f"symlink targets are forbidden: {_display_path(relative)}")


def _ensure_exact_case(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        try:
            names = {entry.name for entry in current.iterdir()}
        except FileNotFoundError:
            return
        except NotADirectoryError as error:
            raise DocumentLinkError(
                f"link traverses a non-directory: {_display_path(relative)}"
            ) from error
        if part not in names:
            aliases = sorted(name for name in names if name.casefold() == part.casefold())
            if aliases:
                raise DocumentLinkError(
                    f"case alias is forbidden: {_display_path(relative)} (found {aliases[0]})"
                )
            return
        current /= part


def _repo_relative_candidate(root: Path, document: Path, target_path: str) -> Path:
    if "\\" in target_path:
        raise DocumentLinkError("backslashes are forbidden in local link targets")
    if target_path.startswith("/") or PureWindowsPath(target_path).is_absolute():
        raise DocumentLinkError("absolute link targets are forbidden")

    pure_path = PurePosixPath(target_path)
    candidate = Path(os.path.abspath(root / document.parent / Path(*pure_path.parts)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise DocumentLinkError("link target escapes the repository") from error
    if tuple(part.casefold() for part in relative.parts[:2]) == ("docs", "superpowers"):
        raise DocumentLinkError("public documents cannot link to docs/superpowers")
    return relative


def _validate_repo_path(
    root: Path,
    relative: Path,
    *,
    require_file: bool,
    allow_missing: bool,
) -> bool:
    _ensure_no_symlink(root, relative)
    _ensure_exact_case(root, relative)
    candidate = root / relative
    if not candidate.exists():
        if allow_missing:
            return False
        raise DocumentLinkError(f"broken local link: {_display_path(relative)}")
    if require_file and not candidate.is_file():
        raise DocumentLinkError(f"expected a regular file: {_display_path(relative)}")
    return True


def _normalize_exception(root: Path, value: str | Path) -> str:
    raw = value.as_posix() if isinstance(value, Path) else value
    decoded = _decode_target(raw)
    split = urlsplit(decoded)
    if split.scheme or split.netloc or split.query or split.fragment or not split.path:
        raise DocumentLinkError(f"invalid allowed-missing exception: {raw}")
    relative = _repo_relative_candidate(root, Path("."), split.path)
    _validate_repo_path(root, relative, require_file=False, allow_missing=True)
    return relative.as_posix()


def _validate_target(
    root: Path,
    document: Path,
    target: str,
    *,
    image: bool,
    allowed_missing: frozenset[str],
    used_exceptions: set[str],
) -> None:
    decoded = _decode_target(target.strip())
    if any(character in _CONTROL_CHARACTERS for character in decoded):
        raise DocumentLinkError("control character in link target")

    try:
        split = urlsplit(decoded)
    except ValueError as error:
        raise DocumentLinkError("malformed link target") from error
    if split.scheme:
        if split.scheme.casefold() != "https" or not split.netloc:
            raise DocumentLinkError("external links must use an absolute https URL")
        if image:
            raise DocumentLinkError("external images are forbidden")
        return
    if split.netloc:
        raise DocumentLinkError("scheme-relative links are forbidden")
    if not split.path:
        return

    relative = _repo_relative_candidate(root, document, split.path)
    relative_name = relative.as_posix()
    exists = _validate_repo_path(
        root,
        relative,
        require_file=False,
        allow_missing=relative_name in allowed_missing,
    )
    if not exists:
        used_exceptions.add(relative_name)


def verify_document_links(
    root: Path,
    documents: Iterable[Path] = PUBLIC_DOCUMENTS,
    allowed_missing: Iterable[str | Path] = TEMPORARY_MISSING_ASSETS,
) -> None:
    """Validate public Markdown links without opening local or external targets."""

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise DocumentLinkError("repository root must be a real directory, not a symlink")
    root = root.resolve(strict=True)

    normalized_exceptions = frozenset(
        _normalize_exception(root, value) for value in allowed_missing
    )
    used_exceptions: set[str] = set()

    for document_value in documents:
        raw_document = Path(document_value)
        if raw_document.is_absolute():
            raise DocumentLinkError("public document paths must be repository-relative")
        document = _repo_relative_candidate(root, Path("."), raw_document.as_posix())
        _validate_repo_path(root, document, require_file=True, allow_missing=False)
        try:
            content = (root / document).read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise DocumentLinkError(
                f"public document is not valid UTF-8: {_display_path(document)}"
            ) from error
        for image, target in _links(content):
            _validate_target(
                root,
                document,
                target,
                image=image,
                allowed_missing=normalized_exceptions,
                used_exceptions=used_exceptions,
            )

    unused = sorted(normalized_exceptions - used_exceptions)
    if unused:
        raise DocumentLinkError(f"unused allowed-missing exceptions: {', '.join(unused)}")


def main() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    verify_document_links(repository_root)
    print(f"verified links in {len(PUBLIC_DOCUMENTS)} public documents")


if __name__ == "__main__":
    try:
        main()
    except DocumentLinkError as error:
        raise SystemExit(f"document link verification failed: {error}") from None
