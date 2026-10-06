/*
 * Keeps the number badges on the customer navigation (the bell = unread messages, the person icon =
 * reservations with news) current WITHOUT a page change.
 *
 * The server works those numbers out when a page is built, so a page that was already open -- the
 * homepage, say -- never learned about a message that arrived afterwards, and the badge only showed up
 * "suddenly" once the customer clicked through to another page. This asks the small JSON endpoint
 * (gowns.views.notification_badges, which uses the very same counting code as the page badges) and
 * redraws every badge on the page:
 *   - when the customer comes back to the tab / app, or a page is restored with the Back button,
 *   - when the connection returns,
 *   - and every ~45 seconds while the page is on screen (never while it is in the background).
 *
 * Every badge to keep current is marked in the templates:  data-live-badge="messages" | "reservations"
 * on the button/link that holds it (+ optional data-live-badge-style for its position). Loaded only for
 * signed-in customers; the endpoint URL comes from this script tag's data-live-badges-url.
 */
(function () {
  'use strict';

  var tag = document.querySelector('script[data-live-badges-url]');
  if (!tag || window.ArabelaBadges) { return; }
  var url = tag.getAttribute('data-live-badges-url');

  var POLL_MS = 45000;          // while the page is on screen
  var MIN_GAP_MS = 5000;        // never ask more often than this, whatever fires
  var MAX_BACKOFF_MS = 300000;  // when the server is slow or unreachable
  var TIMEOUT_MS = 12000;

  var timer = null, inFlight = false, failures = 0, lastFetch = 0, stopped = false;

  function toCount(value) {
    var n = parseInt(value, 10);
    return n > 0 ? n : 0;
  }
  function label(count) { return count > 99 ? '99+' : String(count); }

  function badgeOf(host) {
    var found = null;
    Array.prototype.forEach.call(host.children, function (child) {
      if (child.classList && child.classList.contains('icon-count-badge')) { found = child; }
    });
    return found;
  }

  // Makes every badge of this kind show `count` (none at all when it is 0). Only touches a badge whose
  // text is wrong, so a number that did not change never flickers; a number that GREW pops in again.
  function apply(kind, count) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-live-badge="' + kind + '"]'), function (host) {
      var badge = badgeOf(host);
      if (!count) {
        if (badge) { host.removeChild(badge); }
        return;
      }
      var text = label(count);
      if (badge && badge.textContent.trim() === text) { return; }
      var previous = badge ? toCount(badge.textContent) : 0;
      var next = document.createElement('span');
      next.className = 'icon-count-badge' + (count > previous ? ' badge-pop' : '');
      var style = (badge && badge.getAttribute('style')) || host.getAttribute('data-live-badge-style');
      if (style) { next.setAttribute('style', style); }
      next.textContent = text;
      if (badge) { host.replaceChild(next, badge); } else { host.appendChild(next); }
    });
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped || document.hidden) { return; }   // nothing to do while the page is in the background
    var wait = failures ? Math.min(MAX_BACKOFF_MS, POLL_MS * Math.pow(2, failures)) : POLL_MS;
    timer = setTimeout(refresh, wait * (0.9 + Math.random() * 0.2));   // a little jitter
  }

  function refresh(force) {
    if (stopped || inFlight) { return Promise.resolve(); }
    if (force !== true && Date.now() - lastFetch < MIN_GAP_MS) { schedule(); return Promise.resolve(); }
    inFlight = true;
    lastFetch = Date.now();
    var controller = window.AbortController ? new AbortController() : null;
    var abort = setTimeout(function () { if (controller) { controller.abort(); } }, TIMEOUT_MS);
    return fetch(url, {
      credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' },
      signal: controller ? controller.signal : undefined
    }).then(function (res) {
      if (res.status === 401 || res.status === 403) { stopped = true; return null; }   // signed out: stop asking
      if (!res.ok) { throw new Error('http ' + res.status); }
      if ((res.headers.get('content-type') || '').indexOf('json') === -1) { throw new Error('not json'); }
      return res.json();
    }).then(function (data) {
      if (!data) { return; }
      failures = 0;
      apply('messages', toCount(data.messages));
      apply('reservations', toCount(data.reservations));
    }).catch(function () {
      failures += 1;   // slow or offline: say nothing, try again a little later
    }).then(function () {
      clearTimeout(abort);
      inFlight = false;
      schedule();
    });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { clearTimeout(timer); } else { refresh(); }   // back on the page: catch up now
  });
  window.addEventListener('focus', function () { refresh(); });
  // A page restored from the back/forward cache is not rebuilt by the server, so it can be arbitrarily stale.
  window.addEventListener('pageshow', function (event) { if (event.persisted) { refresh(true); } });
  window.addEventListener('online', function () { failures = 0; refresh(true); });

  window.ArabelaBadges = { refresh: function () { return refresh(true); } };
  schedule();
})();
