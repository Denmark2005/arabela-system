/*
 * The event-date calendar of the admin's New Booking page. It opens right under the date box, shows one month of ONE gown, and
 * marks every day the gown can't be booked (faded and crossed out, the way Active Reservations does it) with the reason a tap or a
 * hover shows underneath. The answers come from the server (the same rules as the check made after a date is chosen); this file
 * only draws them, so it never decides anything itself. Pick a free day and the page is told through onPick; saving a booking is
 * checked again by the server whatever this calendar showed.
 *
 *   arabelaAvailabilityCalendar.open(root, {
 *     anchor,           the date box (clicks on it are not "outside", and it gets the focus back)
 *     today, min,       ISO dates: today in the shop's time zone, and the first day that can be chosen
 *     selected,         the ISO date already chosen, or ''
 *     title,            what the calendar is for, e.g. "Wedding Gown 3 (Wedding Gown-WH-003)"
 *     load(month),      -> Promise of { days: { ISO date: { ok, pickup, returnDate, restUntil } | { ok: false, reason, why } } }
 *     onPick(iso), onClose()
 *   });
 *   arabelaAvailabilityCalendar.close(root, focusAnchor);  .isOpen(root)
 */
(function () {
    'use strict';

    var STYLE_ID = 'acal-style';
    var CSS = [
        '.acal { width: 100%; max-width: 20rem; box-sizing: border-box; margin-top: .5rem; padding: .7rem; border: 1px solid #d0d5dd; border-radius: .8rem; background: #fff; color: #1d2939; font-size: .8125rem; box-shadow: 0 6px 18px rgba(16,24,40,.08); }',
        '.dark .acal { background: #101828; border-color: #344054; color: #e4e7ec; box-shadow: none; }',
        '.acal[hidden] { display: none; }',
        '.acal-title { margin: 0 0 .35rem; font-weight: 600; font-size: .8125rem; }',
        '.acal-nav { display: flex; align-items: center; justify-content: space-between; margin-bottom: .25rem; }',
        '.acal-nav button { background: none; border: 0; padding: .2rem .65rem; border-radius: .4rem; color: #667085; font-size: 1.15rem; line-height: 1; cursor: pointer; }',
        '.acal-nav button:hover { background: rgba(152,162,179,.18); }',
        '.acal-month { font-weight: 500; }',
        '.acal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; text-align: center; }',
        '.acal-dow { font-size: .7rem; color: #98a2b3; }',
        '.acal-day { height: 2rem; padding: 0; border: 1px solid transparent; border-radius: .4rem; background: none; color: inherit; font: inherit; font-size: .8125rem; cursor: pointer; }',
        '.acal-day:hover { border-color: #98a2b3; }',
        '.acal-day:focus-visible, .acal-nav button:focus-visible { outline: 2px solid #465fff; outline-offset: 1px; }',
        '.acal-day--no { opacity: .38; text-decoration: line-through; cursor: not-allowed; }',
        '.acal-day--no:hover { border-color: transparent; }',
        '.acal-day--wait { opacity: .3; cursor: default; }',
        '.acal-day--today { outline: 1px dashed #98a2b3; }',
        '.acal-day--win { background: #D1E9FF; color: #175CD3; }',
        '.acal-day--pick { background: #FEF0C7; color: #B54708; }',
        '.acal-day--event, .acal-day--event:hover { background: #2E90FA; color: #fff; border-color: #2E90FA; }',
        '.dark .acal-day--win { background: rgba(46,144,250,.2); color: #84caff; }',
        '.dark .acal-day--pick { background: rgba(247,144,9,.22); color: #fec84b; }',
        '.acal-msg { margin: .5rem 0 0; padding: .45rem .6rem; border: 1px solid #e4e7ec; border-radius: .5rem; background: #f9fafb; color: #475467; font-size: .75rem; line-height: 1.4; }',
        '.dark .acal-msg { background: rgba(255,255,255,.04); border-color: #1d2939; color: #98a2b3; }',
        '.acal-msg--ok { background: #ecfdf3; border-color: #abefc6; color: #05603a; }',
        '.dark .acal-msg--ok { background: rgba(18,183,106,.12); border-color: rgba(18,183,106,.35); color: #75e0a7; }',
        '.acal-msg--no { background: #fef3f2; border-color: #fecdca; color: #912018; }',
        '.dark .acal-msg--no { background: rgba(240,68,56,.14); border-color: rgba(240,68,56,.35); color: #fda29b; }',
        '.acal-msg button { margin-left: .4rem; padding: 0; border: 0; background: none; color: inherit; font: inherit; text-decoration: underline; cursor: pointer; }',
        '.acal-legend { margin: .4rem 0 0; font-size: .7rem; color: #98a2b3; }'
    ].join('\n');

    // ---- dates: plain ISO strings and UTC, so the browser's own clock and time zone never move a day ----
    function pad2(n) { return String(n).padStart(2, '0'); }
    function isoToDate(iso) { var p = String(iso).split('-'); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
    function dateToIso(d) { return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()); }
    function shiftMonth(month, step) {
        var d = isoToDate(month + '-01');
        d.setUTCMonth(d.getUTCMonth() + step);
        return dateToIso(d).slice(0, 7);
    }
    function shiftDay(iso, step) {
        var d = isoToDate(iso);
        d.setUTCDate(d.getUTCDate() + step);
        return dateToIso(d);
    }
    function shortDay(iso) { return isoToDate(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }); }
    function longDay(iso) { return isoToDate(iso).toLocaleDateString('en-US', { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }); }
    function monthLabel(month) { return isoToDate(month + '-01').toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' }); }

    // The cells of one month: how many empty cells come before the 1st (Sunday first) and the ISO date of every day.
    function monthCells(month) {
        var first = isoToDate(month + '-01');
        var count = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
        var days = [];
        for (var i = 1; i <= count; i++) { days.push(month + '-' + pad2(i)); }
        return { blanks: first.getUTCDay(), days: days };
    }

    // What the line under the calendar says about one day.
    function describe(iso, info) {
        if (!info) {
            return { kind: 'unknown', text: longDay(iso) + ': the calendar could not be loaded for this day. It is checked again when you choose it.' };
        }
        if (info.ok) {
            return {
                kind: 'ok',
                text: longDay(iso) + ': available. Pick-up ' + shortDay(info.pickup) + ', event ' + shortDay(iso) + ', return '
                    + shortDay(info.returnDate) + '. After that the gown rests until ' + shortDay(info.restUntil) + '.'
            };
        }
        return { kind: 'no', text: longDay(iso) + ' — ' + (info.why || 'not available') };
    }

    function injectStyle() {
        if (document.getElementById(STYLE_ID)) { return; }
        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined) { node.textContent = text; }
        return node;
    }

    function isOpen(root) { return !!(root && root._acal); }

    function close(root, focusAnchor) {
        var st = root && root._acal;
        if (!st) { return; }
        root._acal = null;
        document.removeEventListener('mousedown', st.onOutside, true);
        document.removeEventListener('keydown', st.onKey, true);
        root.textContent = '';
        root.hidden = true;
        if (focusAnchor && st.cfg.anchor && st.cfg.anchor.focus) { st.cfg.anchor.focus(); }
        if (st.cfg.onClose) { st.cfg.onClose(); }
    }

    function open(root, cfg) {
        if (!root) { return; }
        close(root, false);
        injectStyle();

        var st = {
            cfg: cfg, cache: {}, seq: 0, loading: true, failed: false, hover: '', pinned: '',
            selected: cfg.selected || '',
            month: (cfg.selected || cfg.today).slice(0, 7)
        };
        root._acal = st;

        // ---- the parts that stay while the month changes ----
        root.className = 'acal';
        root.setAttribute('role', 'group');
        root.setAttribute('aria-label', 'Choose the event date');
        root.textContent = '';
        if (cfg.title) { root.appendChild(el('p', 'acal-title', cfg.title)); }
        var nav = el('div', 'acal-nav');
        var prev = el('button', '', '‹'); prev.type = 'button'; prev.setAttribute('aria-label', 'Previous month');
        var next = el('button', '', '›'); next.type = 'button'; next.setAttribute('aria-label', 'Next month');
        var label = el('span', 'acal-month');
        nav.appendChild(prev); nav.appendChild(label); nav.appendChild(next);
        root.appendChild(nav);
        var grid = el('div', 'acal-grid');
        root.appendChild(grid);
        var msg = el('p', 'acal-msg');
        msg.setAttribute('aria-live', 'polite');
        root.appendChild(msg);
        root.appendChild(el('p', 'acal-legend', 'Faded, crossed-out days are taken — hover or tap one to see why.'));
        root.hidden = false;

        function infoOf(iso) {
            var days = st.cache[iso.slice(0, 7)];
            return days ? days[iso] : null;
        }

        function say(iso) {
            msg.textContent = '';
            msg.className = 'acal-msg';
            if (st.loading) { msg.textContent = 'Checking the calendar…'; return; }
            if (st.failed) {
                msg.className = 'acal-msg acal-msg--no';
                msg.appendChild(document.createTextNode('The calendar could not be loaded just now. You can still choose a day; it is checked when you do.'));
                var retry = el('button', '', 'Try again'); retry.type = 'button';
                retry.addEventListener('click', function () { st.cache[st.month] = null; loadMonth(st.month); });
                msg.appendChild(retry);
                return;
            }
            var shown = iso || st.hover || st.pinned || st.selected;      // pinned: the taken day last tapped (a phone has no hover)
            if (!shown) { msg.textContent = 'Choose a free day.'; return; }
            var said = describe(shown, infoOf(shown));
            msg.className = 'acal-msg' + (said.kind === 'ok' ? ' acal-msg--ok' : said.kind === 'no' ? ' acal-msg--no' : '');
            msg.textContent = said.text;
        }

        function fill() {
            label.textContent = monthLabel(st.month);
            grid.textContent = '';
            ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach(function (d) { grid.appendChild(el('span', 'acal-dow', d)); });
            var cells = monthCells(st.month);
            for (var b = 0; b < cells.blanks; b++) { grid.appendChild(el('span')); }
            var chosen = st.selected ? infoOf(st.selected) : null;
            var tabbable = '';
            cells.days.forEach(function (iso) {
                var info = infoOf(iso);
                var btn = el('button', 'acal-day', String(+iso.slice(8)));
                btn.type = 'button';
                btn.dataset.iso = iso;
                var taken;
                if (st.loading) {
                    btn.className += ' acal-day--wait'; btn.disabled = true; taken = true;
                } else if (st.failed) {
                    taken = iso < (cfg.min || cfg.today);                      // no answers: only the days that have certainly passed are closed
                    if (taken) { btn.className += ' acal-day--no'; }
                } else {
                    taken = !(info && info.ok);
                    if (taken) { btn.className += ' acal-day--no'; }
                }
                if (!st.loading) {
                    btn.setAttribute('aria-label', describe(iso, st.failed && !taken ? null : info).text);
                    if (taken) { btn.setAttribute('aria-disabled', 'true'); }
                }
                if (iso === cfg.today) { btn.className += ' acal-day--today'; }
                if (chosen && chosen.ok && st.selected) {
                    if (iso === st.selected) { btn.className += ' acal-day--event'; btn.setAttribute('aria-pressed', 'true'); }
                    else if (iso === chosen.pickup) { btn.className += ' acal-day--pick'; }
                    else if (iso > chosen.pickup && iso <= chosen.returnDate) { btn.className += ' acal-day--win'; }
                }
                btn.tabIndex = -1;
                if (!tabbable && !st.loading && !taken && (iso === st.selected || !st.selected)) { tabbable = iso; }
                grid.appendChild(btn);
            });
            var fallback = grid.querySelector('.acal-day:not(.acal-day--no):not(.acal-day--wait)') || grid.querySelector('.acal-day');
            var stop = (tabbable && grid.querySelector('[data-iso="' + tabbable + '"]')) || fallback;
            if (stop) { stop.tabIndex = 0; }
            say('');
        }

        function loadMonth(month) {
            if (st.cache[month]) { st.loading = false; st.failed = false; fill(); return; }
            var seq = ++st.seq;
            st.loading = true; st.failed = false;
            fill();
            Promise.resolve().then(function () { return cfg.load(month); }).then(function (data) {
                if (root._acal !== st || seq !== st.seq) { return; }
                st.cache[month] = (data && data.days) || {};
                st.loading = false;
                fill();
                var after = shiftMonth(month, 1);                                 // the next month is fetched quietly, so paging forward is instant
                if (!st.cache[after]) {
                    Promise.resolve().then(function () { return cfg.load(after); }).then(function (more) {
                        if (root._acal === st && more && more.days) { st.cache[after] = more.days; }
                    }).catch(function () { /* quiet: it is asked for again if the page is turned to it */ });
                }
            }).catch(function () {
                if (root._acal !== st || seq !== st.seq) { return; }
                st.loading = false; st.failed = true;
                fill();
            });
        }

        // ---- what the staff member does ----
        prev.addEventListener('click', function () { st.month = shiftMonth(st.month, -1); st.hover = ''; st.pinned = ''; loadMonth(st.month); });
        next.addEventListener('click', function () { st.month = shiftMonth(st.month, 1); st.hover = ''; st.pinned = ''; loadMonth(st.month); });

        grid.addEventListener('click', function (event) {
            var btn = event.target.closest && event.target.closest('.acal-day');
            if (!btn || btn.disabled) { return; }
            var iso = btn.dataset.iso;
            if (btn.getAttribute('aria-disabled') === 'true') { st.hover = iso; st.pinned = iso; say(iso); return; }    // a taken day: say why, choose nothing
            st.selected = iso;
            cfg.onPick(iso);
            close(root, true);
        });
        grid.addEventListener('mouseover', function (event) {
            var btn = event.target.closest && event.target.closest('.acal-day');
            if (!btn || btn.disabled) { return; }
            st.hover = btn.dataset.iso; say(st.hover);
        });
        grid.addEventListener('mouseleave', function () { st.hover = ''; say(''); });
        grid.addEventListener('focusin', function (event) {
            var btn = event.target.closest && event.target.closest('.acal-day');
            if (btn && !btn.disabled) { st.hover = btn.dataset.iso; say(st.hover); }
        });

        st.onOutside = function (event) {
            if (!document.body.contains(root)) { close(root, false); return; }        // the line was removed from the form
            if (root.contains(event.target) || (cfg.anchor && cfg.anchor.contains(event.target))) { return; }
            close(root, false);
        };
        st.onKey = function (event) {
            if (event.key === 'Escape') { event.stopPropagation(); close(root, true); return; }
            var btn = event.target && event.target.closest && event.target.closest('.acal-day');
            if (!btn || !root.contains(btn)) { return; }
            var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key];
            if (!step) { return; }
            var target = grid.querySelector('[data-iso="' + shiftDay(btn.dataset.iso, step) + '"]');
            if (target && !target.disabled) {
                event.preventDefault();
                btn.tabIndex = -1; target.tabIndex = 0; target.focus();
            }
        };
        document.addEventListener('mousedown', st.onOutside, true);
        document.addEventListener('keydown', st.onKey, true);

        loadMonth(st.month);
    }

    window.arabelaAvailabilityCalendar = {
        open: open, close: close, isOpen: isOpen,
        // pure helpers, exposed so they can be tested on their own
        monthCells: monthCells, shiftMonth: shiftMonth, describe: describe, longDay: longDay, shortDay: shortDay, monthLabel: monthLabel
    };
})();
