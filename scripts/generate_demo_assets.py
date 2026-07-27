from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import urlopen

from project_memory_hub.demo.privacy import (
    build_privacy_policy,
    canonical_dom_receipt,
    scan_asset_directory,
    scan_dom_receipt,
    scan_text,
)
from project_memory_hub.demo.runtime import (
    DEMO_MARKER_DOCUMENT,
    OUTPUT_MARKER_NAME,
    DemoWorkspace,
    prepare_demo_workspace,
)
from project_memory_hub.demo.seed import (
    CODEX_NAMESPACE,
    DEMO_LABEL,
    PROJECT_ID,
    build_demo_container,
    seed_demo_database,
)
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.security.web import LocalAccessToken


VIEWPORT = {"width": 1440, "height": 1000}
SOCIAL_PREVIEW_SIZE = (1280, 640)
SCREENSHOT_NAMES = (
    "screenshots/overview.png",
    "screenshots/sources.png",
    "screenshots/memories.png",
)
SVG_NAMES = (
    "diagrams/local-data-flow.svg",
    "diagrams/strict-model-isolation.svg",
    "diagrams/approval-gated-improvement.svg",
)
MANIFEST_NAME = "demo-manifest.json"
GENERATED_ASSET_NAMES = frozenset(
    (*SCREENSHOT_NAMES, *SVG_NAMES, "social-preview.png", MANIFEST_NAME)
)
PUBLIC_ASSET_NAMES = GENERATED_ASSET_NAMES
_ALLOWED_DOCUMENT_ROUTES = frozenset({"/", "/sources", "/memories"})
_EXPECTED_ROUTE_RECEIPT = ("/", "/sources", "/memories")
_MAX_DEFAULT_RUNTIME_FILE_BYTES = 512 * 1024 * 1024
_MAX_DEFAULT_RUNTIME_TOTAL_BYTES = 1024 * 1024 * 1024
_MAX_DEFAULT_RUNTIME_ENTRIES = 4096
_MAX_DEFAULT_RUNTIME_DEPTH = 16

_FIXED_CLOCK_SCRIPT = """
(() => {
  const fixed = 1784376000000;
  const NativeDate = Date;
  class FixedDate extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
    static now() { return fixed; }
  }
  FixedDate.parse = NativeDate.parse;
  FixedDate.UTC = NativeDate.UTC;
  window.Date = FixedDate;
  try { localStorage.setItem("project-memory-hub-language", "en"); } catch (_) {}
})();
"""

