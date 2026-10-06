/*
 * The admin panel's live "Needs Attention" bell.
 *
 * The server draws the bell when a page loads; this script keeps it current afterwards, so a
 * customer's reservation shows up without anyone refreshing. Every few seconds it asks the
 * live feed (api/notifications/, see arabela_admin/notifications.py) what needs attention. When
 * something changed it swaps the fresh HTML into the bell, and for anything NEW it plays a
 * chime, shows a toast, flags the tab title and -- if switched on -- raises a desktop pop-up.
 *
 * What it relies on (all in templates/arabela_admin/partials/notifications.html):
 *   - #arabela-notifier             the bell's root; its data-notif-* attributes are the settings
 *   - [data-notif-region="..."]     badge, chip, list, footer, modal: the parts that get swapped
 *   - li[data-key][data-alert]      every notification row (stable identity + whether it alerts)
 *   - [data-notif-action] / [data-notif-filter]   the buttons inside the swapped HTML
 *
 * Nothing here is stored on the server. What each person has already seen/heard lives in their
 * own browser (localStorage), so it needs no database and works the same on every admin page.
 */
(function () {
  'use strict';

  var root = document.getElementById('arabela-notifier');
  if (!root || window.ArabelaNotifier) { return; }

  var cfg = {
    feed: root.getAttribute('data-notif-feed'),
    login: root.getAttribute('data-notif-login'),
    user: root.getAttribute('data-notif-user') || '0',
    sound: root.getAttribute('data-notif-sound'),
    icon: root.getAttribute('data-notif-icon')
  };

  var VISIBLE_ROWS = 6;              // rows the dropdown shows (the rest are in "View all")
  var FIRST_POLL_MS = 10000;         // the page was just drawn by the server, so no rush
  var POLL_VISIBLE_MS = 15000;       // how often to check while you are looking at the page
  var POLL_HIDDEN_MS = 30000;        // ...and while the tab is in the background
  var MAX_BACKOFF_MS = 120000;       // when the server is slow or unreachable
  var MIN_GAP_MS = 3000;             // never poll more often than this, whatever fires
  var REQUEST_TIMEOUT_MS = 12000;
  var TOAST_MS = 9000;
  var MAX_TOASTS = 3;
  var ANNOUNCED_TTL_MS = 6 * 60 * 60 * 1000;

  // ------------------------------------------------------------------ storage (per browser)
  var NS = 'arabela.notif.';
  var SEEN_KEY = 'seen.' + cfg.user;            // keys of the notifications this person has looked at
  var ANNOUNCED_KEY = 'announced.' + cfg.user;  // what has already been heard / toasted (shared by tabs)
  var PREFS_KEY = 'prefs';                      // { sound, desktop } -- per browser

  function load(name, fallback) {
    try {
      var raw = window.localStorage.getItem(NS + name);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  }
  function save(name, value) {
    try { window.localStorage.setItem(NS + name, JSON.stringify(value)); } catch (e) { /* private mode: carry on without saving */ }
  }

  var prefs = readPrefs();
  function readPrefs() {
    var stored = load(PREFS_KEY, {});
    return { sound: stored.sound !== false, desktop: stored.desktop === true };
  }

  // ------------------------------------------------------------------ state
  var initialKeys = [];
  try {
    var keysEl = document.getElementById('arabela-notif-keys');
    initialKeys = keysEl ? JSON.parse(keysEl.textContent || '[]') : [];
  } catch (e) { initialKeys = []; }

  var version = root.getAttribute('data-notif-version') || '';
  var known = new Set(initialKeys);   // keys already on screen -- a key not in here is NEW
  var seen = (function () {
    var stored = load(SEEN_KEY, null);
    if (stored === null) {              // first time on this browser: whatever is waiting now isn't "new"
      save(SEEN_KEY, initialKeys);
      return new Set(initialKeys);
    }
    return new Set(stored);
  })();
  var visibleNew = new Set();           // rows to keep marked "New" while the bell is open
  var filter = 'all';                   // the "View all" window's current tab
  var pendingHtml = null;               // a swap held back while you are pointing at the list
  var isOpen = { dropdown: false, modal: false };

  // ------------------------------------------------------------------ small DOM helpers
  function region(name) { return root.querySelector('[data-notif-region="' + name + '"]'); }
  function modalList() { return root.querySelector('[data-notif-modal-list]'); }
  function rowsOf(ul) {
    return ul ? Array.prototype.filter.call(ul.children, function (el) {
      return el.tagName === 'LI' && el.hasAttribute('data-key');
    }) : [];
  }
  function keyOf(li) { return li.getAttribute('data-key'); }
  function findRow(container, key) {
    var rows = container ? container.querySelectorAll('li[data-key]') : [];
    for (var i = 0; i < rows.length; i++) { if (keyOf(rows[i]) === key) { return rows[i]; } }
    return null;
  }
  function openBell() { window.dispatchEvent(new CustomEvent('arabela-open-bell')); }
  function bellButton() { return root.querySelector('button[aria-label="Notifications"]'); }

  // ------------------------------------------------------------------ "New" marks and pinning
  // A notification is NEW to you when it is one that alerts (not something staff caused
  // themselves) and you have not looked at it yet -- i.e. it was not in the list the last time
  // you opened the bell. New rows are pinned to the top and tagged, so a customer who just
  // reserved is never buried under older overdue / pick-up items.
  function isFresh(li) {
    var key = keyOf(li);
    return li.getAttribute('data-alert') === '1' && (visibleNew.has(key) || !seen.has(key));
  }

  function decorate() {
    [region('list'), modalList()].forEach(function (ul, which) {
      if (!ul) { return; }
      var rows = rowsOf(ul), fresh = [], rest = [];
      rows.forEach(function (li) {
        if (isFresh(li)) { li.setAttribute('data-new', '1'); fresh.push(li); }
        else { li.removeAttribute('data-new'); rest.push(li); }
      });
      var ordered = fresh.concat(rest);
      var moved = ordered.some(function (li, i) { return rows[i] !== li; });
      if (moved) { ordered.forEach(function (li) { ul.appendChild(li); }); }
      if (which === 0) {   // the dropdown shows the first few, after pinning
        ordered.forEach(function (li, i) { li.hidden = i >= VISIBLE_ROWS; });
      }
    });
  }

  function currentKeys() {
    return new Set(rowsOf(modalList()).map(keyOf));
  }

  function commitSeen(keys) {
    var present = currentKeys();
    var next = new Set();
    seen.forEach(function (k) { if (present.has(k)) { next.add(k); } });
    keys.forEach(function (k) { next.add(k); });
    seen = next;
    save(SEEN_KEY, Array.from(seen));
  }

  function shownKeys(which) {
    var ul = which === 'dropdown' ? region('list') : modalList();
    return rowsOf(ul).filter(function (li) { return !li.hidden; }).map(keyOf);
  }

  function bellOpen() { return isOpen.dropdown || isOpen.modal; }

  // Called by the bell (Alpine $watch) whenever the dropdown or the "View all" window opens/closes.
  function onToggle(which, open) {
    isOpen[which] = !!open;
    if (open) {
      dismissToasts();   // you are looking at the list now; the toasts would only sit on top of it
      applyPending();
      var ul = which === 'dropdown' ? region('list') : modalList();
      visibleNew = new Set(rowsOf(ul).filter(function (li) { return !li.hidden && isFresh(li); }).map(keyOf));
      commitSeen(shownKeys(which));
    } else {
      visibleNew = new Set();
      applyPending();
    }
    decorate();
  }

  function hasFresh() {
    return rowsOf(region('list')).some(isFresh);
  }

  // ------------------------------------------------------------------ the "View all" filter tabs
  function applyFilter() {
    var ul = modalList();
    if (!ul) { return; }
    var tabs = root.querySelectorAll('[data-notif-filter]');
    var exists = Array.prototype.some.call(tabs, function (t) { return t.getAttribute('data-notif-filter') === filter; });
    if (!exists) { filter = 'all'; }
    rowsOf(ul).forEach(function (li) {
      li.hidden = filter !== 'all' && li.getAttribute('data-group') !== filter;
    });
    Array.prototype.forEach.call(tabs, function (t) {
      t.setAttribute('aria-pressed', t.getAttribute('data-notif-filter') === filter ? 'true' : 'false');
    });
  }

  // ------------------------------------------------------------------ swapping in fresh HTML
  function interacting() {
    // Don't move rows under a pointer that is about to click one.
    var dropdown = root.querySelector('.anf-dropdown');
    var body = region('modal');
    return !!((dropdown && dropdown.matches(':hover')) || (body && body.matches(':hover')));
  }

  function setHtml(html, name) {
    var el = region(name);
    if (el && typeof html[name] === 'string') { el.innerHTML = html[name]; }
  }

  function swapRegions(html) {
    setHtml(html, 'badge');
    setHtml(html, 'chip');
    if (interacting()) { pendingHtml = html; return; }
    pendingHtml = null;
    var list = region('list');
    var modalScroll = root.querySelector('.anf-modal-scroll');
    var listTop = list ? list.scrollTop : 0;
    var modalTop = modalScroll ? modalScroll.scrollTop : 0;
    ['list', 'footer', 'modal'].forEach(function (name) { setHtml(html, name); });
    decorate();
    applyFilter();
    if (list) { list.scrollTop = listTop; }
    modalScroll = root.querySelector('.anf-modal-scroll');
    if (modalScroll) { modalScroll.scrollTop = modalTop; }
  }

  function applyPending() {
    if (pendingHtml && !interacting()) { var html = pendingHtml; pendingHtml = null; swapRegions(html); }
  }
  root.addEventListener('mouseleave', function () { setTimeout(applyPending, 0); }, true);

  // On a phone the bell sits behind the "..." button, so that button gets a dot while anything needs you.
  var dotHost = document.querySelector('header button.z-99999.lg\\:hidden');
  function updateDot() {
    if (!dotHost) { return; }
    var count = parseInt(root.getAttribute('data-notif-count'), 10) || 0;
    var dot = dotHost.querySelector('.anf-dot');
    if (!count) { if (dot) { dot.remove(); } return; }
    if (!dot) {
      dot = document.createElement('span');
      dot.className = 'anf-dot';
      dot.setAttribute('aria-hidden', 'true');
      if (window.getComputedStyle(dotHost).position === 'static') { dotHost.style.position = 'relative'; }
      dotHost.appendChild(dot);
    }
    dot.setAttribute('data-urgent', (parseInt(root.getAttribute('data-notif-urgent'), 10) || 0) > 0 ? '1' : '0');
  }

  function ring() {
    var button = bellButton();
    if (!button) { return; }
    button.classList.remove('anf-ring');
    void button.offsetWidth;   // restart the animation
    button.classList.add('anf-ring');
    setTimeout(function () { button.classList.remove('anf-ring'); }, 1300);
  }

  // ------------------------------------------------------------------ sound
  var audio = null, unlocked = false, unlocking = null, soundBlocked = false, blockedNoted = false;

  function getAudio() {
    if (!audio) {
      audio = new Audio(cfg.sound);
      audio.preload = 'auto';
      audio.volume = 0.75;
    }
    return audio;
  }

  // Browsers refuse to play sound on a page until you have clicked or typed on it once. The
  // first such gesture quietly "primes" the audio so every later chime is allowed.
  function unlock() {
    if (unlocked || unlocking) { return; }
    var a = getAudio(), p;
    a.muted = true;
    try { p = a.play(); } catch (e) { a.muted = false; return; }
    unlocking = Promise.resolve(p).then(function () { return true; }, function () { return false; }).then(function (ok) {
      try { a.pause(); a.currentTime = 0; } catch (e) { /* nothing to undo */ }
      a.muted = false;
      unlocking = null;
      if (ok) { unlocked = true; soundBlocked = false; detachUnlock(); }
    });
  }
  var UNLOCK_EVENTS = ['pointerdown', 'keydown', 'touchend'];
  function detachUnlock() { UNLOCK_EVENTS.forEach(function (ev) { document.removeEventListener(ev, unlock, true); }); }
  UNLOCK_EVENTS.forEach(function (ev) { document.addEventListener(ev, unlock, true); });

  // Resolves true once the chime actually started, false if the browser refused.
  function playChime() {
    var go = function () {
      try {
        var a = getAudio();
        a.muted = false;
        a.currentTime = 0;
        return Promise.resolve(a.play()).then(function () {
          unlocked = true; soundBlocked = false;
          return true;
        }, function (err) {
          if (err && err.name === 'NotAllowedError') { soundBlocked = true; }
          return false;
        });
      } catch (e) { return Promise.resolve(false); }
    };
    return unlocking ? unlocking.then(go) : go();
  }

  // ------------------------------------------------------------------ toasts
  var toastWrap = null;
  function wrap() {
    if (!toastWrap) {
      toastWrap = document.createElement('div');
      toastWrap.className = 'anf-toast-wrap';
      toastWrap.setAttribute('role', 'region');
      toastWrap.setAttribute('aria-live', 'polite');
      toastWrap.setAttribute('aria-label', 'New notifications');
      document.body.appendChild(toastWrap);
    }
    return toastWrap;
  }

  // opts: kicker, text, chip (a node), note, href + hrefLabel, onAction + actionLabel, sticky.
  // Everything is set with textContent, so nothing a customer typed can ever become markup.
  function toast(opts) {
    var el = document.createElement('div');
    el.className = 'anf-toast';
    el.setAttribute('role', 'status');
    if (opts.sticky) { el.setAttribute('data-sticky', '1'); }
    if (opts.chip) { el.appendChild(opts.chip); }

    var body = document.createElement('div');
    body.className = 'anf-toast-body';
    var kicker = document.createElement('div');
    kicker.className = 'anf-toast-kicker';
    kicker.textContent = opts.kicker;
    body.appendChild(kicker);
    var text = document.createElement('div');
    text.className = 'anf-toast-text';
    text.textContent = opts.text;
    body.appendChild(text);
    if (opts.note) {
      var note = document.createElement('div');
      note.className = 'anf-toast-note';
      note.textContent = opts.note;
      body.appendChild(note);
    }
    var timer = null;
    function close() {
      clearTimeout(timer);
      if (el.parentNode) { el.parentNode.removeChild(el); }
    }
    if (opts.href || opts.onAction) {
      var actions = document.createElement('div');
      actions.className = 'anf-toast-actions';
      if (opts.href) {
        var link = document.createElement('a');
        link.className = 'anf-toast-btn';
        link.href = opts.href;
        link.textContent = opts.hrefLabel || 'View';
        actions.appendChild(link);
      }
      if (opts.onAction) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'anf-toast-btn';
        button.textContent = opts.actionLabel || 'Open';
        button.addEventListener('click', function () { opts.onAction(); close(); });
        actions.appendChild(button);
      }
      body.appendChild(actions);
    }
    el.appendChild(body);

    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'anf-toast-x';
    x.setAttribute('aria-label', 'Dismiss');
    x.textContent = '×';
    x.addEventListener('click', close);
    el.appendChild(x);

    function arm(ms) { clearTimeout(timer); if (!opts.sticky) { timer = setTimeout(close, ms); } }
    el.addEventListener('mouseenter', function () { clearTimeout(timer); });
    el.addEventListener('mouseleave', function () { arm(3000); });

    var container = wrap();
    container.appendChild(el);
    while (container.children.length > MAX_TOASTS) { container.removeChild(container.firstChild); }
    arm(TOAST_MS);
    return el;
  }

  function dismissToasts() {
    if (!toastWrap) { return; }
    Array.prototype.slice.call(toastWrap.children).forEach(function (el) {
      if (!el.hasAttribute('data-sticky')) { toastWrap.removeChild(el); }   // "signed out" stays until you act
    });
  }

  var lastHtml = null;
  function chipFor(key) {
    var row = findRow(root, key);
    if (!row && lastHtml && typeof lastHtml.modal === 'string') {   // the row's swap is being held back
      var holder = document.createElement('div');
      holder.innerHTML = lastHtml.modal;
      row = findRow(holder, key);
    }
    var chip = row ? row.querySelector('.anf-chip') : null;
    return chip ? chip.cloneNode(true) : null;
  }

  function showFresh(items) {
    var note = '';
    if (prefs.sound && soundBlocked && !blockedNoted) {
      note = 'Sound is blocked by your browser until you click on this page once.';
      blockedNoted = true;
    }
    if (items.length <= 2) {
      items.forEach(function (item, i) {
        toast({ kicker: item.module, text: item.title, chip: chipFor(item.key), href: item.url, hrefLabel: 'View', note: i === 0 ? note : '' });
      });
    } else {
      toast({ kicker: 'New notifications', text: items.length + ' new things need your attention.', onAction: openBell, actionLabel: 'Open', note: note });
    }
  }

  // A short message INSIDE the dropdown (for the two switches), so it never lands on top of the
  // very controls you just used. Falls back to a toast if the dropdown has no status line.
  var statusTimer = null;
  function status(text) {
    var el = root.querySelector('[data-notif-status]');
    if (!el) { toast({ kicker: 'Notifications', text: text }); return; }
    el.textContent = text;
    el.hidden = false;
    clearTimeout(statusTimer);
    statusTimer = setTimeout(function () { el.hidden = true; }, 8000);
  }

  // ------------------------------------------------------------------ tab title + desktop pop-up
  var baseTitle = document.title, titleCount = 0;
  function bumpTitle(n) { titleCount += n; document.title = '(' + titleCount + ' new) ' + baseTitle; }
  function restoreTitle() { if (titleCount) { titleCount = 0; document.title = baseTitle; } }

  function desktopNotify(items) {
    if (!prefs.desktop || !('Notification' in window) || Notification.permission !== 'granted') { return; }
    if (document.visibilityState === 'visible' && document.hasFocus()) { return; }   // you're looking at it
    try {
      var one = items.length === 1;
      var n = new Notification(one ? items[0].module : items.length + ' new notifications', {
        body: one ? items[0].title : items.slice(0, 3).map(function (i) { return i.title; }).join('\n'),
        icon: cfg.icon,
        // The same tag from two tabs replaces instead of doubling up.
        tag: 'arabela-' + items.map(function (i) { return i.key; }).sort().join(',')
      });
      n.onclick = function () {
        try { window.focus(); } catch (e) { /* ignore */ }
        if (one) { window.location.href = items[0].url; } else { openBell(); }
        n.close();
      };
    } catch (e) { /* some browsers only allow pop-ups from a service worker */ }
  }

  // ------------------------------------------------------------------ announcing what's new
  // Several tabs may notice the same new reservation. Whichever gets the lock first records it in
  // localStorage; the others see it was already announced and stay quiet -- one chime, one toast.
  // A tab only counts as having played the chime if the browser really let it play, so a
  // background tab that is not allowed to make noise never steals the chime from the tab you are using.
  function withLock(fn) {
    if (window.navigator.locks && window.navigator.locks.request) {
      return window.navigator.locks.request('arabela-notif-announce', fn);
    }
    return Promise.resolve().then(fn);
  }
  function readAnnounced() {
    var store = load(ANNOUNCED_KEY, {}), now = Date.now();
    Object.keys(store).forEach(function (k) { if (!store[k] || now - store[k].ts > ANNOUNCED_TTL_MS) { delete store[k]; } });
    return store;
  }
  function entry(store, key) {
    if (!store[key]) { store[key] = { ts: Date.now(), s: 0, t: 0 }; }
    return store[key];
  }

  function announce(fresh) {
    var visible = document.visibilityState === 'visible';
    return withLock(function () {
      var store = readAnnounced();
      var needSound = fresh.filter(function (i) { return !(store[i.key] && store[i.key].s); });
      var needToast = visible ? fresh.filter(function (i) { return !(store[i.key] && store[i.key].t); }) : [];
      var sound = prefs.sound && needSound.length ? playChime() : Promise.resolve(false);
      return sound.then(function (played) {
        if (played) { needSound.forEach(function (i) { entry(store, i.key).s = 1; }); }
        needToast.forEach(function (i) { entry(store, i.key).t = 1; });
        save(ANNOUNCED_KEY, store);
        return needToast;
      });
    }).then(function (toasts) {
      if (toasts && toasts.length && !bellOpen()) { showFresh(toasts); }
      if (visible) { ring(); } else { bumpTitle(fresh.length); }
      desktopNotify(fresh);
    });
  }

  // ------------------------------------------------------------------ polling
  var timer = null, inFlight = false, failures = 0, lastPoll = 0, stopped = false;

  function nextDelay() {
    var base = document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS;
    var wait = failures ? Math.min(MAX_BACKOFF_MS, base * Math.pow(2, failures)) : base;
    return wait * (0.9 + Math.random() * 0.2);   // a little jitter so open tabs don't all fire together
  }
  function schedule(ms) {
    clearTimeout(timer);
    if (!stopped) { timer = setTimeout(poll, ms); }
  }

  function sessionEnded() {
    stopped = true;
    toast({
      kicker: 'Signed out', text: 'Your session has ended. Sign in again to keep getting live alerts.',
      href: cfg.login, hrefLabel: 'Sign in', sticky: true
    });
  }

  function handle(data) {
    if (data.unchanged) { version = data.version || version; return; }
    lastHtml = data.html || {};
    root.setAttribute('data-notif-count', String(data.count || 0));
    root.setAttribute('data-notif-urgent', String(data.urgent || 0));
    swapRegions(lastHtml);
    updateDot();
    version = data.version || version;   // only once the swap worked, so a failed one is retried
    var items = data.items || [];
    var fresh = items.filter(function (i) { return i.alert && !known.has(i.key); });
    known = new Set(items.map(function (i) { return i.key; }));   // a key that left and comes back counts as new again
    if (fresh.length) {
      announce(fresh).catch(function (err) { report('could not announce', err); });
    }
  }

  function report(what, err) {
    if (window.console && console.error) { console.error('Notification bell: ' + what, err); }
  }

  function poll(force) {
    if (inFlight || stopped) { return Promise.resolve(); }
    if (!force && Date.now() - lastPoll < MIN_GAP_MS) { schedule(nextDelay()); return Promise.resolve(); }
    inFlight = true;
    lastPoll = Date.now();
    var controller = window.AbortController ? new AbortController() : null;
    var abort = setTimeout(function () { if (controller) { controller.abort(); } }, REQUEST_TIMEOUT_MS);
    return fetch(cfg.feed + '?v=' + encodeURIComponent(version), {
      credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' },
      signal: controller ? controller.signal : undefined
    }).then(function (res) {
      if (res.status === 401 || res.status === 403) { sessionEnded(); return null; }
      if (!res.ok) { throw new Error('http ' + res.status); }
      if ((res.headers.get('content-type') || '').indexOf('json') === -1) { throw new Error('not json'); }
      return res.json();
    }).then(function (data) {
      if (!data) { return; }
      failures = 0;
      try { handle(data); } catch (err) { report('could not update the bell', err); }
    }).catch(function () {
      failures += 1;   // slow or offline: say nothing, try again a little later
    }).then(function () {
      clearTimeout(abort);
      inFlight = false;
      schedule(nextDelay());
    });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      restoreTitle();
      if (hasFresh()) { ring(); }
      if (Date.now() - lastPoll > 4000) { schedule(250); return; }   // you're back: catch up now
    }
    schedule(nextDelay());
  });
  window.addEventListener('online', function () { failures = 0; lastPoll = 0; schedule(250); });   // reconnected: catch up now

  // Other tabs change what has been seen / the switches; follow along.
  window.addEventListener('storage', function (e) {
    if (e.key === NS + SEEN_KEY) { var stored = load(SEEN_KEY, null); if (stored) { seen = new Set(stored); decorate(); } }
    if (e.key === NS + PREFS_KEY) { prefs = readPrefs(); syncSwitches(); }
  });

  // ------------------------------------------------------------------ the two switches in the dropdown
  function syncSwitches() {
    var sound = root.querySelector('[data-notif-action="toggle-sound"]');
    if (sound) {
      sound.setAttribute('aria-pressed', prefs.sound ? 'true' : 'false');
      sound.title = 'Notification sound: ' + (prefs.sound ? 'on' : 'off');
    }
    var desktop = root.querySelector('[data-notif-action="toggle-desktop"]');
    if (desktop) {
      var supported = 'Notification' in window;
      var on = supported && prefs.desktop && Notification.permission === 'granted';
      desktop.hidden = !supported;
      desktop.setAttribute('aria-pressed', on ? 'true' : 'false');
      desktop.title = 'Desktop alerts: ' + (on ? 'on' : 'off');
    }
  }

  function toggleSound() {
    prefs.sound = !prefs.sound;
    save(PREFS_KEY, prefs);
    syncSwitches();
    status(prefs.sound ? 'Sound is on.' : 'Sound is off. New notifications will still show here.');
    if (prefs.sound) { playChime(); }   // the click is a gesture: you hear what you just switched on
  }

  function askPermission() {
    try {
      var result = Notification.requestPermission();
      if (result && result.then) { return result; }
    } catch (e) { /* fall through to the old callback style */ }
    return new Promise(function (resolve) { Notification.requestPermission(resolve); });
  }

  function toggleDesktop() {
    if (!('Notification' in window)) { return; }
    if (prefs.desktop) {
      prefs.desktop = false; save(PREFS_KEY, prefs); syncSwitches();
      status('Desktop alerts are off.');
      return;
    }
    var ask = Notification.permission === 'default' ? askPermission() : Promise.resolve(Notification.permission);
    ask.then(function (permission) {
      prefs.desktop = permission === 'granted';
      save(PREFS_KEY, prefs);
      syncSwitches();
      if (prefs.desktop) {
        status('Desktop alerts are on. You will get a pop-up when something new needs you while this tab is in the background.');
      } else {
        status('Desktop alerts are blocked. Allow notifications for this site in your browser (the padlock next to the address bar), then try again.');
      }
    });
  }

  root.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('[data-notif-action],[data-notif-filter]') : null;
    if (!el || !root.contains(el)) { return; }
    var action = el.getAttribute('data-notif-action');
    if (action === 'open-all') { window.dispatchEvent(new CustomEvent('arabela-open-all')); }
    else if (action === 'toggle-sound') { toggleSound(); }
    else if (action === 'toggle-desktop') { toggleDesktop(); }
    else if (el.hasAttribute('data-notif-filter')) { filter = el.getAttribute('data-notif-filter'); applyFilter(); }
  });

  // ------------------------------------------------------------------ go
  window.ArabelaNotifier = {
    onToggle: onToggle,
    pollNow: function () { return poll(true); }
  };
  getAudio();          // start fetching the chime now so it is ready when needed
  syncSwitches();
  decorate();
  applyFilter();
  updateDot();
  schedule(FIRST_POLL_MS);
})();
