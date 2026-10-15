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
 *
 * It also notices when the sign-in in this browser is no longer the one this page was opened for -- another
 * tab signed in as someone else, or signed out -- and says so with a bar at the top, instead of the page
 * quietly acting for a different account the next time it is refreshed or a reservation is confirmed.
 * The page's own account comes from <body data-user-key> (or this script tag's data-user-id on the few pages
 * that are not built from base.html).
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

  var pageUser = (document.body && document.body.dataset && document.body.dataset.userKey) || tag.getAttribute('data-user-id') || '';
  if (pageUser === 'guest') { pageUser = ''; }

  // A bar across the top of the page. The page stays as it is (nothing is changed behind the customer's back);
  // the button reloads it for whoever is signed in now.
  function showAccountNotice(signedOut) {
    if (document.getElementById('arabela-account-notice')) { return; }
    var bar = document.createElement('div');
    bar.id = 'arabela-account-notice';
    bar.setAttribute('role', 'alert');
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483000;display:flex;flex-wrap:wrap;gap:8px 14px;' +
      'align-items:center;justify-content:center;padding:12px 16px;background:#111;color:#fff;font:500 14px/1.4 system-ui,sans-serif;text-align:center;';
    var text = document.createElement('span');
    text.textContent = signedOut
      ? 'You were signed out in another tab or window.'
      : 'The account signed in on this browser changed in another tab or window. This page still shows the previous account.';
    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Reload this page';
    button.style.cssText = 'border:1px solid #fff;background:#fff;color:#111;border-radius:6px;padding:6px 14px;font:600 13px system-ui,sans-serif;cursor:pointer;';
    button.addEventListener('click', function () { window.location.reload(); });
    bar.appendChild(text);
    bar.appendChild(button);
    document.body.appendChild(bar);
  }

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
      if (res.status === 401 || res.status === 403) {   // signed out: stop asking, and say so
        stopped = true;
        if (pageUser) { showAccountNotice(true); }
        return null;
      }
      if (!res.ok) { throw new Error('http ' + res.status); }
      if ((res.headers.get('content-type') || '').indexOf('json') === -1) { throw new Error('not json'); }
      return res.json();
    }).then(function (data) {
      if (!data) { return; }
      if (pageUser && data.user !== undefined && String(data.user) !== String(pageUser)) {
        stopped = true;                   // someone else is signed in now: these numbers are not this page's
        showAccountNotice(false);
        return;
      }
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
