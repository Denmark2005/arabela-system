(function () {
  "use strict";

  var SEARCH_URL = "/admin-panel/api/search/";
  var MIN_QUERY_LENGTH = 2;
  var DEBOUNCE_MS = 250;

  var STATUS_CLASSES = {
    Pending: "bg-warning-50 text-warning-700 dark:bg-warning-500/15 dark:text-warning-400",
    Confirmed: "bg-brand-50 text-brand-500 dark:bg-brand-500/15 dark:text-brand-400",
    Active: "bg-brand-50 text-brand-500 dark:bg-brand-500/15 dark:text-brand-400",
    Overdue: "bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-500",
    Returned: "bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-500",
    Rejected: "bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-500",
    Cancelled: "bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-500",
    // Gown statuses -- same classes gown-catalog.html's own status badge already uses,
    // no key collisions with the reservation statuses above.
    Available: "bg-success-50 text-success-700 dark:bg-success-500/15 dark:text-success-500",
    Reserved: "bg-warning-50 text-warning-700 dark:bg-warning-500/15 dark:text-warning-400",
    "Out-of-Stock": "bg-error-50 text-error-700 dark:bg-error-500/15 dark:text-error-500",
  };

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function statusClass(status) {
    return STATUS_CLASSES[status] || "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400";
  }

  // Small "kind" tag so a gown result can't be mistaken for a reservation at a glance --
  // reservation rows render exactly as before (no tag), this is purely additive.
  var GOWN_TAG =
    '<span class="rounded-full bg-gray-100 px-2 py-1 font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-400" style="font-size:10px;">Gown</span>';

  function renderResult(r) {
    if (r.type === "gown") {
      return (
        '<a href="' + escapeHtml(r.target_url) + '" ' +
        'class="flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors hover:bg-gray-50 dark:hover:bg-white/[0.03]">' +
        '<span class="flex min-w-0 flex-col">' +
        '<span class="flex items-center gap-2">' +
        '<span class="truncate font-medium text-gray-800 dark:text-white/90">' + escapeHtml(r.gown_name) + '</span>' +
        GOWN_TAG +
        '</span>' +
        '<span class="text-xs text-gray-400 dark:text-gray-500">' + escapeHtml(r.gown_id) + ' · ' + escapeHtml(r.category) + '</span>' +
        '</span>' +
        '<span class="inline-flex shrink-0 rounded-full px-2 py-0.5 text-theme-xs font-medium ' + statusClass(r.status) + '">' + escapeHtml(r.status) + '</span>' +
        '</a>'
      );
    }
    return (
      '<a href="' + escapeHtml(r.target_url) + '" ' +
      'class="flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors hover:bg-gray-50 dark:hover:bg-white/[0.03]">' +
      '<span class="flex min-w-0 flex-col">' +
      '<span class="truncate font-medium text-gray-800 dark:text-white/90">' + escapeHtml(r.customer_name) + '</span>' +
      '<span class="text-xs text-gray-400 dark:text-gray-500">' + escapeHtml(r.reference_code) + '</span>' +
      '</span>' +
      '<span class="inline-flex shrink-0 rounded-full px-2 py-0.5 text-theme-xs font-medium ' + statusClass(r.status) + '">' + escapeHtml(r.status) + '</span>' +
      '</a>'
    );
  }

  function renderResults(container, results) {
    if (!results.length) {
      container.innerHTML =
        '<p class="px-3 py-4 text-center text-sm text-gray-400 dark:text-gray-500">No matching results.</p>';
      return;
    }
    container.innerHTML = results.map(renderResult).join("");
  }

  function initSearch(input) {
    var wrap = input.closest(".relative");
    if (!wrap) return;

    var container = document.createElement("div");
    container.id = "admin-global-search-results";
    container.setAttribute(
      "class",
      "absolute left-0 right-0 top-[calc(100%+8px)] z-50 max-h-80 overflow-y-auto rounded-2xl border border-gray-200 bg-white p-2 shadow-theme-lg dark:border-gray-800 dark:bg-gray-900"
    );
    wrap.appendChild(container);

    input.setAttribute("autocomplete", "off");
    input.setAttribute("placeholder", "Search customer, reference, or gown (e.g. Maria, RSV-2026-0001, Suit-RD-001)...");

    var debounceTimer = null;
    var activeController = null;
    // What the dropdown shows right now. Enter and the arrow keys only act on results that belong to the text typed right now.
    var loading = false; // a search request is in flight
    var shownQuery = ""; // the text the shown results belong to ("" while they are old or not there)
    var activeIndex = -1; // the highlighted result, -1 = none

    // The box sits inside a plain <form> with no action. Without this, Enter (or a click on the "K" badge, which is a submit button)
    // submits it: the whole page reloads and the loader runs, even when nothing matched.
    var form = input.closest("form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
      });
    }
    var badge = document.getElementById("search-button");
    if (badge) badge.setAttribute("type", "button");

    function openDropdown() {
      container.classList.add("is-open");
    }

    function closeDropdown() {
      container.classList.remove("is-open");
    }

    function resultLinks() {
      return container.querySelectorAll("a");
    }

    function setActive(index) {
      var items = resultLinks();
      for (var i = 0; i < items.length; i++) {
        items[i].classList.toggle("is-active", i === index);
      }
      activeIndex = index;
      if (index >= 0 && items[index] && items[index].scrollIntoView) items[index].scrollIntoView({ block: "nearest" });
    }

    // True only when the shown results are for exactly what is typed now: not loading, not old, and at least one result.
    function hasCurrentResults() {
      var query = input.value.trim();
      return !loading && query.length >= MIN_QUERY_LENGTH && shownQuery === query && resultLinks().length > 0;
    }

    function runSearch(query) {
      if (activeController) activeController.abort();
      activeController = typeof AbortController !== "undefined" ? new AbortController() : null;
      loading = true;
      shownQuery = "";

      fetch(SEARCH_URL + "?q=" + encodeURIComponent(query), {
        signal: activeController ? activeController.signal : undefined,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      })
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          renderResults(container, data.results || []);
          loading = false;
          shownQuery = query;
          setActive(-1);
          openDropdown();
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return;
          loading = false;
          shownQuery = "";
          activeIndex = -1;
          container.innerHTML =
            '<p class="px-3 py-4 text-center text-sm text-gray-400 dark:text-gray-500">Search unavailable right now.</p>';
          openDropdown();
        });
    }

    input.addEventListener("input", function () {
      var query = input.value.trim();
      clearTimeout(debounceTimer);
      shownQuery = ""; // whatever is shown now belongs to older text
      activeIndex = -1;
      if (query.length < MIN_QUERY_LENGTH) {
        // Too short: drop any search still on its way, so its answer cannot reopen the dropdown for text that is gone.
        if (activeController) activeController.abort();
        loading = false;
        closeDropdown();
        return;
      }
      debounceTimer = setTimeout(function () {
        runSearch(query);
      }, DEBOUNCE_MS);
    });

    input.addEventListener("focus", function () {
      if (input.value.trim().length >= MIN_QUERY_LENGTH && container.innerHTML.trim()) {
        openDropdown();
      }
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeDropdown();
        input.blur();
        return;
      }
      // A keyboard for another language is still composing its letters: Enter belongs to it, not to the search.
      if (event.isComposing || event.keyCode === 229) return;

      if (event.key === "Enter") {
        // Enter never submits the form (that reloads the page). It only opens a result that is really shown for the typed text:
        // not while loading, not for old results, and not when nothing matched.
        event.preventDefault();
        if (!hasCurrentResults() || !container.classList.contains("is-open")) return;
        var items = resultLinks();
        items[activeIndex >= 0 && activeIndex < items.length ? activeIndex : 0].click();
        return;
      }

      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!hasCurrentResults()) return;
        event.preventDefault();
        if (!container.classList.contains("is-open")) {
          openDropdown();
          return;
        }
        var count = resultLinks().length;
        var next = event.key === "ArrowDown" ? activeIndex + 1 : activeIndex - 1;
        if (next >= count) next = 0; // wrap round
        if (next < 0) next = count - 1;
        setActive(next);
      }
    });

    document.addEventListener("click", function (event) {
      if (!wrap.contains(event.target)) closeDropdown();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var input = document.getElementById("search-input");
    if (input) initSearch(input);
  });
})();
