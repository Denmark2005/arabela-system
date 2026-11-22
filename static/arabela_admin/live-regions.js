/*
 * Live regions: keeps the numbers and lists of a page current WITHOUT a reload, so a customer's new reservation shows up in an
 * open admin page by itself (no loading screen, no blink, the scroll position stays).
 *
 * A page opts in by marking the parts that show reservation data:   <div data-live-region="quick-verify"> ... </div>
 * When something may have changed, this script asks the server for a fresh copy of the same page in the BACKGROUND, compares each
 * marked part with what the server sent the last time, and swaps only the parts that really changed.
 *
 * Two kinds of parts
 *   - an HTML part:  data-live-region="name"   its children are replaced with the fresh ones (Alpine and the pager pick the new rows up),
 *   - a DATA part:   <script type="application/json" data-live-data="name" data-live-watch=".css-selector">  a page that draws its rows
 *     from a JSON array (Reservation Records) gives the fresh JSON to the function it registered in window.ArabelaLiveData[name];
 *     data-live-watch names the element(s) the person works in (pointer / typing / selection there hold the update).
 *
 * Optional attributes on a part
 *   data-live-hold="css selector"   while anything on the page matches (an open actions panel, an open date calendar, an open help
 *                                   bubble), the update waits.
 *   data-live-strip="selector"      things inside the part that are not "content" (a "New" tag that goes away once it has been seen) do
 *                                   not count as a change; the part still gets the full fresh markup when it does change.
 *   data-live-attrs="a b"           attributes of the part's own element that the server draws from the data (a page-count flag): they
 *                                   count as content, and are copied over with it.
 *   window.ArabelaLiveBaseline[name]  a page whose own start-up script changes a part before this script runs gives the part as the
 *                                   server drew it (a string), so "what changed" is always compared with what the server sent.
 *
 * What starts a check
 *   - the bell found a new / a gone notification ("arabela-feed-changed", sent by notifications-live.js; the customer pages send it
 *     from customer-badges-live.js when a badge number changes),
 *   - the tab comes back into view, the connection returns, or the page returns from the browser's history,
 *   - a slow safety check every minute while the tab is on screen (catches what no notification covers, e.g. another
 *     staff member's walk-in booking).
 *   Never more often than every 10 seconds, never while the tab is in the background.
 *
 * When a fresh part is NOT swapped yet (it waits, and is applied the moment the person is done)
 *   - a pop-up / confirm window / receipt window is open,
 *   - the pointer moved over the part, or the person pressed, touched or scrolled it, in the last few seconds,
 *   - the person is typing in a field, is moving through the part with the keyboard, or has text selected in the part,
 *   - something marked data-live-hold is open.
 * If it has waited a long time a small bar says so ("Update now"). If the background check fails three times in a row, the bar offers
 * a normal refresh. Nothing is ever lost: the page is only changed by replacing the marked parts with what the server drew.
 *
 * After a part is swapped the page is told:  window event "arabela-region-updated" { detail: { name } }  (a page that counts or filters
 * the rows in its own script listens to it and redoes that work).
 *
 * Safety: an HTML part whose fresh markup contains a <script> is never swapped (scripts would not run); a session that ended stops the
 * checks quietly (the bell already says "signed out"); every step is wrapped so a problem here can never break the page.
 */
