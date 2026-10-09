(function () {
  var STORAGE_PREFIX = 'arabela_collection_sort:';
  var sortDrawerPendingMode = null;
  var sortDrawerPendingLabel = null;

  function pathKey() {
    // A search page lists something different for every search, so its sort is remembered per search
    // (a new search starts on Best Match again). Every other page keeps one choice per path.
    var root = getRoot();
    var perQuery = root && root.hasAttribute('data-sort-per-query');
    return STORAGE_PREFIX + location.pathname + (perQuery ? location.search : '');
  }

  function getRoot() {
    return document.querySelector('[data-collection-sort-root]');
  }

  function getVisibleAllGrid(root) {
    if (!root || root.id !== 'all-collections-root') return null;
    var panels = root.querySelectorAll('.all-collection-panel');
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].style.display !== 'none') {
        return panels[i].querySelector('.grid');
      }
    }
    return root.querySelector('.all-collection-panel .grid');
  }

  function getGrid(root) {
    if (!root) return null;
    if (root.id === 'all-collections-root') return getVisibleAllGrid(root);
    var g = root.querySelector(':scope > .grid');
    if (g) return g;
    return root.querySelector('.grid');
  }

  function getCards(grid) {
    if (!grid) return [];
    return Array.prototype.slice.call(grid.children).filter(function (el) {
      return el.tagName === 'A' && el.querySelector('h3');
    });
  }

  function parsePrice(text) {
    if (!text) return 0;
    var n = parseFloat(String(text).replace(/[^\d.]/g, ''));
    return isNaN(n) ? 0 : n;
  }

  function getTitle(card) {
    var h = card.querySelector('h3');
    return h ? String(h.textContent).trim() : '';
  }

  function getPrice(card) {
    var ps = card.querySelectorAll('.space-y-2 p');
    // The price is the line with the peso sign. (A product with several units also has a "3 available" line,
    // which used to be read as the price.)
    for (var i = 0; i < ps.length; i++) {
      if (ps[i].textContent.indexOf('\u20B1') !== -1) return parsePrice(ps[i].textContent);
    }
    if (ps.length >= 2) return parsePrice(ps[1].textContent);
    if (ps.length === 1) return parsePrice(ps[0].textContent);
    return 0;
  }

  function rememberOriginal(grid) {
    if (!grid || grid.__sortOriginalOrder) return;
    grid.__sortOriginalOrder = getCards(grid).slice();
  }

  function placeCards(grid, order) {
    var frag = document.createDocumentFragment();
    order.forEach(function (c) {
      frag.appendChild(c);
    });
    grid.appendChild(frag);
  }

  // The order the grid's cards should be in for a sort mode, as a new array (nothing is moved), or null when the
  // mode is unknown or there is nothing to sort.
  function targetOrder(mode, grid) {
    rememberOriginal(grid);
    var cards = getCards(grid);
    if (!cards.length) return null;
    var sorted = cards.slice();
    if (mode === 'az') sorted.sort(function (a, b) { return getTitle(a).localeCompare(getTitle(b)); });
    else if (mode === 'za') sorted.sort(function (a, b) { return getTitle(b).localeCompare(getTitle(a)); });
    else if (mode === 'priceAsc') sorted.sort(function (a, b) { return getPrice(a) - getPrice(b); });
    else if (mode === 'priceDesc') sorted.sort(function (a, b) { return getPrice(b) - getPrice(a); });
    else if (mode === 'bestMatch') {
      // The server sends the list already in Best Match order, so this is simply "put it back".
      return grid.__sortOriginalOrder.slice();
    }
    else return null;
    return sorted;
  }

  // Re-orders the grid at once, with no loading step (used when a page opens with a sort already chosen).
  function sortGrid(mode) {
    var grid = getGrid(getRoot());
    if (!grid) return;
    if (running) cleanup(running);
    var order = targetOrder(mode, grid);
    if (order) placeCards(grid, order);
  }

  // ---- the loading step between "pick a sort" and "see the new order" ------------------------------------------
  // When someone applies, resets or clears a sort, the cards fade into grey placeholders (the same shimmer as the
  // Orders page), re-order behind them, then fade back in one after another -- so a sort never just pops.
  // Whatever goes wrong, cleanup() puts every card back exactly as it should be.
  var HIDE_MS = 170;          // the cards fade out; only then are they re-ordered, unseen
  var SHOW_MS = 560;          // the placeholders have been up this long; then the new order comes in
  var STAGGER_MS = 35;        // gap between one card starting to fade in and the next
  var MAX_STAGGER_MS = 280;   // ceiling, so a long list never crawls in
  var FADE_IN_MS = 420;
  var MAX_PLACEHOLDERS = 60;
  var running = null;         // the loading step in progress, if any

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function sameOrder(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  // One grey placeholder laid exactly over each real card (same spot, same size), so nothing shifts.
  function buildPlaceholders(cards) {
    var layer = document.createElement('div');
    layer.className = 'arb-skel-layer';
    layer.setAttribute('aria-hidden', 'true');
    cards.slice(0, MAX_PLACEHOLDERS).forEach(function (card) {
      var w = card.offsetWidth;
      var h = card.offsetHeight;
      var imageH = Math.min(Math.round(w * 5 / 3), Math.max(h - 60, 0));
      var el = document.createElement('div');
      el.className = 'arb-skel-card';
      el.style.cssText = 'left:' + card.offsetLeft + 'px;top:' + card.offsetTop + 'px;width:' + w + 'px;height:' + h + 'px;';
      el.innerHTML =
        '<div class="arb-skel" style="height:' + imageH + 'px"></div>' +
        '<div class="arb-skel" style="height:14px;width:62%;margin-top:24px"></div>' +
        '<div class="arb-skel" style="height:12px;width:34%;margin-top:10px"></div>';
      layer.appendChild(el);
    });
    return layer;
  }

  // Ends a loading step right now: cards in their final order, fully visible, no placeholders, no leftovers.
  function cleanup(r) {
    r.timers.forEach(clearTimeout);
    r.timers = [];
    if (!r.placed) {
      placeCards(r.grid, r.order);
      r.placed = true;
    }
    getCards(r.grid).forEach(function (card) {
      card.style.removeProperty('opacity');
      card.style.removeProperty('transform');
      card.style.removeProperty('transition');
    });
    r.grid.classList.remove('arb-sort-loading', 'arb-skel-host');
    r.grid.removeAttribute('aria-busy');
    if (r.layer.parentNode) r.layer.parentNode.removeChild(r.layer);
    if (running === r) running = null;
  }

  function reveal(r) {
    if (running !== r) return;
    if (!r.placed) {
      placeCards(r.grid, r.order);
      r.placed = true;
    }
    var cards = getCards(r.grid);
    // Hold the cards invisible with inline styles first, so dropping the loading class cannot flash them.
    cards.forEach(function (card) {
      card.style.transition = 'none';
      card.style.opacity = '0';
      card.style.transform = 'translateY(14px)';
    });
    r.grid.classList.remove('arb-sort-loading');
    void r.grid.offsetWidth;
    r.layer.classList.remove('is-in');
    r.layer.classList.add('is-leaving');
    r.timers.push(setTimeout(function () {         // once it has faded out, the placeholders are not needed any more
      if (r.layer.parentNode) r.layer.parentNode.removeChild(r.layer);
    }, 340));
    // Two frames: the first applies the hidden start, the second starts the transition from it.
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        if (running !== r) return;
        var longest = 0;
        cards.forEach(function (card, i) {
          var delay = Math.min(i * STAGGER_MS, MAX_STAGGER_MS);
          if (delay > longest) longest = delay;
          var ease = FADE_IN_MS + 'ms cubic-bezier(0.16, 1, 0.3, 1) ' + delay + 'ms';
          card.style.transition = 'opacity ' + ease + ', transform ' + ease;
          card.style.opacity = '1';
          card.style.transform = 'none';
        });
        r.timers.push(setTimeout(function () { cleanup(r); }, longest + FADE_IN_MS + 120));
      });
    });
  }

  function animateGridChange(grid, order, cards) {
    grid.classList.add('arb-skel-host');          // the placeholders are positioned against the grid
    var layer = buildPlaceholders(cards);
    grid.appendChild(layer);
    var r = { grid: grid, order: order, placed: false, layer: layer, timers: [] };
    running = r;
    grid.setAttribute('aria-busy', 'true');
    void layer.offsetWidth;                       // so the fade-in has a starting point
    layer.classList.add('is-in');
    grid.classList.add('arb-sort-loading');       // the real cards fade out under the placeholders
    r.timers.push(setTimeout(function () {
      if (running === r && !r.placed) {
        placeCards(grid, order);
        r.placed = true;
      }
    }, HIDE_MS));
    r.timers.push(setTimeout(function () { reveal(r); }, SHOW_MS));
    r.timers.push(setTimeout(function () { cleanup(r); }, SHOW_MS + 2500));   // safety net: never leave anything hidden
  }

  // Changes the grid to the order getOrder() returns, with the loading step when the order really changes.
  function changeGrid(grid, getOrder) {
    if (!grid) return;
    if (running) cleanup(running);                // finish any earlier change first, so this one starts from its end
    var order = getOrder();
    if (!order) return;
    var cards = getCards(grid);
    if (sameOrder(cards, order)) return;          // nothing would move: nothing to load
    if (cards.length < 2 || prefersReducedMotion() || grid.offsetParent === null) {
      placeCards(grid, order);
      return;
    }
    animateGridChange(grid, order, cards);
  }

  function showChip(label) {
    var row = document.getElementById('collection-sort-active-row');
    var lbl = document.getElementById('collection-sort-active-label');
    if (!row || !lbl) return;
    lbl.textContent = 'SORT BY: ' + label;
    row.classList.remove('hidden');
  }

  function hideChip() {
    var row = document.getElementById('collection-sort-active-row');
    if (!row) return;
    row.classList.add('hidden');
  }

  function persist(mode, label) {
    try {
      sessionStorage.setItem(pathKey(), JSON.stringify({ mode: mode, label: label }));
    } catch (e) {}
  }

  function readPersist() {
    try {
      var raw = sessionStorage.getItem(pathKey());
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function clearPersist() {
    try {
      sessionStorage.removeItem(pathKey());
    } catch (e) {}
  }

  function getCollectionDrawer() {
    return document.getElementById('explore-collections-drawer');
  }

  function setSortRowVisual(row, isSelected, labelText) {
    var plain = row.querySelector('[data-sort-option]');
    var shell = row.querySelector('[data-sort-selected-shell]');
    var labelEl = row.querySelector('[data-sort-pill-label]');
    if (!plain || !shell) return;
    if (isSelected) {
      plain.classList.add('hidden');
      shell.classList.remove('hidden');
      if (labelEl) labelEl.textContent = labelText || '';
    } else {
      plain.classList.remove('hidden');
      shell.classList.add('hidden');
    }
  }

  function clearDrawerPendingSelection() {
    var drawer = getCollectionDrawer();
    if (!drawer) return;
    drawer.querySelectorAll('[data-sort-row]').forEach(function (row) {
      setSortRowVisual(row, false, '');
    });
  }

  function syncSortDrawerVisuals(pendingMode, pendingLabel) {
    var drawer = getCollectionDrawer();
    if (!drawer) return;
    var mode = pendingMode;
    var label = pendingLabel;
    if (!mode) {
      var applied = readPersist();
      if (applied && applied.mode) {
        mode = applied.mode;
        label = applied.label || '';
      }
    }
    drawer.querySelectorAll('[data-sort-row]').forEach(function (row) {
      var m = row.getAttribute('data-sort-mode');
      setSortRowVisual(row, mode === m, mode === m ? label : '');
    });
  }

  // options.silent: re-order at once with no loading step (a page opening with its sort already chosen).
  window.applyCollectionSort = function (mode, label, options) {
    if (options && options.silent) {
      sortGrid(mode);
    } else {
      var grid = getGrid(getRoot());
      changeGrid(grid, function () { return targetOrder(mode, grid); });
    }
    if (label) persist(mode, label);
    if (label) showChip(label);
  };

  window.resetCollectionSort = function () {
    var root = getRoot();
    var grid = getGrid(root);
    if (grid && grid.__sortOriginalOrder && grid.__sortOriginalOrder.length) {
      var original = grid.__sortOriginalOrder;
      changeGrid(grid, function () { return original.slice(); });
    }
    if (grid) delete grid.__sortOriginalOrder;
    clearPersist();
    hideChip();
    sortDrawerPendingMode = null;
    sortDrawerPendingLabel = null;
    clearDrawerPendingSelection();
  };

  window.applyStoredCollectionSortIfAny = function () {
    var data = readPersist();
    if (!data || !data.mode) return;
    sortGrid(data.mode);
    if (data.label) showChip(data.label);
  };

  function initDrawer() {
    var drawer = getCollectionDrawer();
    if (!drawer) return;

    drawer.querySelectorAll('[data-sort-option]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        sortDrawerPendingMode = btn.getAttribute('data-sort-mode');
        sortDrawerPendingLabel = btn.getAttribute('data-sort-label');
        syncSortDrawerVisuals(sortDrawerPendingMode, sortDrawerPendingLabel);
      });
    });

    drawer.querySelectorAll('[data-sort-pill-dismiss]').forEach(function (dismissBtn) {
      dismissBtn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var row = dismissBtn.closest('[data-sort-row]');
        if (!row) return;
        var m = row.getAttribute('data-sort-mode');
        if (sortDrawerPendingMode === m) {
          sortDrawerPendingMode = null;
          sortDrawerPendingLabel = null;
        }
        var applied = readPersist();
        if (applied && applied.mode === m) {
          window.resetCollectionSort();
        } else {
          syncSortDrawerVisuals(sortDrawerPendingMode, sortDrawerPendingLabel);
        }
      });
    });

    var applyFooter = drawer.querySelector('[data-sort-apply-footer]');
    if (applyFooter) {
      applyFooter.addEventListener('click', function () {
        if (sortDrawerPendingMode) {
          window.applyCollectionSort(sortDrawerPendingMode, sortDrawerPendingLabel);
          sortDrawerPendingMode = null;
          sortDrawerPendingLabel = null;
        }
        syncSortDrawerVisuals(null, null);
        if (typeof window.closeExploreDrawer === 'function') {
          window.closeExploreDrawer();
        }
      });
    }

    var drawerReset = drawer.querySelector('[data-sort-drawer-reset]');
    if (drawerReset) {
      drawerReset.addEventListener('click', function () {
        window.resetCollectionSort();
        syncSortDrawerVisuals(null, null);
        if (typeof window.closeExploreDrawer === 'function') {
          window.closeExploreDrawer();
        }
      });
    }

    window.syncCollectionSortDrawerVisuals = function () {
      syncSortDrawerVisuals(sortDrawerPendingMode, sortDrawerPendingLabel);
    };
  }

  function initChipDismiss() {
    var dismiss = document.getElementById('collection-sort-chip-dismiss');
    var clearAll = document.getElementById('collection-sort-clear-all');
    function go() {
      window.resetCollectionSort();
    }
    if (dismiss) dismiss.addEventListener('click', go);
    if (clearAll) clearAll.addEventListener('click', go);
  }

  function boot() {
    initDrawer();
    initChipDismiss();
    var root = getRoot();
    var defaultMode = root && root.getAttribute('data-default-sort-mode');
    if (defaultMode && !readPersist()) {
      // A page that opens already sorted (search results: Best Match) shows that sort as active, exactly as if
      // it had been picked, so the chip, the drawer and "clear" all behave the usual way.
      window.applyCollectionSort(defaultMode, root.getAttribute('data-default-sort-label') || '', { silent: true });
    } else {
      window.applyStoredCollectionSortIfAny();
    }
    if (typeof window.syncCollectionSortDrawerVisuals === 'function') {
      window.syncCollectionSortDrawerVisuals();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
