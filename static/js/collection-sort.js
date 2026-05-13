(function () {
  var STORAGE_PREFIX = 'arabela_collection_sort:';
  var sortDrawerPendingMode = null;
  var sortDrawerPendingLabel = null;

  function pathKey() {
    return STORAGE_PREFIX + location.pathname;
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
    if (ps.length >= 2) return parsePrice(ps[1].textContent);
    if (ps.length === 1) return parsePrice(ps[0].textContent);
    return 0;
  }

  function rememberOriginal(grid) {
    if (!grid || grid.__sortOriginalOrder) return;
    grid.__sortOriginalOrder = getCards(grid).slice();
  }

  function restoreOriginal(grid) {
    if (!grid || !grid.__sortOriginalOrder || !grid.__sortOriginalOrder.length) return;
    var frag = document.createDocumentFragment();
    grid.__sortOriginalOrder.forEach(function (n) {
      frag.appendChild(n);
    });
    grid.appendChild(frag);
  }

  function sortGrid(mode) {
    var root = getRoot();
    var grid = getGrid(root);
    if (!grid) return;
    rememberOriginal(grid);
    var cards = getCards(grid);
    if (!cards.length) return;
    var sorted = cards.slice();
    if (mode === 'az') sorted.sort(function (a, b) { return getTitle(a).localeCompare(getTitle(b)); });
    else if (mode === 'za') sorted.sort(function (a, b) { return getTitle(b).localeCompare(getTitle(a)); });
    else if (mode === 'priceAsc') sorted.sort(function (a, b) { return getPrice(a) - getPrice(b); });
    else if (mode === 'priceDesc') sorted.sort(function (a, b) { return getPrice(b) - getPrice(a); });
    else return;
    var frag = document.createDocumentFragment();
    sorted.forEach(function (c) {
      frag.appendChild(c);
    });
    grid.appendChild(frag);
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

  window.applyCollectionSort = function (mode, label) {
    sortGrid(mode);
    if (label) persist(mode, label);
    if (label) showChip(label);
  };

  window.resetCollectionSort = function () {
    var root = getRoot();
    var grid = getGrid(root);
    restoreOriginal(grid);
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
    window.applyStoredCollectionSortIfAny();
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
