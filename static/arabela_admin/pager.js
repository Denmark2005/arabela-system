/*
 * Admin list pager: a long list shows 15 rows at a time, with Back / Next, page numbers and a marked current page.
 *
 * It sits ON TOP of each page's own search / filter / sort and changes none of it. A list opts in with data-arpg on its scroll box:
 *   data-arpg="tbody"          the rows are the <tr>s of the box's table (a booking with several gowns, or an item with its timeline row,
 *                              is moved as ONE unit so it is never cut across two pages)
 *   data-arpg=".some-class"    the rows are the box's direct children with that class
 *   data-arpg-noun="bookings"  what the rows are called in "Showing 16-30 of 685 bookings"
 * The page decides which rows MATCH (its own x-show hides the rest); this file only hides the matching rows that are not on the
 * current page, with its own attribute (data-arpg-off), so nothing the page does to a row is ever overwritten.
 *
 * The page number lives in the address (?page=3) so the reload that follows many admin actions stays on the same page. A change of
 * filter, search or sort goes back to page 1. If anything here ever throws, every hidden row is shown again: the list then looks and
 * works exactly as it did before paging existed.
 */
(function () {
  "use strict";

  var PAGE_SIZE = 15;
  var PARAM = "page";
  var OFF = "data-arpg-off";
  var instances = [];
  // True once the person has touched the page. Until then a change in the rows is just the page finishing loading, so the page number
  // that came from the address is kept; after that, a change in the rows means they changed a filter / search / sort.
  var interacted = false;

  var CSS = [
    "[" + OFF + "]{display:none !important;}",
    ".arpg-top,.arpg-bar{display:none;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem .75rem;padding:.6rem 1.25rem;font-size:.8125rem;color:#667085;border-top:1px solid #eaecf0;}",
    ".arpg-top{border-top:0;border-bottom:1px solid #eaecf0;}",
    ".arpg-bar{padding:.8rem 1.25rem;}",
    ".arpg-top.is-on,.arpg-bar.is-on{display:flex;}",
    ".arpg-pagepos{font-weight:600;color:#344054;font-variant-numeric:tabular-nums;}",
    ".arpg-nav{display:flex;align-items:center;flex-wrap:wrap;gap:.35rem;}",
    ".arpg-btn{min-width:2.25rem;height:2.25rem;padding:0 .7rem;border:1px solid #d0d5dd;border-radius:.5rem;background:transparent;color:#344054;font-size:.875rem;font-weight:500;line-height:1;cursor:pointer;font-variant-numeric:tabular-nums;}",
    ".arpg-btn:hover:not(:disabled):not(.is-cur){background:rgba(152,162,179,.14);}",
    ".arpg-btn:disabled{opacity:.4;cursor:not-allowed;}",
    ".arpg-btn:focus-visible{outline:2px solid #465fff;outline-offset:2px;}",
    ".arpg-btn.is-cur{background:#465fff;border-color:#465fff;color:#fff;cursor:default;}",
    ".arpg-btn[data-key^=\"n\"]{width:2.5rem;min-width:2.5rem;padding:0;}",      // 1, 10 and 100 take the same room: the row never changes width
    ".arpg-gap{width:2.5rem;text-align:center;color:#98a2b3;}",
    ".arpg-compact{display:none;min-width:4rem;text-align:center;font-weight:600;color:#344054;font-variant-numeric:tabular-nums;}",
    ".dark .arpg-top,.dark .arpg-bar{color:#98a2b3;border-color:#1d2939;}",
    ".dark .arpg-top{border-top:0;}",
    ".dark .arpg-pagepos,.dark .arpg-compact{color:#d0d5dd;}",
    ".dark .arpg-btn{border-color:#344054;color:#d0d5dd;}",
    ".dark .arpg-btn.is-cur{background:#465fff;border-color:#465fff;color:#fff;}",
    "@media (max-width:640px){",
    "  .arpg-wide{display:none !important;}",
    "  .arpg-compact{display:inline-block;}",
    "  .arpg-top,.arpg-bar{justify-content:center;text-align:center;}",
    "  .arpg-bar .arpg-pagepos{display:none;}",
    "  .arpg-btn{height:2.75rem;min-width:2.75rem;padding:0 1rem;}",
    "}"
  ].join("\n");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text != null) { node.textContent = text; }
    return node;
  }

  // The page's own filter hides a row with Alpine's x-show, which sets style.display = "none".
  function isShown(row) {
    return row.style.display !== "none" && !row.hidden;
  }

  function hasCell(tr, attr) {
    for (var i = 0; i < tr.children.length; i++) {
      if (tr.children[i].hasAttribute(attr)) { return true; }
    }
    return false;
  }

  function sameList(a, b) {
    if (a.length !== b.length) { return false; }
    for (var i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) { return false; }
    }
    return true;
  }

  // The page numbers to draw; 0 means "...". Past 7 pages it is ALWAYS exactly 7 slots (and a "..." is as wide as a number), so the row
  // never changes width from one page to the next and Back / Next stay under the pointer while someone clicks through the pages.
  function numbersFor(current, last) {
    var out = [];
    var i;
    if (last <= 7) {
      for (i = 1; i <= last; i++) { out.push(i); }
      return out;
    }
    if (current <= 4) { return [1, 2, 3, 4, 5, 0, last]; }
    if (current >= last - 3) { return [1, 0, last - 4, last - 3, last - 2, last - 1, last]; }
    return [1, 0, current - 1, current, current + 1, 0, last];
  }

  function Pager(box) {
    var mode = box.getAttribute("data-arpg") || "tbody";
    var container = mode === "tbody" ? box.querySelector("tbody") : box;
    if (!container) { return null; }
    var noun = box.getAttribute("data-arpg-noun") || "items";
    var size = parseInt(box.getAttribute("data-arpg-size"), 10) || PAGE_SIZE;
    var card = box.closest(".rounded-2xl") || box.parentElement;
    var region = box;
    while (region.parentElement && region.parentElement !== card) { region = region.parentElement; }
    if (!region.parentElement) { return null; }

    var wanted = parseInt(new URLSearchParams(window.location.search).get(PARAM), 10);
    if (!(wanted >= 1)) { wanted = 1; }
    var lastList = null;
    var lastPage = 0;
    var failed = false;
    var observer = null;
    var fullHeight = 0;          // how tall the list box is on a page of 15 (measured), so a shorter last page can keep that height
    var fullMeasured = false;
    var lastPages = 1;

    var topInfo = el("span", "arpg-info");
    var top = el("div", "arpg-top");
    top.appendChild(topInfo);
    var barPos = el("span", "arpg-pagepos");
    var nav = el("nav", "arpg-nav");
    nav.setAttribute("aria-label", "Pages");
    var bar = el("div", "arpg-bar");
    bar.appendChild(barPos);
    bar.appendChild(nav);
    if (box.getAttribute("data-arpg-top") !== "off") { region.parentElement.insertBefore(top, region); }      // data-arpg-top="off": the page shows the count itself
    var countEl = box.getAttribute("data-arpg-count") ? document.querySelector(box.getAttribute("data-arpg-count")) : null;
    region.parentElement.insertBefore(bar, region.nextSibling);

    // The rows as units: [firstRow, ...rowsThatGoWithIt]. The first row is the one the page's filter shows or hides.
    function collectUnits() {
      var kids = Array.prototype.slice.call(container.children);
      if (mode !== "tbody") {
        return kids.filter(function (node) { return node.matches(mode); }).map(function (node) { return [node]; });
      }
      var grouped = kids.some(function (tr) { return tr.tagName === "TR" && !hasCell(tr, "colspan") && hasCell(tr, "rowspan"); });
      var units = [];
      kids.forEach(function (tr) {
        if (tr.tagName !== "TR") { return; }
        var detail = hasCell(tr, "colspan");              // a timeline row under its item, or a "nothing found" row
        if (!units.length) { if (!detail) { units.push([tr]); } return; }
        if (detail || (grouped && !hasCell(tr, "rowspan"))) { units[units.length - 1].push(tr); return; }
        units.push([tr]);
      });
      return units;
    }

    function syncUrl(page) {
      var params = new URLSearchParams(window.location.search);
      if (page > 1) { params.set(PARAM, String(page)); } else { params.delete(PARAM); }
      var query = params.toString();
      var next = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
      if (next !== window.location.pathname + window.location.search + window.location.hash) {
        history.replaceState(history.state, "", next);
      }
    }

    function button(label, key, target, disabled, extra, aria) {
      var b = el("button", "arpg-btn" + (extra ? " " + extra : ""), label);
      b.type = "button";
      b.setAttribute("data-key", key);
      b.setAttribute("data-page", String(target));
      b.setAttribute("aria-label", aria);
      if (disabled) { b.disabled = true; }
      return b;
    }

    // data-arpg-count="#id": that element always says how many rows there are ("Showing 1-15 of 390 gowns", or "7 gowns" when they all fit).
    function setCount(page, pages, total, from, to) {
      if (!countEl) { return; }
      var one = noun.replace(/s$/, "");
      countEl.textContent = total === 0 ? "No " + noun : pages > 1 ? "Showing " + (from + 1) + "–" + to + " of " + total + " " + noun : total + " " + (total === 1 ? one : noun);
    }

    function paint(page, pages, total, from, to) {
      var on = pages > 1;
      top.classList.toggle("is-on", on);
      bar.classList.toggle("is-on", on);
      if (!on) { return; }
      topInfo.textContent = "Showing " + (from + 1) + "–" + to + " of " + total + " " + noun;
      barPos.textContent = "Page " + page + " / " + pages;
      var keep = nav.contains(document.activeElement) ? document.activeElement.getAttribute("data-key") : null;
      while (nav.firstChild) { nav.removeChild(nav.firstChild); }
      nav.appendChild(button("«", "first", 1, page === 1, "arpg-wide", "First page"));
      nav.appendChild(button("‹ Back", "prev", page - 1, page === 1, "", "Previous page"));
      numbersFor(page, pages).forEach(function (n) {
        if (!n) { nav.appendChild(el("span", "arpg-gap arpg-wide", "…")); return; }
        var b = button(String(n), "n" + n, n, false, "arpg-wide" + (n === page ? " is-cur" : ""), "Page " + n);
        if (n === page) { b.setAttribute("aria-current", "page"); }
        nav.appendChild(b);
      });
      nav.appendChild(el("span", "arpg-compact", page + " / " + pages));
      nav.appendChild(button("Next ›", "next", page + 1, page === pages, "", "Next page"));
      nav.appendChild(button("»", "last", pages, page === pages, "arpg-wide", "Last page"));
      if (keep) {
        var again = nav.querySelector('[data-key="' + keep + '"]');
        if (again && !again.disabled) { again.focus(); }
      }
    }

    // Keep the screen still while paging. Without this the last page (fewer rows) made the card shorter: the Back / Next bar jumped up from
    // under the pointer and, once the page was short enough to need no scrollbar, the whole layout slid sideways by the scrollbar's width.
    // The list box keeps the height it has on a full page (never more than its own max-height), so every page is the same height and the
    // page never gets shorter, so it can neither move the bar nor lose its scrollbar.
    // How tall the rows themselves are right now, measured without touching the box's own minimum height.
    function naturalHeight() {
      var table = mode === "tbody" ? box.querySelector("table") : null;
      if (table) { return table.offsetHeight; }
      var sum = 0;
      Array.prototype.forEach.call(box.children, function (child) { sum += child.offsetHeight; });
      return sum;
    }

    // Called BEFORE any row is hidden: pin the box at the height it has right now. Hiding rows first would shorten the page for a moment
    // and the browser would clamp the scroll position to the shorter page; pinned, the page never gets shorter, so it cannot scroll.
    function pinHeight() {
      if (lastPages > 1 && box.offsetParent !== null) { box.style.minHeight = box.offsetHeight + "px"; }
    }

    function holdStill(onPage, pages) {
      lastPages = pages;
      if (pages <= 1) { box.style.minHeight = ""; return; }
      if (box.offsetParent === null) { return; }                 // hidden for a moment (the page's own loading spinner): nothing to measure
      var cap = parseFloat(window.getComputedStyle(box).maxHeight);
      var natural = naturalHeight();
      var height = isNaN(cap) ? natural : Math.min(natural, cap);
      if (onPage >= size) {
        fullHeight = fullMeasured ? Math.max(fullHeight, height) : height;
        fullMeasured = true;
      } else if (!fullMeasured && onPage > 0) {
        // Opened straight on a short last page: estimate a full page from the rows here (header + rows), until a full page has been seen.
        var head = box.querySelector("thead, .ar-head");
        var headHeight = head ? head.offsetHeight : 0;
        var estimate = headHeight + (height - headHeight) * size / onPage;
        fullHeight = Math.max(fullHeight, isNaN(cap) ? estimate : Math.min(estimate, cap));
      }
      box.style.minHeight = fullHeight > height ? Math.round(fullHeight) + "px" : "";
    }

    function failOpen(err) {
      failed = true;
      if (observer) { observer.disconnect(); }
      try { console.error("Pager turned off, the full list is shown:", err); } catch (ignore) { /* no console */ }
      Array.prototype.forEach.call(container.querySelectorAll("[" + OFF + "]"), function (row) { row.removeAttribute(OFF); });
      box.style.minHeight = "";
      top.classList.remove("is-on");
      bar.classList.remove("is-on");
    }

    function render() {
      if (failed) { return; }
      try {
        var units = collectUnits();
        var matching = units.filter(function (u) { return isShown(u[0]); });
        var list = matching.map(function (u) { return u[0]; });
        var listChanged = lastList === null || !sameList(lastList, list);
        if (interacted && lastList !== null && listChanged) { wanted = 1; }      // a new filter, search or sort starts at page 1
        lastList = list;
        var pages = Math.max(1, Math.ceil(matching.length / size));
        var page = Math.min(Math.max(wanted, 1), pages);
        var from = (page - 1) * size;
        var to = Math.min(from + size, matching.length);
        var seen = -1;
        pinHeight();
        units.forEach(function (u) {
          var off = false;
          if (isShown(u[0])) { seen += 1; off = seen < from || seen >= to; }
          u.forEach(function (row) {
            if (off !== row.hasAttribute(OFF)) {
              if (off) { row.setAttribute(OFF, ""); } else { row.removeAttribute(OFF); }
            }
          });
        });
        if (interacted) { wanted = page; syncUrl(page); }
        paint(page, pages, matching.length, from, to);
        setCount(page, pages, matching.length, from, to);
        holdStill(to - from, pages);
        if (listChanged || page !== lastPage) {
          lastPage = page;
          window.dispatchEvent(new CustomEvent("arabela-pager", { detail: { page: page, pages: pages, total: matching.length } }));
        }
      } catch (err) {
        failOpen(err);
      }
    }

    // Only the list's own scroll goes back to its first row. The page itself is never scrolled, so the Back / Next bar stays exactly
    // where it is under the pointer and the screen does not move while someone clicks through the pages.
    function go(target) {
      var before = lastPage;
      interacted = true;
      wanted = target;
      render();
      if (lastPage !== before) { box.scrollTop = 0; }
    }

    nav.addEventListener("click", function (event) {
      var b = event.target.closest("button[data-page]");
      if (b && !b.disabled) { go(parseInt(b.getAttribute("data-page"), 10)); }
    });

    // A different window size changes the row heights: measure a full page again.
    var resizeTimer = null;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () { fullHeight = 0; fullMeasured = false; render(); }, 150);
    });

    // Re-count whenever the page changes which rows it shows (its x-show on a row) or adds / removes / re-orders rows (x-for, sort).
    observer = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        if (r.type === "childList" ? r.target === container : r.target.parentElement === container) { render(); return; }
      }
    });
    observer.observe(container, { childList: true, attributes: true, attributeFilter: ["style"], subtree: true });

    this.box = box;
    this.container = container;
    this.render = render;
    // The matching rows on the page being shown (the first row of each unit), or null once the pager has turned itself off.
    this.pageRows = function () {
      if (failed) { return null; }
      return collectUnits().map(function (u) { return u[0]; }).filter(function (row) { return isShown(row) && !row.hasAttribute(OFF); });
    };
    render();
  }

  function start() {
    var style = document.createElement("style");
    style.id = "arpg-style";
    style.textContent = CSS;
    document.head.appendChild(style);
    ["pointerdown", "keydown", "input", "change"].forEach(function (name) {
      document.addEventListener(name, function () { interacted = true; }, true);
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-arpg]"), function (box) {
      try {
        var pager = new Pager(box);
        if (pager && pager.render) { instances.push(pager); }
      } catch (err) {
        try { console.error("Pager could not start on this list:", err); } catch (ignore) { /* no console */ }
      }
    });
  }

  // For page scripts that need to know which rows are on screen (the Gown Catalog's "select all"). Pass the list's <tbody> or box.
  window.arabelaPager = {
    shownUnits: function (node) {
      for (var i = 0; i < instances.length; i++) {
        if (instances[i].container === node || instances[i].box === node) { return instances[i].pageRows(); }
      }
      return null;
    }
  };

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
})();