(function () {
  'use strict';

  var found = document.querySelectorAll('[data-live-region], [data-live-data]');
  if (!found.length || window.ArabelaLive) { return; }

  var MIN_GAP_MS = 10000;          // never ask the server more often than this
  var CATCHUP_MS = 60000;          // the slow safety check
  var REQUEST_TIMEOUT_MS = 15000;
  var POINTER_ACTIVE_MS = 6000;    // the pointer was over the part (or the part was touched / scrolled) this recently
  var CLICK_ACTIVE_MS = 2000;      // a press anywhere on the page this recently: the person is about to act
  var KEY_ACTIVE_MS = 20000;       // typing this recently in a field
  var NAV_ACTIVE_MS = 6000;        // Tab / arrows / Enter this recently while a control of the part has the focus
  var BUSY_RETRY_MS = 1500;        // while a fresh part waits, look again this often
  var HELD_BAR_MS = 45000;         // a part has waited this long: tell the person
  var FAILURES_FOR_BAR = 3;
  var MAX_PART_CHARS = 1500000;

  // ------------------------------------------------------------------ the parts
  function normalize(text) {
    // Whitespace does not matter, and a form token differs on every render: neither may count as "changed".
    return String(text).replace(/<input[^>]*name="csrfmiddlewaretoken"[^>]*>/g, '<input csrf>').replace(/\s+/g, ' ').trim();
  }

  // What "the same" means for a part: its markup without the whitespace and without anything the server may draw differently
  // on every render, plus the few attributes of the part itself that the page also keeps current.
  function attributesOf(node, wanted) {
    if (!wanted.length) { return null; }
    var out = {};
    wanted.forEach(function (name) { out[name] = node.getAttribute(name); });
    return out;
  }
  function signature(part, content, attrs) {
    var text = String(content);
    if (part.strip) {
      // data-live-strip="selector": things that are not "content" (a "New" tag that goes away once it has been seen) do not count as a
      // change. Taken out of an inert copy (nothing runs, nothing loads); the part itself always gets the full fresh markup.
      var body = new DOMParser().parseFromString('<body>' + text + '</body>', 'text/html').body;
      Array.prototype.forEach.call(body.querySelectorAll(part.strip), function (node) { node.parentNode.removeChild(node); });
      text = body.innerHTML;
    }
    return normalize(text) + (attrs ? '|' + JSON.stringify(attrs) : '');
  }

  var baseline = window.ArabelaLiveBaseline || {};
  var parts = {};          // name -> { kind, el, zones, hold, snapshot, pending: { text, sig, since, doc, attrs } | null, touchedAt }
  var names = [];

  function watch(part, node) {
    part.zones.push(node);
    function touch() { part.touchedAt = Date.now(); }
    ['pointermove', 'pointerdown', 'touchstart', 'touchmove', 'wheel'].forEach(function (type) { node.addEventListener(type, touch, { passive: true }); });
    node.addEventListener('scroll', touch, { passive: true });
  }

  Array.prototype.forEach.call(found, function (el) {
    var isData = el.hasAttribute('data-live-data');
    var name = el.getAttribute(isData ? 'data-live-data' : 'data-live-region');
    if (!name || parts[name]) { return; }
    // A data part's JSON may live in another element (Django's json_script cannot carry extra attributes): data-live-source="#its-id".
    var sourceSelector = isData ? (el.getAttribute('data-live-source') || '') : '';
    var source = sourceSelector ? document.querySelector(sourceSelector) : el;
    if (isData && !source) { return; }
    var part = {
      kind: isData ? 'data' : 'html', el: el, sourceSelector: sourceSelector, zones: [], hold: el.getAttribute('data-live-hold') || '', pending: null, touchedAt: 0,
      strip: isData ? '' : (el.getAttribute('data-live-strip') || ''),
      attrNames: isData ? [] : (el.getAttribute('data-live-attrs') || '').split(/[\s,]+/).filter(Boolean)    // attributes of the part itself kept current too
    };
    // This script runs before the page's deferred scripts (Alpine, the pager), so what is here now is what the server sent;
    // a page whose own inline script changes the part earlier says so with ArabelaLiveBaseline.
    part.snapshot = signature(part, typeof baseline[name] === 'string' ? baseline[name] : (isData ? source.textContent : el.innerHTML), attributesOf(el, part.attrNames));
    parts[name] = part;
    names.push(name);
    if (isData) {
      var selector = el.getAttribute('data-live-watch');
      if (selector) { Array.prototype.forEach.call(document.querySelectorAll(selector), function (node) { watch(part, node); }); }
    } else {
      watch(part, el);
    }
  });

  // ------------------------------------------------------------------ what the person is doing
  var lastPressAt = 0, lastKeyAt = 0, lastNavKeyAt = 0;
  var NAV_KEYS = { Tab: 1, Enter: 1, ' ': 1, ArrowUp: 1, ArrowDown: 1, ArrowLeft: 1, ArrowRight: 1, Home: 1, End: 1, PageUp: 1, PageDown: 1 };
  document.addEventListener('pointerdown', function () { lastPressAt = Date.now(); }, true);
  document.addEventListener('keydown', function (e) {
    var now = Date.now();
    lastKeyAt = now;
    if (NAV_KEYS[e.key]) { lastNavKeyAt = now; }   // moving around with the keyboard; Escape (closing a pop-up) is not one of these
  }, true);

  function isVisible(node) {
    if (!node.getClientRects().length) { return false; }
    var style = window.getComputedStyle(node);
    return style.visibility !== 'hidden' && style.display !== 'none' && parseFloat(style.opacity) !== 0;
  }

  // A pop-up is open: the shared confirm window, or any full-screen layer / dialog that is showing.
  function overlayOpen() {
    var dialog = document.getElementById('adm-dialog');
    if (dialog && dialog.getAttribute('aria-hidden') === 'false') { return true; }
    // A window that fades in (its card is invisible for its first moments) says it is open with data-overlay-root + aria-hidden="false".
    if (document.querySelector('[data-overlay-root][aria-hidden="false"]')) { return true; }
    var layers = document.querySelectorAll('.fixed.inset-0, [role="dialog"], [aria-modal="true"]');
    for (var i = 0; i < layers.length; i++) {
      if (!layers[i].hasAttribute('data-live-ignore') && isVisible(layers[i])) { return true; }
    }
    return false;
  }

  function inZones(part, node) {
    if (!node) { return false; }
    for (var i = 0; i < part.zones.length; i++) { if (part.zones[i].contains(node)) { return true; } }
    return false;
  }

  function busy(part) {
    var now = Date.now();
    if (overlayOpen()) { return true; }
    if (part.hold) {
      try { if (document.querySelector(part.hold)) { return true; } } catch (e) { /* a bad selector must not stop the page */ }
    }
    if (now - lastPressAt < CLICK_ACTIVE_MS) { return true; }
    if (now - part.touchedAt < POINTER_ACTIVE_MS) { return true; }
    var active = document.activeElement;
    if (active && active !== document.body) {
      var editable = /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) || active.isContentEditable;
      if (editable && now - lastKeyAt < KEY_ACTIVE_MS) { return true; }                            // typing in a field
      if (!editable && inZones(part, active) && now - lastNavKeyAt < NAV_ACTIVE_MS) { return true; }   // Tab / arrows / Enter inside the part
    }
    var selection = window.getSelection ? window.getSelection() : null;
    if (selection && !selection.isCollapsed && String(selection).length && inZones(part, selection.anchorNode)) { return true; }
    return false;
  }

  // ------------------------------------------------------------------ the small bar
  var bar = null, barMode = '';
  function ensureBar() {
    if (bar) { return bar; }
    bar = document.createElement('div');
    bar.id = 'arabela-live-bar';
    bar.setAttribute('role', 'status');
    bar.setAttribute('aria-live', 'polite');
    bar.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99990;display:none;align-items:center;gap:10px;' +
      'max-width:calc(100vw - 24px);padding:10px 14px;border-radius:10px;background:#101828;color:#fff;font:500 13px/1.4 system-ui,sans-serif;' +
      'box-shadow:0 8px 24px rgba(16,24,40,.28);';
    var text = document.createElement('span');
    text.setAttribute('data-live-bar-text', '');
    var go = document.createElement('button');
    go.type = 'button';
    go.setAttribute('data-live-bar-go', '');
    go.style.cssText = 'border:0;border-radius:7px;background:#fff;color:#101828;padding:5px 11px;font:600 12px system-ui,sans-serif;cursor:pointer;white-space:nowrap;';
    var close = document.createElement('button');
    close.type = 'button';
    close.setAttribute('aria-label', 'Hide this message');
    close.textContent = '×';
    close.style.cssText = 'border:0;background:transparent;color:#d0d5dd;font:400 18px/1 system-ui,sans-serif;cursor:pointer;padding:0 2px;';
    close.addEventListener('click', function () { bar.style.display = 'none'; });
    go.addEventListener('click', function () {
      if (barMode === 'failed') { window.location.reload(); } else { applyPending(true); }
    });
    bar.appendChild(text);
    bar.appendChild(go);
    bar.appendChild(close);
    document.body.appendChild(bar);
    return bar;
  }
  function showBar(mode) {
    var el = ensureBar();
    barMode = mode;
    el.querySelector('[data-live-bar-text]').textContent = mode === 'failed'
      ? 'This page could not update by itself.'
      : 'New information is waiting for this page. It updates as soon as you are done.';
    el.querySelector('[data-live-bar-go]').textContent = mode === 'failed' ? 'Refresh' : 'Update now';
    el.style.display = 'flex';
  }
  function hideBar(mode) {
    if (bar && (!mode || barMode === mode)) { bar.style.display = 'none'; }
  }

  // ------------------------------------------------------------------ swapping
  var failures = 0, stopped = false, retryTimer = null;

  function swap(name) {
    var part = parts[name];
    var pending = part.pending;
    if (part.kind === 'data') {
      var handler = window.ArabelaLiveData && window.ArabelaLiveData[name];
      if (typeof handler !== 'function') { part.pending = null; return; }      // the page did not register how to take new data: leave it
      handler(pending.text, pending.doc);
    } else {
      var el = part.el;
      var top = el.scrollTop;
      var run = function () { el.innerHTML = pending.text; };
      // The pager (pager.js) goes back to page 1 when the rows change; a background update must keep the page the person is on.
      if (window.arabelaPager && typeof window.arabelaPager.keepPage === 'function') { window.arabelaPager.keepPage(run); } else { run(); }
      el.scrollTop = top;
      if (pending.attrs) {
        Object.keys(pending.attrs).forEach(function (attr) {
          if (pending.attrs[attr] === null) { el.removeAttribute(attr); } else { el.setAttribute(attr, pending.attrs[attr]); }
        });
      }
    }
    part.snapshot = pending.sig;
    part.pending = null;
    try { window.dispatchEvent(new CustomEvent('arabela-region-updated', { detail: { name: name } })); } catch (e) { /* old browser */ }
  }

  // force = the person pressed "Update now": swap even if they were busy (a pop-up that is open is still respected).
  function applyPending(force) {
    clearTimeout(retryTimer);
    var waiting = false, longest = 0;
    names.forEach(function (name) {
      var part = parts[name];
      if (!part.pending || !part.el.isConnected) { return; }
      var hold = force ? overlayOpen() : busy(part);
      if (hold) { waiting = true; longest = Math.max(longest, Date.now() - part.pending.since); return; }
      try { swap(name); } catch (err) { failures += 1; report('could not update ' + name, err); }
    });
    if (waiting) {
      retryTimer = setTimeout(function () { applyPending(false); }, BUSY_RETRY_MS);
      if (longest >= HELD_BAR_MS) { showBar('held'); }
    } else {
      hideBar('held');
    }
  }

  function report(what, err) {
    if (window.console && console.error) { console.error('Live update: ' + what, err); }
  }

  // ------------------------------------------------------------------ asking the server
  var timer = null, inFlight = false, lastFetchAt = 0, dirty = false;

  function schedule(ms) {
    clearTimeout(timer);
    if (!stopped) { timer = setTimeout(run, ms); }
  }

  function request() {
    if (stopped) { return; }
    dirty = true;
    schedule(0);
  }

  function run() {
    if (stopped || inFlight) { return; }
    if (document.hidden) { return; }   // it catches up when the tab comes back into view
    var wait = MIN_GAP_MS - (Date.now() - lastFetchAt);
    if (wait > 0) { schedule(wait); return; }
    check();
  }

  function compare(text) {
    var doc = new DOMParser().parseFromString(text, 'text/html');
    names.forEach(function (name) {
      var part = parts[name];
      if (!part.el.isConnected) { return; }
      var fresh = part.kind === 'data'
        ? doc.querySelector(part.sourceSelector || '[data-live-data="' + name + '"]')
        : doc.querySelector('[data-live-region="' + name + '"]');
      if (!fresh) { return; }                                    // the server drew the page without this part now: leave it as it is
      var content = part.kind === 'data' ? fresh.textContent : fresh.innerHTML;
      if (content.length > MAX_PART_CHARS) { return; }
      if (part.kind === 'html' && /<script[\s>]/i.test(content)) { return; }   // never swap what needs scripts to work
      var attrs = part.kind === 'html' ? attributesOf(fresh, part.attrNames) : null;
      var sig = signature(part, content, attrs);
      if (sig === part.snapshot) { part.pending = null; return; }     // nothing changed (or it changed back)
      part.pending = { text: content, sig: sig, doc: doc, attrs: attrs, since: part.pending && part.pending.sig === sig ? part.pending.since : Date.now() };
    });
    applyPending(false);
  }

  function check() {
    inFlight = true;
    lastFetchAt = Date.now();
    dirty = false;
    var controller = window.AbortController ? new AbortController() : null;
    var abort = setTimeout(function () { if (controller) { controller.abort(); } }, REQUEST_TIMEOUT_MS);
    fetch(window.location.pathname + window.location.search, {
      credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'text/html' },
      signal: controller ? controller.signal : undefined
    }).then(function (res) {
      if (res.status === 401 || res.status === 403) { stopped = true; return null; }
      if (res.redirected) {      // sent to the sign-in page: the session ended; the bell says so, this stops quietly
        var to = new URL(res.url, window.location.href);
        if (to.pathname !== window.location.pathname) { stopped = true; return null; }
      }
      if (!res.ok) { throw new Error('http ' + res.status); }
      if ((res.headers.get('content-type') || '').indexOf('text/html') === -1) { throw new Error('not html'); }
      return res.text();
    }).then(function (text) {
      if (text === null) { return; }
      failures = 0;
      hideBar('failed');
      compare(text);
    }).catch(function () {
      failures += 1;                                   // slow or offline: say nothing, like the bell; the bar appears after a few in a row
      if (failures >= FAILURES_FOR_BAR) { showBar('failed'); }
    }).then(function () {
      clearTimeout(abort);
      inFlight = false;
      if (dirty) { schedule(MIN_GAP_MS); }
    });
  }

  // ------------------------------------------------------------------ what starts a check
  window.addEventListener('arabela-feed-changed', request);
  window.addEventListener('online', request);
  window.addEventListener('pageshow', function (e) { if (e.persisted) { request(); } });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && (dirty || Date.now() - lastFetchAt > 20000)) { request(); }
  });
  setInterval(function () { if (!document.hidden && !stopped) { request(); } }, CATCHUP_MS);

  window.ArabelaLive = {
    refresh: request,
    applyNow: function () { applyPending(true); },
    regions: names,
    // Read-only, for the tests and for anyone checking why a part has not updated.
    status: function () {
      return {
        regions: names,
        waiting: names.filter(function (name) { return !!parts[name].pending; }),
        overlayOpen: overlayOpen(),
        failures: failures,
        stopped: stopped,
        checkedAt: lastFetchAt
      };
    }
  };
})();
