from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "src/project_memory_hub/web/static/projects.js"


NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

class FakeClassList {
  constructor() {
    this.values = new Set();
    this.throwOnAdd = false;
  }

  add(value) {
    if (this.throwOnAdd) {
      throw new Error("synthetic class failure");
    }
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }
}

class FakeElement {
  constructor({ text = "", value = "", attributes = {} } = {}) {
    this.textContent = text;
    this.value = value;
    this.hidden = false;
    this.dataset = {};
    this.classList = new FakeClassList();
    this.throwOnSetAttribute = false;
    this._attributes = { ...attributes };
    this._listeners = new Map();
    this._selectors = new Map();
    this._selectorLists = new Map();
  }

  querySelector(selector) {
    return this._selectors.get(selector) || null;
  }

  querySelectorAll(selector) {
    return this._selectorLists.get(selector) || [];
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attributes, name)
      ? this._attributes[name]
      : null;
  }

  setAttribute(name, value) {
    if (this.throwOnSetAttribute) {
      throw new Error("synthetic attribute failure");
    }
    const rendered = String(value);
    this._attributes[name] = rendered;
    if (name === "data-count") {
      this.dataset.count = rendered;
    }
    if (name === "data-visible-count") {
      this.dataset.visibleCount = rendered;
    }
    if (name === "data-project-total-count") {
      this.dataset.projectTotalCount = rendered;
    }
  }

  addEventListener(type, listener) {
    const listeners = this._listeners.get(type) || [];
    listeners.push(listener);
    this._listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    const listeners = this._listeners.get(type) || [];
    this._listeners.set(
      type,
      listeners.filter((candidate) => candidate !== listener),
    );
  }

  dispatch(type) {
    const event = {
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
    };
    for (const listener of [...(this._listeners.get(type) || [])]) {
      listener(event);
    }
    return event;
  }
}

function makeCard(name, id, status) {
  const card = new FakeElement({
    attributes: {
      "data-project-name": name,
      "data-project-id": id,
      "data-project-status": status,
    },
  });
  card.projectName = name;
  card.projectId = id;
  return card;
}

function makeFixture(projects, options = {}) {
  const root = new FakeElement();
  const list = new FakeElement();
  const controls = new FakeElement();
  const search = new FakeElement({ value: "" });
  const status = new FakeElement({ value: "all" });
  const visibleCount = new FakeElement({ text: String(projects.length) });
  const totalCount = new FakeElement({
    text: String(projects.length),
    attributes: { "data-project-total-count": String(projects.length) },
  });
  const loadMore = new FakeElement();
  const empty = new FakeElement();
  controls.hidden = true;
  const cards = projects.map((project) =>
    makeCard(project.name, project.id, project.status),
  );

  root._attributes["data-project-page-size"] = "12";
  root._selectors.set("[data-project-controls]", controls);
  root._selectors.set("[data-project-search]", search);
  root._selectors.set("[data-project-status-filter]", status);
  root._selectors.set("[data-project-visible-count]", visibleCount);
  root._selectors.set("[data-project-total-count]", totalCount);
  root._selectors.set("[data-project-show-more]", loadMore);
  root._selectors.set("[data-project-no-results]", empty);
  root._selectors.set("[data-project-list]", list);
  list._selectorLists.set("[data-project-card]", cards);

  if (options.missingSelector) {
    root._selectors.delete(options.missingSelector);
  }
  if (options.classAddThrows) {
    root.classList.throwOnAdd = true;
  }

  const documentListeners = new Map();
  const document = {
    readyState: options.readyState || "complete",
    querySelector(selector) {
      if (options.queryThrows) {
        throw new Error("synthetic query failure");
      }
      return selector === "[data-project-browser]" && !options.missingRoot
        ? root
        : null;
    },
    addEventListener(type, listener) {
      const listeners = documentListeners.get(type) || [];
      listeners.push(listener);
      documentListeners.set(type, listeners);
    },
    fire(type) {
      for (const listener of [...(documentListeners.get(type) || [])]) {
        listener({ type });
      }
    },
  };

  return {
    root,
    list,
    controls,
    search,
    status,
    visibleCount,
    totalCount,
    loadMore,
    empty,
    cards,
    document,
  };
}

