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
  };

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function statusClass(status) {
    return STATUS_CLASSES[status] || "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400";
  }

  function renderResults(container, results) {
    if (!results.length) {
      container.innerHTML =
        '<p class="px-3 py-4 text-center text-sm text-gray-400 dark:text-gray-500">No matching reservations.</p>';
      return;
    }
    container.innerHTML = results
      .map(function (r) {
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
      })
      .join("");
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
    input.setAttribute("placeholder", "Search customer name or reference (e.g. Maria, RSV-2026-0001)...");

    var debounceTimer = null;
    var activeController = null;

    function openDropdown() {
      container.classList.add("is-open");
    }

    function closeDropdown() {
      container.classList.remove("is-open");
    }

    function runSearch(query) {
      if (activeController) activeController.abort();
      activeController = typeof AbortController !== "undefined" ? new AbortController() : null;

      fetch(SEARCH_URL + "?q=" + encodeURIComponent(query), {
        signal: activeController ? activeController.signal : undefined,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      })
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          renderResults(container, data.results || []);
          openDropdown();
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return;
          container.innerHTML =
            '<p class="px-3 py-4 text-center text-sm text-gray-400 dark:text-gray-500">Search unavailable right now.</p>';
          openDropdown();
        });
    }

    input.addEventListener("input", function () {
      var query = input.value.trim();
      clearTimeout(debounceTimer);
      if (query.length < MIN_QUERY_LENGTH) {
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