_VISIBLE_DOM_SCRIPT = r"""
() => {
  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (element.hasAttribute("hidden")) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const bounds = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    return {
      bottom: Math.round(rect.bottom),
      left: Math.round(rect.left),
      right: Math.round(rect.right),
      top: Math.round(rect.top),
    };
  };
  const attributes = [];
  for (const element of document.querySelectorAll("[aria-label],[title],[alt],input,select,textarea")) {
    if (!visible(element)) continue;
    const type = normalize(element.getAttribute("type")).toLowerCase();
    if (type === "hidden" || type === "password") continue;
    for (const name of ["aria-label", "title", "alt"]) {
      const value = normalize(element.getAttribute(name));
      if (value) attributes.push(value);
    }
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
      const value = normalize(element.value);
      if (value) attributes.push(value);
    }
    if (element instanceof HTMLSelectElement) {
      const value = normalize(element.selectedOptions[0]?.textContent);
      if (value) attributes.push(value);
    }
  }
  const main = document.querySelector("main#content");
  const heading = main?.querySelector("h1");
  const currentNavigation = [...document.querySelectorAll("nav a[aria-current='page']")]
    .filter(visible)
    .map((element) => ({
      aria_current: normalize(element.getAttribute("aria-current")),
      href: new URL(element.href, location.href).pathname,
      text: normalize(element.textContent),
    }));
  const overlays = [...document.querySelectorAll("#project-memory-hub-demo-overlay")].filter(visible);
  const overlay = overlays.length === 1 ? overlays[0] : null;
  const overlayRect = overlay?.getBoundingClientRect();
  let uiContract = null;
  if (location.pathname === "/") {
    const steps = [...document.querySelectorAll("section[data-next-safe-step]")].filter(visible);
    const step = steps.length === 1 ? steps[0] : null;
    const stepCommand = step?.querySelector("code") || null;
    uiContract = {
      kind: "overview",
      next_safe_step: {
        bounds: bounds(step),
        command: normalize(stepCommand?.textContent),
        command_bounds: bounds(stepCommand),
        command_visible: visible(stepCommand),
        count: steps.length,
        kind: normalize(step?.getAttribute("data-next-safe-step")),
      },
      visible_recorded_operations_count: [...document.querySelectorAll("section.ledger")]
        .filter(visible).length,
    };
  } else if (location.pathname === "/sources") {
    const collections = [...document.querySelectorAll("[data-source-collection]")]
      .filter(visible)
      .map((collection) => ({
        heading: normalize(collection.querySelector("h2")?.textContent),
        heading_bounds: bounds(collection.querySelector("h2")),
        role: normalize(collection.getAttribute("data-source-collection")),
        sources: [...collection.querySelectorAll("tr[data-source]")]
          .filter(visible)
          .map((row) => {
            const behaviorImport = row.querySelector(".status.locked");
            return {
              behavior_import: normalize(behaviorImport?.textContent),
              behavior_import_bounds: bounds(behaviorImport),
              behavior_import_visible: visible(behaviorImport),
              bounds: bounds(row),
              source_agent: normalize(row.getAttribute("data-source")),
            };
          }),
      }));
    uiContract = {kind: "sources", collections};
  } else if (location.pathname === "/memories") {
    const guidance = [...document.querySelectorAll("section.guidance-panel")].filter(visible);
    const selectedGuidance = guidance.length === 1 ? guidance[0] : null;
    const guidanceCommand = selectedGuidance?.querySelector("code") || null;
    const projectSelect = document.querySelector('select[name="project_id"]');
    const sourceSelect = document.querySelector('select[name="source_agent"]');
    const modelInputs = [...document.querySelectorAll('input[name="model_id"]')].filter(visible);
    const selectedModel = modelInputs.length === 1 ? modelInputs[0] : null;
    const memoryCards = [...document.querySelectorAll("article.memory-card")].filter(visible);
    const behaviorCards = [...document.querySelectorAll(
      "section.memory-list:not([aria-labelledby]) article.memory-card"
    )].filter(visible);
    uiContract = {
      guidance_bounds: bounds(selectedGuidance),
      guidance_command: normalize(guidanceCommand?.textContent),
      guidance_command_bounds: bounds(guidanceCommand),
      guidance_command_visible: visible(guidanceCommand),
      guidance_count: guidance.length,
      kind: "memories",
      memory_card_bounds: memoryCards.map(bounds),
      memory_card_count: memoryCards.length,
      rendered_behavior_namespaces: behaviorCards
        .map((card) => normalize(card.querySelector("dl dd")?.textContent))
        .filter(Boolean),
      rendered_model_ids: modelInputs.map((input) => normalize(input.value)).filter(Boolean),
      selected_model_bounds: bounds(selectedModel),
      selected_model_id: normalize(selectedModel?.value),
      selected_project_bounds: bounds(projectSelect),
      selected_project_id: normalize(projectSelect?.value),
      selected_source_agent: normalize(sourceSelect?.value),
      selected_source_bounds: bounds(sourceSelect),
    };
  }
  return {
    attributes: [...new Set(attributes)].sort(),
    current_navigation: {
      count: currentNavigation.length,
      items: currentNavigation,
    },
    demo_overlay: overlay && overlayRect ? {
      aria_label: normalize(overlay.getAttribute("aria-label")),
      bounds: {
        bottom: Math.round(overlayRect.bottom),
        left: Math.round(overlayRect.left),
        right: Math.round(overlayRect.right),
        top: Math.round(overlayRect.top),
      },
      count: overlays.length,
      id: overlay.id,
      position: getComputedStyle(overlay).position,
      role: normalize(overlay.getAttribute("role")),
      text: normalize(overlay.textContent),
    } : null,
    heading: normalize(heading?.textContent),
    main_content_count: document.querySelectorAll("main#content").length,
    route: location.pathname,
    title: normalize(document.title),
    ui_contract: uiContract,
    visible_text: [normalize(document.body.innerText)],
  };
}
"""

_OVERLAY_SCRIPT = """
() => {
  let overlay = document.getElementById("project-memory-hub-demo-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "project-memory-hub-demo-overlay";
    overlay.setAttribute("role", "note");
    overlay.setAttribute("aria-label", "Synthetic demonstration data");
    overlay.textContent = "DEMO DATA";
    Object.assign(overlay.style, {
      position: "fixed",
      right: "24px",
      bottom: "20px",
      zIndex: "2147483647",
      padding: "10px 14px",
      border: "2px solid #155d3b",
      background: "#edf7ef",
      color: "#143526",
      font: "700 14px/1 system-ui, sans-serif",
      letterSpacing: "0.12em",
      boxShadow: "4px 4px 0 rgba(20, 53, 38, 0.18)",
    });
    document.body.appendChild(overlay);
  }
  const rect = overlay.getBoundingClientRect();
  return {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom};
}
"""

