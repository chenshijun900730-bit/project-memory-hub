(() => {
  "use strict";

  const PAGE_SIZE = 12;
  const ENHANCED_CLASS = "projects-enhanced";

  const FILTERS = new Set([
    "all",
    "enabled",
    "disabled",
    "permission",
    "inactive",
  ]);
  const PROJECT_STATUSES = new Set([
    "enabled",
    "disabled",
    "permission",
    "inactive",
  ]);

  function normalizedText(value) {
    return (typeof value === "string" ? value : "").trim().toLowerCase();
  }

  function initializeProjects() {
    let failOpen = () => {};

    try {
      const root = document.querySelector("[data-project-browser]");
      if (!root) {
        return;
      }
      if (root.getAttribute("data-project-page-size") !== String(PAGE_SIZE)) {
        return;
      }

      const controls = root.querySelector("[data-project-controls]");
      const search = root.querySelector("[data-project-search]");
      const status = root.querySelector("[data-project-status-filter]");
      const visibleCountElement = root.querySelector("[data-project-visible-count]");
      const totalCount = root.querySelector("[data-project-total-count]");
      const loadMore = root.querySelector("[data-project-show-more]");
      const empty = root.querySelector("[data-project-no-results]");
      const list = root.querySelector("[data-project-list]");
      if (
        !controls ||
        !search ||
        !status ||
        !visibleCountElement ||
        !totalCount ||
        !loadMore ||
        !empty ||
        !list
      ) {
        return;
      }

      const cards = Array.from(list.querySelectorAll("[data-project-card]"));
      const projects = [];
      for (const card of cards) {
        const name = card.getAttribute("data-project-name");
        const id = card.getAttribute("data-project-id");
        const statusText = card.getAttribute("data-project-status");
        const projectStatuses = new Set(normalizedText(statusText).split(/\s+/));
        const hasPrimaryStatus =
          projectStatuses.has("enabled") !== projectStatuses.has("disabled");
        if (
          typeof name !== "string" ||
          typeof id !== "string" ||
          normalizedText(id) === "" ||
          !hasPrimaryStatus ||
          [...projectStatuses].some((value) => !PROJECT_STATUSES.has(value))
        ) {
          return;
        }
        projects.push({
          card,
          projectStatuses,
          searchText: `${normalizedText(name)} ${normalizedText(id)}`,
        });
      }

      let visibleLimit = PAGE_SIZE;
      let active = true;
      const listeners = [];

      failOpen = () => {
        active = false;
        for (const [element, type, listener] of listeners) {
          try {
            element.removeEventListener(type, listener);
          } catch (_error) {
            // A broken DOM node must not prevent the remaining cards from returning.
          }
        }
        try {
          root.classList.remove(ENHANCED_CLASS);
        } catch (_error) {
          // Without the class, the server-rendered page remains the fallback.
        }
        for (const project of projects) {
          try {
            project.card.hidden = false;
          } catch (_error) {
            // Continue restoring every other server-rendered card.
          }
        }
        try {
          controls.hidden = true;
          loadMore.hidden = true;
          empty.hidden = true;
        } catch (_error) {
          // The enhancement class is already absent, so controls stay inactive.
        }
      };

      function selectedStatus() {
        return FILTERS.has(status.value) ? status.value : "all";
      }

      function render() {
        const query = String(search.value || "").trim().toLowerCase();
        const selected = selectedStatus();
        const matches = projects.filter(
          (project) =>
            (query === "" || project.searchText.includes(query)) &&
            (selected === "all" || project.projectStatuses.has(selected)),
        );
        const visible = new Set(
          matches.slice(0, visibleLimit).map((project) => project.card),
        );

        for (const project of projects) {
          project.card.hidden = !visible.has(project.card);
        }

        const visibleCount = visible.size;
        const matchCount = matches.length;
        visibleCountElement.textContent = String(visibleCount);
        totalCount.textContent = String(matchCount);
        totalCount.setAttribute("data-project-total-count", String(matchCount));
        loadMore.hidden = visibleCount >= matchCount;
        empty.hidden = matchCount !== 0;
      }

      function guarded(listener) {
        return (event) => {
          if (!active) {
            return;
          }
          try {
            listener(event);
          } catch (_error) {
            failOpen();
          }
        };
      }

      const onSearch = guarded(() => {
        visibleLimit = PAGE_SIZE;
        render();
      });
      const onStatus = guarded(() => {
        visibleLimit = PAGE_SIZE;
        render();
      });
      const onLoadMore = guarded((event) => {
        if (event && typeof event.preventDefault === "function") {
          event.preventDefault();
        }
        visibleLimit = Math.min(visibleLimit + PAGE_SIZE, projects.length);
        render();
      });

      search.addEventListener("input", onSearch);
      listeners.push([search, "input", onSearch]);
      status.addEventListener("change", onStatus);
      listeners.push([status, "change", onStatus]);
      loadMore.addEventListener("click", onLoadMore);
      listeners.push([loadMore, "click", onLoadMore]);

      render();
      controls.hidden = false;
      root.classList.add(ENHANCED_CLASS);
    } catch (_error) {
      failOpen();
    }
  }

  try {
    if (typeof document === "undefined") {
      return;
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initializeProjects, {
        once: true,
      });
    } else {
      initializeProjects();
    }
  } catch (_error) {
    // The original server-rendered project list is the final fallback.
  }
})();