function runScript(fixture) {
  const context = { document: fixture.document };
  context.window = context;
  vm.runInNewContext(
    fs.readFileSync(process.argv[1], "utf8"),
    context,
    { filename: "projects.js" },
  );
}

function visibleIds(fixture) {
  return fixture.cards
    .filter((card) => !card.hidden)
    .map((card) => card.projectId);
}
"""


def _run_node(scenario: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to verify the Projects enhancement")
    result = subprocess.run(
        [node, "-e", NODE_HARNESS + scenario, str(SCRIPT_PATH)],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_projects_script_is_local_and_uses_safe_dom_operations() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    forbidden_tokens = (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
    )
    assert all(token not in script for token in forbidden_tokens)
    assert re.search(r"https?://", script) is None
    assert "textContent" in script
    assert "data-project-browser" in script
    assert "data-project-page-size" in script
    assert "data-project-controls" in script
    assert "data-project-search" in script
    assert "data-project-status-filter" in script
    assert "data-project-visible-count" in script
    assert "data-project-total-count" in script
    assert "data-project-show-more" in script
    assert "data-project-no-results" in script
    assert "data-project-name" in script
    assert "data-project-id" in script
    assert "data-project-status" in script


def test_projects_script_starts_with_twelve_and_loads_twelve_more() -> None:
    result = _run_node(
        r"""
const projects = Array.from({ length: 25 }, (_, index) => ({
  name: `Project ${index}`,
  id: `project-${String(index).padStart(2, "0")}`,
  status: index % 2 === 0 ? "enabled" : "disabled",
}));
const fixture = makeFixture(projects);
const before = visibleIds(fixture).length;
runScript(fixture);
const initial = {
  visible: visibleIds(fixture).length,
  visibleCount: fixture.visibleCount.textContent,
  totalCount: fixture.totalCount.textContent,
  totalDataCount: fixture.totalCount.dataset.projectTotalCount,
  loadMoreHidden: fixture.loadMore.hidden,
  controlsHidden: fixture.controls.hidden,
};
const firstClick = fixture.loadMore.dispatch("click");
const afterFirst = visibleIds(fixture).length;
fixture.loadMore.dispatch("click");
console.log(JSON.stringify({
  before,
  enhanced: fixture.root.classList.contains("projects-enhanced"),
  initial,
  firstPrevented: firstClick.defaultPrevented,
  afterFirst,
  finalVisible: visibleIds(fixture).length,
  finalLoadMoreHidden: fixture.loadMore.hidden,
}));
"""
    )

    assert result == {
        "before": 25,
        "enhanced": True,
        "initial": {
            "visible": 12,
            "visibleCount": "12",
            "totalCount": "25",
            "totalDataCount": "25",
            "loadMoreHidden": False,
            "controlsHidden": False,
        },
        "firstPrevented": True,
        "afterFirst": 24,
        "finalVisible": 25,
        "finalLoadMoreHidden": True,
    }


def test_projects_script_searches_dom_text_and_combines_status_filter() -> None:
    result = _run_node(
        r"""
const fixture = makeFixture([
  { name: "Alpha Tool", id: "project-001", status: "enabled permission" },
  { name: "Beta Tool", id: "alpha-id-002", status: "disabled inactive" },
  { name: "<b>Gamma</b>", id: "project-003", status: "enabled" },
]);
runScript(fixture);
fixture.search.value = "  ALPHA  ";
fixture.search.dispatch("input");
const searchResult = {
  ids: visibleIds(fixture),
  visibleCount: fixture.visibleCount.textContent,
  totalCount: fixture.totalCount.textContent,
};
fixture.status.value = "inactive";
fixture.status.dispatch("change");
const combinedResult = {
  ids: visibleIds(fixture),
  visibleCount: fixture.visibleCount.textContent,
  totalCount: fixture.totalCount.textContent,
};
fixture.search.value = "no match";
fixture.search.dispatch("input");
const emptyResult = {
  ids: visibleIds(fixture),
  visibleCount: fixture.visibleCount.textContent,
  totalCount: fixture.totalCount.textContent,
  emptyHidden: fixture.empty.hidden,
  loadMoreHidden: fixture.loadMore.hidden,
};
fixture.search.value = "";
fixture.status.value = "unexpected";
fixture.status.dispatch("change");
console.log(JSON.stringify({
  searchResult,
  combinedResult,
  emptyResult,
  invalidStatusFallsBackToAll: visibleIds(fixture),
}));
"""
    )

    assert result == {
        "searchResult": {
            "ids": ["project-001", "alpha-id-002"],
            "visibleCount": "2",
            "totalCount": "2",
        },
        "combinedResult": {
            "ids": ["alpha-id-002"],
            "visibleCount": "1",
            "totalCount": "1",
        },
        "emptyResult": {
            "ids": [],
            "visibleCount": "0",
            "totalCount": "0",
            "emptyHidden": False,
            "loadMoreHidden": True,
        },
        "invalidStatusFallsBackToAll": [
            "project-001",
            "alpha-id-002",
            "project-003",
        ],
    }


def test_projects_script_waits_for_dom_content_loaded() -> None:
    result = _run_node(
        r"""