_OVERVIEW_CAPTURE_STYLE = """
section.ledger { display: none; }
footer { display: none; }
"""

_SOURCES_CAPTURE_STYLE = """
body { margin: 6px auto; }
.masthead { padding: 12px 24px 10px; }
.masthead-tools { gap: 6px; }
.rail a { padding-block: 7px; }
.page { padding: 16px 24px; }
.page-heading { padding-bottom: 12px; }
.page-heading h1 { font-size: 3.2rem; }
.page-heading .kicker { font-size: 0.9rem; }
.lede-copy { max-width: none; margin: 10px 0; font-size: 0.9rem; }
[data-source-collection].ledger { margin-top: 10px; padding: 10px; }
[data-source-collection] h2 { margin-bottom: 6px; font-size: 1.05rem; }
[data-source-collection] .table-wrap { margin-top: 6px; }
[data-source-collection] th,
[data-source-collection] td { padding: 5px 7px; font-size: 0.68rem; line-height: 1.2; }
[data-source-collection] .status { padding: 2px 4px; font-size: 0.55rem; line-height: 1.15; }
[data-source-collection] .probe-warnings { margin-top: 3px; }
[data-source-collection] .warning-codes { gap: 1px; }
footer { display: none; }
"""

_MEMORIES_CAPTURE_STYLE = """
body { margin: 6px auto; }
.masthead { padding: 12px 24px 10px; }
.masthead-tools { gap: 6px; }
.rail a { padding-block: 7px; }
.page { padding: 16px 24px; }
.page-heading { padding-bottom: 12px; }
.page-heading h1 { font-size: 3.2rem; }
.guidance-panel { margin-top: 10px; padding: 12px 18px; }
.guidance-panel h2 { margin-bottom: 5px; font-size: 1.1rem; }
.guidance-panel p { margin-block: 5px; }
.guidance-panel .command-block { margin: 6px 0; }
.guidance-panel .command-block code { padding-block: 6px; }
.filter-panel { margin: 10px 0; }
.memory-list { gap: 10px; margin-top: 10px; }
.memory-list h2 { margin-bottom: 2px; font-size: 1.1rem; }
.memory-card { padding: 10px 16px; }
.memory-card header { margin-bottom: 5px; }
.memory-card p { margin-block: 5px; }
.memory-card dl > div { padding-block: 3px; }
.memory-card .actions { display: none; }
section[aria-labelledby="shared-facts-heading"] .memory-card:not(:first-of-type),
section.memory-list:not([aria-labelledby]) .memory-card:not(:first-of-type) { display: none; }
footer { display: none; }
"""

_CAPTURE_STYLES = {
    "/static/demo-overview-capture.css": _OVERVIEW_CAPTURE_STYLE,
    "/static/demo-sources-capture.css": _SOURCES_CAPTURE_STYLE,
    "/static/demo-memories-capture.css": _MEMORIES_CAPTURE_STYLE,
}

_CAPTURE_STYLE_PATHS = {
    "screenshots/overview.png": "/static/demo-overview-capture.css",
    "screenshots/sources.png": "/static/demo-sources-capture.css",
    "screenshots/memories.png": "/static/demo-memories-capture.css",
}


@dataclass(slots=True)
class DemoRoutePolicy:
    base_url: str
    _routes: list[str] = field(default_factory=list, init=False, repr=False)
    _violations: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.hostname
            not in {
                "127.0.0.1",
                "localhost",
            }
        ):
            raise ValueError("demo base URL rejected")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(self._routes)

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(self._violations)

    def authorize(self, url: str, *, resource_type: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != self.base_url:
            self._note_violation("external_origin_blocked")
            return False
        path = parsed.path
        for _index in range(3):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        if not path.startswith("/") or "\\" in path or ".." in Path(path).parts:
            self._note_violation("route_path_rejected")
            return False
        if resource_type == "document":
            if path == "/projects" or path.startswith("/projects/"):
                self._note_violation("projects_route_blocked")
                return False
            if path not in _ALLOWED_DOCUMENT_ROUTES:
                self._note_violation("document_route_blocked")
                return False
            if path not in self._routes:
                self._routes.append(path)
            return True
        if path.startswith("/static/"):
            return True
        if path == "/favicon.ico":
            return False
        self._note_violation("subresource_route_blocked")
        return False

    def _note_violation(self, code: str) -> None:
        if code not in self._violations:
            self._violations.append(code)


def default_runtime_snapshot() -> tuple[tuple[str, str, str], ...]:
    """Return a bounded physical receipt without opening or mutating live SQLite."""
    root = RuntimePaths.for_root().root
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return ((".", "missing", ""),)
    except OSError as error:
        raise RuntimeError("default runtime snapshot rejected") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("default runtime snapshot rejected")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(root, flags)
        opened = os.fstat(descriptor)
        if _snapshot_identity(opened) != _snapshot_identity(root_metadata):
            raise RuntimeError("default runtime snapshot rejected")
        receipt: list[tuple[str, str, str]] = [(".", _snapshot_metadata("directory", opened), "")]
        budget = {"entries": 1, "bytes": 0}
        directory_identities = {".": _snapshot_identity(opened)}
        file_identities: dict[str, tuple[int, ...]] = {}
        symlink_identities: dict[str, tuple[tuple[int, ...], str]] = {}
        _snapshot_runtime_directory(
            descriptor,
            prefix="",
            depth=0,
            receipt=receipt,
            budget=budget,
            directory_identities=directory_identities,
            file_identities=file_identities,
            symlink_identities=symlink_identities,
        )
        _revalidate_runtime_snapshot(
            descriptor,
            directory_identities=directory_identities,
            file_identities=file_identities,
            symlink_identities=symlink_identities,
        )
        after = os.fstat(descriptor)
        if _snapshot_identity(after) != _snapshot_identity(opened):
            raise RuntimeError("default runtime snapshot rejected")
        live_descriptor = os.open(root, flags)
        try:
            if _snapshot_identity(os.fstat(live_descriptor)) != _snapshot_identity(opened):
                raise RuntimeError("default runtime snapshot rejected")
        finally:
            os.close(live_descriptor)
        return tuple(receipt)
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError("default runtime snapshot rejected") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _snapshot_runtime_directory(
    descriptor: int,
    *,
    prefix: str,
    depth: int,
    receipt: list[tuple[str, str, str]],
    budget: dict[str, int],
    directory_identities: dict[str, tuple[int, ...]],
    file_identities: dict[str, tuple[int, ...]],
    symlink_identities: dict[str, tuple[tuple[int, ...], str]],
) -> None:
    if depth >= _MAX_DEFAULT_RUNTIME_DEPTH:
        raise RuntimeError("default runtime snapshot rejected")
    before = os.fstat(descriptor)
    names_list: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            names_list.append(entry.name)
            if budget["entries"] + len(names_list) > _MAX_DEFAULT_RUNTIME_ENTRIES:
                raise RuntimeError("default runtime snapshot rejected")
    names = tuple(sorted(names_list))
    for name in names:
        if name in {".", ".."} or "/" in name or "\x00" in name:
            raise RuntimeError("default runtime snapshot rejected")
        budget["entries"] += 1
        if budget["entries"] > _MAX_DEFAULT_RUNTIME_ENTRIES:
            raise RuntimeError("default runtime snapshot rejected")
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                if _snapshot_identity(opened) != _snapshot_identity(metadata):
                    raise RuntimeError("default runtime snapshot rejected")
                receipt.append((relative, _snapshot_metadata("directory", opened), ""))
                directory_identities[relative] = _snapshot_identity(opened)
                _snapshot_runtime_directory(
                    child,
                    prefix=relative,
                    depth=depth + 1,
                    receipt=receipt,
                    budget=budget,
                    directory_identities=directory_identities,
                    file_identities=file_identities,
                    symlink_identities=symlink_identities,
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            document_digest = _snapshot_runtime_file(
                descriptor,
                name,
                metadata,
                budget=budget,
            )
            receipt.append((relative, _snapshot_metadata("file", metadata), document_digest))
            file_identities[relative] = _snapshot_identity(metadata)
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=descriptor)
            after_link = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _snapshot_identity(after_link) != _snapshot_identity(metadata):
                raise RuntimeError("default runtime snapshot rejected")
            receipt.append(
                (
                    relative,
                    _snapshot_metadata("symlink", metadata),
                    hashlib.sha256(os.fsencode(target)).hexdigest(),
                )
            )
            symlink_identities[relative] = (
                _snapshot_identity(metadata),
                hashlib.sha256(os.fsencode(target)).hexdigest(),
            )
        else:
            raise RuntimeError("default runtime snapshot rejected")
    with os.scandir(descriptor) as entries:
        if tuple(sorted(entry.name for entry in entries)) != names:
            raise RuntimeError("default runtime snapshot rejected")
    if _snapshot_identity(os.fstat(descriptor)) != _snapshot_identity(before):
        raise RuntimeError("default runtime snapshot rejected")


def _revalidate_runtime_snapshot(
    root_descriptor: int,
    *,
    directory_identities: dict[str, tuple[int, ...]],
    file_identities: dict[str, tuple[int, ...]],
    symlink_identities: dict[str, tuple[tuple[int, ...], str]],
) -> None:
    for relative, expected in sorted(directory_identities.items()):
        if relative == ".":
            actual = _snapshot_identity(os.fstat(root_descriptor))
        else:
            descriptor = _open_snapshot_directory(root_descriptor, relative)
            try:
                actual = _snapshot_identity(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        if actual != expected:
            raise RuntimeError("default runtime snapshot rejected")

    for relative, expected in sorted(file_identities.items()):
        parent, leaf_name = _open_snapshot_parent(root_descriptor, relative)
        descriptor = -1
        try:
            descriptor = os.open(
                leaf_name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            actual = _snapshot_identity(os.fstat(descriptor))
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)
        if actual != expected:
            raise RuntimeError("default runtime snapshot rejected")

    for relative, (expected_identity, expected_target) in sorted(symlink_identities.items()):
        parent, leaf_name = _open_snapshot_parent(root_descriptor, relative)
        try:
            metadata = os.stat(leaf_name, dir_fd=parent, follow_symlinks=False)
            target_digest = hashlib.sha256(
                os.fsencode(os.readlink(leaf_name, dir_fd=parent))
            ).hexdigest()
        finally:
            os.close(parent)
        if _snapshot_identity(metadata) != expected_identity or target_digest != expected_target:
            raise RuntimeError("default runtime snapshot rejected")


def _open_snapshot_directory(root_descriptor: int, relative: str) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for component in PurePosixPath(relative).parts:
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_snapshot_parent(root_descriptor: int, relative: str) -> tuple[int, str]:
    components = PurePosixPath(relative).parts
    descriptor = os.dup(root_descriptor)
    try:
        for component in components[:-1]:
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor, components[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _snapshot_runtime_file(
    directory_descriptor: int,
    name: str,
    metadata: os.stat_result,
    *,
    budget: dict[str, int],
) -> str:
    if metadata.st_size > _MAX_DEFAULT_RUNTIME_FILE_BYTES:
        raise RuntimeError("default runtime snapshot rejected")
    budget["bytes"] += metadata.st_size
    if budget["bytes"] > _MAX_DEFAULT_RUNTIME_TOTAL_BYTES:
        raise RuntimeError("default runtime snapshot rejected")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if _snapshot_identity(opened) != _snapshot_identity(metadata):
            raise RuntimeError("default runtime snapshot rejected")
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeError("default runtime snapshot rejected")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RuntimeError("default runtime snapshot rejected")
        if _snapshot_identity(os.fstat(descriptor)) != _snapshot_identity(opened):
            raise RuntimeError("default runtime snapshot rejected")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _snapshot_metadata(kind: str, metadata: os.stat_result) -> str:
    return (
        f"{kind}:mode={stat.S_IMODE(metadata.st_mode):04o}:uid={metadata.st_uid}:"
        f"gid={metadata.st_gid}:nlink={metadata.st_nlink}:size={metadata.st_size}"
    )


def _snapshot_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _scoped_output_dir(output_dir: Path, repository_root: Path) -> Path:
    repository = Path(repository_root).expanduser()
    if not repository.is_absolute():
        raise ValueError("demo output rejected")
    repository = Path(os.path.abspath(repository))
    selected = Path(output_dir).expanduser()
    if selected.is_absolute():
        return Path(os.path.abspath(selected))
    if (
        not selected.parts
        or any(part in {"", ".", ".."} or "\\" in part for part in selected.parts)
        or any(ord(character) < 32 for character in str(selected))
    ):
        raise ValueError("demo output rejected")
    scoped = Path(os.path.abspath(repository / selected))
    if scoped == repository or repository not in scoped.parents:
        raise ValueError("demo output rejected")
    return scoped


def generate_demo_assets(
    *,
    runtime_dir: Path,
    output_dir: Path,
    repository_root: Path,
    denylist_path: Path | None = None,
) -> Path:
    repository = Path(repository_root).expanduser()
    if not repository.is_absolute():
        raise ValueError("demo repository rejected")
    repository = Path(os.path.abspath(repository))
    selected_output = _scoped_output_dir(Path(output_dir), repository)
    before_default = default_runtime_snapshot()
    workspace = prepare_demo_workspace(
        runtime_dir=Path(runtime_dir),
        output_dir=selected_output,
        repository_root=repository,
        allowed_output_names=GENERATED_ASSET_NAMES,
    )
    completed = False
    try:
        policy = build_privacy_policy(
            repository_root=repository,
            denylist_path=denylist_path,
        )
        inventory = seed_demo_database(workspace)
        inventory_text = inventory.to_json_bytes().decode("utf-8")
        scan_text(inventory_text, policy, asset_name="seed-inventory.json")
        token = LocalAccessToken.load_or_create(workspace.paths)
        asset_stage = workspace.paths.logs / "asset-stage"
        asset_stage.mkdir(mode=0o700, parents=True, exist_ok=False)
        _write_new_private_file(asset_stage / OUTPUT_MARKER_NAME, DEMO_MARKER_DOCUMENT)

        diagrams = _diagram_documents()
        for name, document in diagrams.items():
            _write_new_private_file(asset_stage / name, document)

        social_preview = _social_preview_png()
        _write_new_private_file(asset_stage / "social-preview.png", social_preview)

        screenshot_entries: list[dict[str, object]] = []
        with _demo_server(workspace) as base_url:
            screenshot_entries, routes = _capture_screenshots(
                base_url=base_url,
                token=token,
                workspace=workspace,
                asset_stage=asset_stage,
                policy=policy,
            )
        if routes != _EXPECTED_ROUTE_RECEIPT:
            raise RuntimeError("demo route receipt rejected")

        after_default = default_runtime_snapshot()
        if after_default != before_default:
            raise RuntimeError("default runtime changed during demo generation")

        asset_entries: list[dict[str, object]] = [*screenshot_entries]
        for name in SVG_NAMES:
            document = diagrams[name]
            asset_entries.append(
                {
                    "kind": "diagram",
                    "path": name,
                    "sha256": hashlib.sha256(document).hexdigest(),
                }
            )
        asset_entries.append(
            {
                "height": SOCIAL_PREVIEW_SIZE[1],
                "kind": "social_preview",
                "path": "social-preview.png",
                "sha256": hashlib.sha256(social_preview).hexdigest(),
                "width": SOCIAL_PREVIEW_SIZE[0],
            }
        )
        manifest = {
            "assets": asset_entries,
            "default_runtime_unchanged": True,
            "demo_label": DEMO_LABEL,
            "generator": "project-memory-hub-demo-assets",
            "render": {
                "locale": "en-US",
                "reduced_motion": "reduce",
                "timezone": "UTC",
                "viewport": VIEWPORT,
            },
            "routes": list(routes),
            "schema_version": 1,
            "seed": json.loads(inventory.to_json_bytes()),
            "seed_version": inventory.seed_version,
        }
        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        _write_new_private_file(asset_stage / MANIFEST_NAME, manifest_bytes)
        workspace.validate_runtime()
        scan_asset_directory(asset_stage, policy)
        documents = {name: _read_private_file(asset_stage / name) for name in GENERATED_ASSET_NAMES}
        workspace.validate_runtime()
        workspace.publish_output_files(documents)
        workspace.finalize_output()
        completed = True
        return workspace.output_dir / MANIFEST_NAME
    finally:
        try:
            if not completed:
                workspace.cleanup_incomplete_output()
        finally:
            workspace.cleanup_runtime()


@contextmanager
def _demo_server(workspace: DemoWorkspace) -> Iterator[str]:
    import uvicorn

    from project_memory_hub.web.app import create_app

    workspace.validate_runtime()
    failures: list[BaseException] = []
    listener: Any | None = None
    container: Any | None = None
    server: Any | None = None
    thread: threading.Thread | None = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = int(listener.getsockname()[1])
        container = build_demo_container(workspace)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(container),
                access_log=False,
                log_level="critical",
            )
        )

        def serve() -> None:
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve, name="pmh-demo-server")
        thread.start()
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        ready = False
        while time.monotonic() < deadline:
            if failures or not thread.is_alive():
                raise RuntimeError("demo server exited before readiness")
            try:
                with urlopen(f"{base_url}/", timeout=0.25):
                    pass
            except HTTPError as error:
                error.close()
                if error.code == 401:
                    ready = True
                    break
            except (TimeoutError, URLError):
                pass
            time.sleep(0.05)
        if not ready:
            raise RuntimeError("demo server readiness failed")
        yield base_url
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=8)
            if thread.is_alive() and server is not None:
                server.force_exit = True
                thread.join(timeout=3)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if container is not None:
            container.close()
        if thread is not None and thread.is_alive():
            raise RuntimeError("demo server shutdown failed")
        if failures:
            raise RuntimeError("demo server failed") from failures[0]