const projects = Array.from({ length: 13 }, (_, index) => ({
  name: `Project ${index}`,
  id: `project-${index}`,
  status: "enabled",
}));
const fixture = makeFixture(projects, { readyState: "loading" });
runScript(fixture);
const beforeReady = {
  enhanced: fixture.root.classList.contains("projects-enhanced"),
  visible: visibleIds(fixture).length,
};
fixture.document.fire("DOMContentLoaded");
console.log(JSON.stringify({
  beforeReady,
  afterReady: {
    enhanced: fixture.root.classList.contains("projects-enhanced"),
    visible: visibleIds(fixture).length,
  },
}));
"""
    )

    assert result == {
        "beforeReady": {"enhanced": False, "visible": 13},
        "afterReady": {"enhanced": True, "visible": 12},
    }


def test_projects_script_fails_open_for_missing_dom_and_initialization_errors() -> None:
    result = _run_node(
        r"""
const projects = Array.from({ length: 13 }, (_, index) => ({
  name: `Project ${index}`,
  id: `project-${index}`,
  status: "enabled",
}));
const missingRoot = makeFixture(projects, { missingRoot: true });
runScript(missingRoot);
const missingCount = makeFixture(projects, {
  missingSelector: "[data-project-visible-count]",
});
runScript(missingCount);
const brokenClass = makeFixture(projects, { classAddThrows: true });
runScript(brokenClass);
const brokenQuery = makeFixture(projects, { queryThrows: true });
runScript(brokenQuery);
console.log(JSON.stringify({
  missingRootVisible: visibleIds(missingRoot).length,
  missingCountEnhanced: missingCount.root.classList.contains("projects-enhanced"),
  missingCountVisible: visibleIds(missingCount).length,
  missingCountControlsHidden: missingCount.controls.hidden,
  brokenClassEnhanced: brokenClass.root.classList.contains("projects-enhanced"),
  brokenClassVisible: visibleIds(brokenClass).length,
  brokenClassControlsHidden: brokenClass.controls.hidden,
  brokenQueryVisible: visibleIds(brokenQuery).length,
}));
"""
    )

    assert result == {
        "missingRootVisible": 13,
        "missingCountEnhanced": False,
        "missingCountVisible": 13,
        "missingCountControlsHidden": True,
        "brokenClassEnhanced": False,
        "brokenClassVisible": 13,
        "brokenClassControlsHidden": True,
        "brokenQueryVisible": 13,
    }


def test_projects_script_disables_enhancement_if_an_event_update_fails() -> None:
    result = _run_node(
        r"""
const projects = Array.from({ length: 13 }, (_, index) => ({
  name: `Project ${index}`,
  id: `project-${index}`,
  status: "enabled",
}));
const fixture = makeFixture(projects);
runScript(fixture);
fixture.totalCount.throwOnSetAttribute = true;
fixture.search.value = "Project 1";
fixture.search.dispatch("input");
console.log(JSON.stringify({
  enhanced: fixture.root.classList.contains("projects-enhanced"),
  visible: visibleIds(fixture).length,
  loadMoreHidden: fixture.loadMore.hidden,
  emptyHidden: fixture.empty.hidden,
  controlsHidden: fixture.controls.hidden,
}));
"""
    )

    assert result == {
        "enhanced": False,
        "visible": 13,
        "loadMoreHidden": True,
        "emptyHidden": True,
        "controlsHidden": True,
    }