def _capture_screenshots(
    *,
    base_url: str,
    token: str,
    workspace: DemoWorkspace,
    asset_stage: Path,
    policy: Any,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    route_policy = DemoRoutePolicy(base_url)
    entries: list[dict[str, object]] = []
    capture_directory = workspace.paths.logs / "screenshots"
    capture_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    targets = (
        ("screenshots/overview.png", "/"),
        ("screenshots/sources.png", "/sources"),
        (
            "screenshots/memories.png",
            "/memories?"
            + urlencode(
                {
                    "model_id": CODEX_NAMESPACE.model_id,
                    "project_id": str(PROJECT_ID),
                    "source_agent": CODEX_NAMESPACE.source_agent.value,
                }
            ),
        ),
    )

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            raise RuntimeError("verified Playwright Chromium unavailable") from error
        try:
            context = browser.new_context(
                viewport=VIEWPORT,
                screen=VIEWPORT,
                device_scale_factor=1,
                locale="en-US",
                timezone_id="UTC",
                color_scheme="light",
                reduced_motion="reduce",
                service_workers="block",
            )
            context.add_init_script(_FIXED_CLOCK_SCRIPT)

            def guard(route: Any) -> None:
                authorized = route_policy.authorize(
                    route.request.url,
                    resource_type=route.request.resource_type,
                )
                request_path = urlsplit(route.request.url).path
                if not authorized:
                    route.abort("blockedbyclient")
                elif request_path in _CAPTURE_STYLES:
                    route.fulfill(
                        status=200,
                        content_type="text/css; charset=utf-8",
                        body=_CAPTURE_STYLES[request_path],
                    )
                else:
                    route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            bootstrap_response = page.goto(
                f"{base_url}/?token={token}",
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            bootstrap = urlsplit(page.url)
            if (
                bootstrap_response is None
                or bootstrap_response.status != 200
                or bootstrap.path != "/"
                or bootstrap.query
            ):
                raise RuntimeError("demo browser bootstrap rejected")

            expected_headings = {
                "screenshots/overview.png": "Overview",
                "screenshots/sources.png": "Sources",
                "screenshots/memories.png": "Memories",
            }
            for name, target in targets:
                response = page.goto(
                    f"{base_url}{target}",
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
                if response is None or response.status != 200:
                    raise RuntimeError("demo page response rejected")
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.add_style_tag(url=f"{base_url}{_CAPTURE_STYLE_PATHS[name]}")
                page.evaluate("document.fonts ? document.fonts.ready : Promise.resolve()")
                page.evaluate("window.scrollTo(0, 0); document.activeElement?.blur();")
                box = page.evaluate(_OVERLAY_SCRIPT)
                if (
                    box["left"] < 0
                    or box["top"] < 0
                    or box["right"] > VIEWPORT["width"]
                    or box["bottom"] > VIEWPORT["height"]
                ):
                    raise RuntimeError("demo overlay outside viewport")
                before_receipt = page.evaluate(_VISIBLE_DOM_SCRIPT)
                if (
                    before_receipt.get("heading") != expected_headings[name]
                    or before_receipt.get("main_content_count") != 1
                    or before_receipt.get("route") != urlsplit(target).path
                ):
                    raise RuntimeError("demo page identity rejected")
                before_canonical, dom_hash = canonical_dom_receipt(before_receipt)
                scan_dom_receipt(
                    before_receipt,
                    policy,
                    asset_name=f"{name}.dom.json",
                )
                temporary = capture_directory / Path(name).name
                page.screenshot(
                    path=str(temporary),
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                    scale="css",
                )
                after_receipt = page.evaluate(_VISIBLE_DOM_SCRIPT)
                after_canonical, after_hash = canonical_dom_receipt(after_receipt)
                if after_canonical != before_canonical or after_hash != dom_hash:
                    raise RuntimeError("visible DOM changed during screenshot")
                sanitized = _sanitize_png(temporary)
                _write_new_private_file(asset_stage / name, sanitized)
                entries.append(
                    {
                        "dom_receipt": before_receipt,
                        "dom_sha256": dom_hash,
                        "height": VIEWPORT["height"],
                        "http_status": response.status,
                        "kind": "screenshot",
                        "path": name,
                        "sha256": hashlib.sha256(sanitized).hexdigest(),
                        "width": VIEWPORT["width"],
                    }
                )
            context.close()
        finally:
            browser.close()
    if route_policy.violations:
        raise RuntimeError("demo route policy violation")
    return entries, route_policy.routes


def _sanitize_png(source: Path) -> bytes:
    from PIL import Image

    with Image.open(source) as candidate:
        candidate.load()
        converted = candidate.convert("RGB")
        pixels = converted.tobytes()
        size = converted.size
    clean = Image.frombytes("RGB", size, pixels)
    buffer = io.BytesIO()
    clean.save(buffer, format="PNG", compress_level=9, optimize=False)
    return buffer.getvalue()


def _social_preview_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", SOCIAL_PREVIEW_SIZE, "#f4f0e7")
    draw = ImageDraw.Draw(image)
    eyebrow_font = ImageFont.load_default(size=18)
    title_font = ImageFont.load_default(size=64)
    tagline_font = ImageFont.load_default(size=31)
    feature_font = ImageFont.load_default(size=24)
    badge_font = ImageFont.load_default(size=20)

    draw.rectangle((54, 54, 1226, 586), fill="#fbfaf6", outline="#1f2a24", width=4)
    draw.rectangle((86, 88, 1194, 248), fill="#155d3b")
    draw.text(
        (112, 106),
        "LOCAL-FIRST  /  PRIVATE BY DEFAULT",
        fill="#dcece3",
        font=eyebrow_font,
    )
    draw.text((108, 140), "Project Memory Hub", fill="#ffffff", font=title_font)

    draw.text(
        (112, 286),
        "Durable context for AI-assisted coding.",
        fill="#17241e",
        font=tagline_font,
    )
    features = (
        "Exact project recall",
        "Strict model isolation",
        "Human-approved improvement",
    )
    for index, feature in enumerate(features):
        top = 362 + index * 48
        draw.rectangle((114, top + 8, 128, top + 22), fill="#155d3b")
        draw.text((148, top), feature, fill="#17241e", font=feature_font)

    draw.rectangle((928, 490, 1158, 548), fill="#edf7ef", outline="#155d3b", width=3)
    draw.text((974, 505), DEMO_LABEL, fill="#155d3b", font=badge_font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=9, optimize=False)
    return buffer.getvalue()


def _diagram_documents() -> dict[str, bytes]:
    documents = {
        "diagrams/local-data-flow.svg": _svg_document(
            "Local data flow",
            (
                ("Codex + ChatGPT", "Explicit local inputs"),
                ("Project Memory Hub", "Redact, isolate, store"),
                ("Scoped recall", "Exact project and model"),
            ),
        ),
        "diagrams/strict-model-isolation.svg": _svg_document(
            "Strict model isolation",
            (
                ("Codex namespace", "demo-codex-model-v1"),
                ("Isolation boundary", "No cross-model query"),
                ("ChatGPT namespace", "demo-chatgpt-model-v1"),
            ),
        ),
        "diagrams/approval-gated-improvement.svg": _svg_document(
            "Approval-gated improvement",
            (
                ("Analyze", "Create a bounded proposal"),
                ("Human approval", "Draft remains unapplied"),
                ("Verify or rollback", "No silent self-modification"),
            ),
        ),
    }
    return {name: document.encode("utf-8") for name, document in documents.items()}


def _svg_document(title: str, cards: tuple[tuple[str, str], ...]) -> str:
    card_documents = []
    for index, (heading, detail) in enumerate(cards):
        x = 70 + index * 410
        card_documents.append(
            f'  <rect x="{x}" y="180" width="330" height="210" rx="0" fill="#fbfaf6" stroke="#1f2a24" stroke-width="3"/>\n'
            f'  <text x="{x + 26}" y="246" class="heading">{heading}</text>\n'
            f'  <text x="{x + 26}" y="302" class="detail">{detail}</text>'
        )
        if index < len(cards) - 1:
            arrow_x = x + 346
            card_documents.append(
                f'  <path d="M {arrow_x} 285 H {arrow_x + 42}" stroke="#155d3b" stroke-width="5"/>\n'
                f'  <path d="M {arrow_x + 32} 273 L {arrow_x + 46} 285 L {arrow_x + 32} 297" fill="none" stroke="#155d3b" stroke-width="5"/>'
            )
    cards_text = "\n".join(card_documents)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1360" height="540" viewBox="0 0 1360 540">\n'
        '  <rect width="1360" height="540" fill="#f4f0e7"/>\n'
        "  <style>.title{font:700 34px system-ui,sans-serif;fill:#17241e}.heading{font:700 23px system-ui,sans-serif;fill:#17241e}.detail{font:400 18px system-ui,sans-serif;fill:#4d5a53}</style>\n"
        f'  <text x="70" y="92" class="title">{title}</text>\n'
        '  <text x="70" y="132" class="detail">Synthetic public illustration — DEMO DATA</text>\n'
        f"{cards_text}\n"
        "</svg>\n"
    )


def _write_new_private_file(path: Path, document: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise OSError("demo stage target rejected")
        written = 0
        while written < len(document):
            count = os.write(descriptor, document[written:])
            if count <= 0:
                raise OSError("short demo stage write")
            written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size > 64 * 1024 * 1024
        ):
            raise OSError("demo stage source rejected")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("short demo stage read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError("demo stage source grew")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise OSError("demo stage source changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate isolated synthetic public assets.")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--denylist", type=Path)
    arguments = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    generate_demo_assets(
        runtime_dir=arguments.runtime_dir,
        output_dir=arguments.output_dir,
        repository_root=repository_root,
        denylist_path=arguments.denylist,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
