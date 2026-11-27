(function () {
    var body = document && document.body;
    var userKey = (body && body.dataset && body.dataset.userKey) ? body.dataset.userKey : 'guest';
    var STORAGE_KEY = 'arabela_reservation_cart_' + userKey;
    var GUEST_RESERVATION_KEY = 'arabela_reservation_cart_guest';
    var GUEST_CART_KEY = 'arabela_cart_guest';
    // The owner's deposit per gown (Edit Profile), written on the page by base.html -- never a fixed number here.
    var DEPOSIT_PER_ITEM = (function () {
        var n = Number(body && body.dataset && body.dataset.depositPerGown);
        return n > 0 ? n : 2000;
    })();

    // ---- the held selection (the checkout snapshot) ---------------------------------------------------------
    // The 20-minute hold lives in the SERVER session; the selection it holds lives in this TAB's
    // sessionStorage. Anything that gave the customer a tab without that storage -- a second tab, the
    // "Your selection is held" bar opened in a new tab, a phone restoring a tab it unloaded while they
    // were away -- found the hold alive and the selection missing: an empty checkout with a countdown.
    // So the selection is also kept in localStorage (shared by every tab, survives a restored tab) and
    // used ONLY while the hold is still alive. Whatever ends the hold (Cancel, expiry, submitting) removes
    // both copies, so a released selection can never come back.
    var SNAPSHOT_PREFIX = 'arabela_reservation_cart_';
    var BACKUP_PREFIX = 'arabela_reservation_backup_';
    var BACKUP_MAX_AGE_MS = 25 * 60 * 1000;   // the hold lasts 20 minutes

    function backupKeyFor(storageKey) {
        return String(storageKey).replace(SNAPSHOT_PREFIX, BACKUP_PREFIX);
    }

    // A hold is alive exactly when the server drew its countdown bar into this page.
    function holdIsAlive() {
        return !!document.getElementById('hold-banner');
    }

    // Writes the selection to both places. Returns nothing; storage that is unavailable just means no backup.
    window.arabelaSaveReservationSnapshot = function (storageKey, payload) {
        var raw = JSON.stringify(payload);
        try { sessionStorage.setItem(storageKey, raw); } catch (err) {}
        try { localStorage.setItem(backupKeyFor(storageKey), JSON.stringify({ savedAt: Date.now(), raw: raw })); } catch (err2) {}
    };

    // Removes both copies (the hold ended: cancelled, expired or submitted).
    window.arabelaClearReservationSnapshot = function (storageKey) {
        try { sessionStorage.removeItem(storageKey); } catch (err) {}
        try { localStorage.removeItem(backupKeyFor(storageKey)); } catch (err2) {}
    };

    // The backed-up selection as a raw JSON string, or '' when there is none, it is stale, or no hold is alive.
    function readBackup(storageKey) {
        if (!holdIsAlive()) { return ''; }
        try {
            var saved = JSON.parse(localStorage.getItem(backupKeyFor(storageKey)) || 'null');
            if (!saved || typeof saved.raw !== 'string') { return ''; }
            if (!(Date.now() - Number(saved.savedAt) < BACKUP_MAX_AGE_MS)) { return ''; }
            return saved.raw;
        } catch (err) {
            return '';
        }
    }

    window.arabelaGoHome = function () {
        var url = (document.body && document.body.dataset && document.body.dataset.homeUrl) || '/';
        window.location.replace(url);   // replace, so Back doesn't return to a checkout that no longer exists
    };

    // Tells the customer plainly why this checkout cannot continue, then takes them to the homepage.
    window.arabelaLeaveCheckout = function (title, message) {
        var go = function () { window.arabelaGoHome(); };
        if (window.arabelaAlert) {
            window.arabelaAlert({ title: title, message: message, confirmText: 'Back to home' }).then(go, go);
        } else {
            window.alert(message);
            go();
        }
    };

    function formatPesoInt(n) {
        return '₱' + Math.round(Number(n)).toLocaleString('en-PH');
    }

    function formatPesoSummary(n) {
        return '₱ ' + Math.round(Number(n)).toLocaleString('en-PH');
    }

    function escapeHtml(text) {
        if (text == null || text === '') return '';
        var d = document.createElement('div');
        d.textContent = text;
        return d.innerHTML;
    }

    function attrEscape(s) {
        return String(s || '')
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;');
    }

    // THE one definition of "this line can actually be booked". Every screen that
    // gates on rental dates -- cart drawer, selection page, checkout hand-off,
    // order summary -- must call this rather than re-testing `rental` by hand, or
    // the screens start disagreeing about which rows are ready.
    //
    // Deliberately matches the two rules that already exist downstream and must
    // not drift from them: reservation.html's splitRentalDates(), which builds the
    // submit payload, and the server's own `if not rental_date or not return_date`
    // in gowns/views.py. Size is NOT part of this -- it is optional end to end
    // (the server stores '' without complaint) and shows as TBD when unset.
    window.arabelaHasRentalDates = function (item) {
        var parts = String((item && item.rental) || '').split(' - ');
        return parts.length === 2 && !!parts[0].trim() && !!parts[1].trim();
    };

    // ---- the free days and what the chosen days take out of the deposit ------------------------------------------
    // The same arithmetic the server books with (gowns/availability.py extra_days, reservations/ledger.py): 2 days before the event and 2 days
    // after it are free; every booked day outside them costs the owner's fee per gown per day (written on the page by base.html), taken from
    // the security deposit when the gown is returned. A row from a bag saved before the event was kept has no event: it was always booked
    // inside the free days, so it costs nothing.
    var FREE_BEFORE = 2;
    var FREE_AFTER = 2;
    var FEE_PER_DAY = (function () {
        var raw = body && body.dataset ? body.dataset.lateFeePerDay : undefined;
        var n = Number(raw);
        return raw !== undefined && raw !== '' && n >= 0 ? n : 200;
    })();
    window.arabelaFeePerDay = function () { return FEE_PER_DAY; };

    function parseMdy(text) {
        var bits = String(text || '').trim().split('/');
        if (bits.length !== 3) return null;
        var d = new Date(Number(bits[2]), Number(bits[0]) - 1, Number(bits[1]));
        return isNaN(d.getTime()) || !Number(bits[0]) || !Number(bits[1]) || !Number(bits[2]) ? null : d;
    }
    function dayNumber(d) { return Math.round(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000); }
    function shortDay(d) { return d ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : ''; }

    // -> { event, pickup, ret (Dates or null), before, after, days, fee (pesos for ONE gown), freeFrom, freeUntil (text) }
    window.arabelaExtraDays = function (item) {
        var none = { event: null, pickup: null, ret: null, before: 0, after: 0, days: 0, fee: 0, freeFrom: '', freeUntil: '' };
        var parts = String((item && item.rental) || '').split(' - ');
        if (parts.length !== 2) return none;
        var pickup = parseMdy(parts[0]);
        var ret = parseMdy(parts[1]);
        var event = parseMdy(item && item.event);
        if (!pickup || !ret) return none;
        if (!event) return Object.assign({}, none, { pickup: pickup, ret: ret });
        var before = Math.max(0, (dayNumber(event) - FREE_BEFORE) - dayNumber(pickup));
        var after = Math.max(0, dayNumber(ret) - (dayNumber(event) + FREE_AFTER));
        var freeFrom = new Date(event.getFullYear(), event.getMonth(), event.getDate() - FREE_BEFORE);
        var freeUntil = new Date(event.getFullYear(), event.getMonth(), event.getDate() + FREE_AFTER);
        return { event: event, pickup: pickup, ret: ret, before: before, after: after, days: before + after,
                 fee: (before + after) * FEE_PER_DAY, freeFrom: shortDay(freeFrom), freeUntil: shortDay(freeUntil) };
    };
    // Short words for a row's dates: "Event Dec 15", and the deduction ("₱800 off your deposit (4 extra days)") or ''.
    window.arabelaEventText = function (item) {
        var info = window.arabelaExtraDays(item);
        return info.event ? 'Event ' + shortDay(info.event) : '';
    };
    // `over`: the bag's extra days cost more than its deposit, so they are not simply taken "off your deposit" (part is paid when booking).
    window.arabelaDeductionText = function (item, qty, over) {
        var info = window.arabelaExtraDays(item);
        var copies = Math.max(1, Number(qty) || 1);
        if (!info.days || !info.fee) return '';
        var days = info.days + ' extra day' + (info.days === 1 ? '' : 's') + (copies > 1 ? ' × ' + copies + ' gowns' : '');
        return formatPesoInt(info.fee * copies) + (over ? ' for ' + days : ' off your deposit (' + days + ')');
    };
    // What these lines' extra days cost MORE than their deposit (one deposit per gown, pooled): paid when booking. 0 when the deposit covers it.
    window.arabelaOverDeposit = function (items) {
        var fee = 0;
        var gowns = 0;
        (items || []).forEach(function (it) {
            var copies = Math.max(1, Number(it && it.qty) || 1);
            gowns += copies;
            if (window.arabelaHasRentalDates(it)) fee += window.arabelaExtraDays(it).fee * copies;
        });
        return Math.max(0, fee - gowns * DEPOSIT_PER_ITEM);
    };

    // Display label for a size that was never chosen. products.html seeds the
    // sentinel '-' and the quick-add paths in base.html default to it too, so
    // both that and an empty string mean "not chosen yet".
    window.arabelaSizeLabel = function (size) {
        var t = String(size == null ? '' : size).replace(/^\s*Size:\s*/i, '').trim();
        if (!t || t === '-') return 'TBD';
        return t.toUpperCase() === 'FREE' ? 'Free Size' : t;
    };

    // The size BUTTON code ('XS', 'S', 'M', 'L', 'XL', 'XXL', 'FREE') for any shape a stored size arrives in: the drawer's bare
    // 'M', the checkout snapshot's 'Size: M', or a full name such as 'Medium' or 'Free Size'. '' when it is none of them.
    window.arabelaSizeCode = function (size) {
        var t = String(size == null ? '' : size).replace(/^\s*Size:\s*/i, '').trim().toUpperCase();
        var names = { 'EXTRA SMALL': 'XS', 'SMALL': 'S', 'MEDIUM': 'M', 'LARGE': 'L', 'EXTRA LARGE': 'XL', 'EXTRA EXTRA LARGE': 'XXL', '2XL': 'XXL', 'FREE SIZE': 'FREE' };
        if (names[t]) return names[t];
        return ['XS', 'S', 'M', 'L', 'XL', 'XXL', 'FREE'].indexOf(t) >= 0 ? t : '';
    };

    // The size codes a gown comes in ('XS', 'S', 'M', 'L', 'XL', 'XXL', 'FREE'), from the search catalog the pages carry (gowns.context_processors
    // _build_search_catalog) -- the same list the + button on a collection card reads. The checkout page hides the search overlay, so it
    // carries just the sizes instead (#gown-sizes-data, same rows). null when neither is on this page or it has no such gown: nothing is
    // then asked of the customer here, and the server still refuses a gown that needs a size.
    var sizesCatalog = null;
    window.arabelaSizesOf = function (slug, collection) {
        try {
            if (sizesCatalog === null) {
                var el = document.getElementById('search-catalog-data') || document.getElementById('gown-sizes-data');
                if (!el) return null;                       // not drawn yet: look again next time instead of remembering "nothing"
                var rows = JSON.parse(el.textContent || '[]');
                sizesCatalog = Array.isArray(rows) ? rows : [];
            }
            var coll = collection ? String(collection).trim() : '';
            var row = sizesCatalog.filter(function (r) { return r && r.slug === slug && (!coll || r.collection_key === coll); })[0]
                || sizesCatalog.filter(function (r) { return r && r.slug === slug; })[0];
            return row && Array.isArray(row.sizes) ? row.sizes : null;
        } catch (err) {
            return null;
        }
    };

    // THE one definition of "this line still needs a size": the gown comes in more than one size and none is chosen yet. A gown with a
    // single size never needs asking (the quick-add fills it in), and a gown whose sizes cannot be read is never held back here -- the
    // server still refuses it at checkout, in words that send the customer to the gown page.
    window.arabelaNeedsSize = function (item) {
        if (!item || window.arabelaSizeCode(item.size)) return false;
        var sizes = window.arabelaSizesOf(String(item.id == null ? '' : item.id).trim(), item.collection);
        return !!sizes && sizes.length > 1;
    };

    // Rebuilds a gown's product URL from a stored line. Mirrors base.html's and
    // selection.html's getProductDetailUrl, but the query parameter varies by
    // caller: `cart_item_index` edits a drawer row, `edit_reservation_item` edits
    // a row of the already-handed-off checkout snapshot. Returns '' when the line
    // has no slug, so callers can fall back rather than link somewhere broken.
    function buildProductUrl(item, index, paramName) {
        var slug = item && item.id ? String(item.id).trim() : '';
        if (!slug) return '';
        var col = (item && item.collection) ? String(item.collection).trim() : 'wedding';
        return '/collections/' + encodeURIComponent(col)
            + '/products/' + encodeURIComponent(slug)
            + '/?' + (paramName || 'cart_item_index') + '=' + index;
    }

    // The one dialog shown whenever gowns that cannot be booked yet block a step forward -- a size still to choose, a rental date still to
    // pick, or both. Uses arabelaConfirm rather than arabelaAlert on purpose: an alert's single "Got it" button is exactly what made the
    // old checkout failure a dead end. This names the gowns and its primary button navigates straight to the first one's page, where the
    // size and the dates are both chosen. `entries` is [{name, url, needsSize, needsDates}, ...].
    window.arabelaPromptForGaps = function (entries) {
        if (!entries || !entries.length) return;
        var names = entries.map(function (e) { return e.name || 'a gown'; });
        var listed = names.length === 1
            ? names[0]
            : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
        var many = names.length > 1;
        var anySize = entries.some(function (e) { return e.needsSize; });
        var anyDates = entries.some(function (e) { return e.needsDates; });
        var first = many ? ' We will start with ' + names[0] + '.' : '';
        var title, message;
        if (!anySize) {
            title = many ? 'These gowns need rental dates' : 'This gown needs a rental date';
            message = listed + (many ? ' have no rental dates yet.' : ' has no rental date yet.')
                + ' Pick the date of your event and we will set the pick-up and return days for you.' + first;
        } else if (!anyDates) {
            title = many ? 'These gowns need sizes' : 'This gown needs a size';
            message = listed + (many ? ' have no size yet.' : ' has no size yet.')
                + ' Choose the size you want and we will check it on your dates.' + first;
        } else {
            title = many ? 'These gowns need a size or a rental date' : 'This gown needs a size and a rental date';
            message = many
                ? listed + ' still need a size or a rental date. Choose them on each gown\u2019s page, and we will set the pick-up and return days for you.' + first
                : listed + ' has no size or rental date yet. Choose the size you want and pick the date of your event, and we will set the pick-up and return days for you.';
        }
        window.arabelaConfirm({
            title: title,
            message: message,
            confirmText: anySize ? 'Select size' : 'Set dates',
            cancelText: 'Not now'
        }).then(function (ok) {
            if (!ok) return;
            var target = entries[0].url;
            if (target) window.location.href = target;
        });
    };

    // The earlier name, for a page that was opened before this script was updated: every entry there is a gown missing its dates.
    window.arabelaPromptForDates = function (entries) {
        window.arabelaPromptForGaps((entries || []).map(function (e) {
            return { name: e.name, url: e.url, needsDates: true, needsSize: !!e.needsSize };
        }));
    };

    function maybeMigrateGuestReservationPayload() {
        if (userKey === 'guest') return;
        var guestRaw = null;
        try {
            guestRaw = sessionStorage.getItem(GUEST_RESERVATION_KEY);
        } catch (e) {}
        if (!guestRaw) return;
        var guestData;
        try {
            guestData = JSON.parse(guestRaw);
        } catch (e2) {
            return;
        }
        if (!guestData || !Array.isArray(guestData.items) || guestData.items.length === 0) {
            try {
                sessionStorage.removeItem(GUEST_RESERVATION_KEY);
            } catch (eClear) {}
            return;
        }
        var guestTs = Number(guestData.savedAt) || 0;
        var userRaw = null;
        try {
            userRaw = sessionStorage.getItem(STORAGE_KEY);
        } catch (e3) {}
        var userTs = 0;
        if (userRaw) {
            try {
                var userData = JSON.parse(userRaw);
                if (userData && Array.isArray(userData.items) && userData.items.length > 0) {
                    userTs = Number(userData.savedAt) || 0;
                }
            } catch (e4) {}
        }
        try {
            if (userTs > guestTs) {
                sessionStorage.removeItem(GUEST_RESERVATION_KEY);
                return;
            }
            sessionStorage.setItem(STORAGE_KEY, guestRaw);
            sessionStorage.removeItem(GUEST_RESERVATION_KEY);
        } catch (e5) {}
    }

    function normCartSize(s) {
        var t = String(s == null ? '' : s).trim();
        if (!t || t === '-') return '';
        return t;
    }

    function normCartRental(r) {
        var t = String(r == null ? '' : r).trim();
        if (!t || t === '-') return '';
        return t;
    }

    function sameCartLine(a, b) {
        if (!a || !b) return false;
        var aCol = a.collection ? String(a.collection).trim() : 'wedding';
        var bCol = b.collection ? String(b.collection).trim() : 'wedding';
        return (
            a.id === b.id &&
            (a.name || 'Untitled') === (b.name || 'Untitled') &&
            aCol === bCol &&
            normCartSize(a.size) === normCartSize(b.size) &&
            normCartRental(a.rental) === normCartRental(b.rental) &&
            normCartRental(a.event) === normCartRental(b.event)
        );
    }

    function maybeMergeGuestDrawerCart() {
        if (userKey === 'guest') return;
        var guestRaw = null;
        try {
            guestRaw = localStorage.getItem(GUEST_CART_KEY);
        } catch (e) {}
        if (!guestRaw) return;
        var guestCart;
        try {
            guestCart = JSON.parse(guestRaw);
        } catch (e2) {
            return;
        }
        if (!Array.isArray(guestCart) || guestCart.length === 0) return;
        var userCartKey = 'arabela_cart_' + userKey;
        var userCart = [];
        try {
            userCart = JSON.parse(localStorage.getItem(userCartKey) || '[]');
        } catch (e3) {
            userCart = [];
        }
        if (!Array.isArray(userCart)) userCart = [];
        for (var i = 0; i < guestCart.length; i++) {
            var gitem = guestCart[i];
            if (!gitem) continue;
            var existing = null;
            for (var u = 0; u < userCart.length; u++) {
                var ci = userCart[u];
                if (ci && sameCartLine(ci, gitem)) {
                    existing = ci;
                    break;
                }
            }
            if (existing) {
                // Capped the same way base.html's own +/- stepper is: a guest cart
                // merging into an already-populated account cart must never combine
                // past what the product actually has in stock. Computed inline
                // (not calling base.html's cartLineMaxQty) since this file loads
                // before that script defines it.
                var mergedMax = Number(existing.maxQty) > 0 ? Number(existing.maxQty) : Infinity;
                var mergedWanted = (Number(existing.qty) || 1) + (Number(gitem.qty) || 1);
                existing.qty = Math.min(mergedWanted, mergedMax);
            } else {
                userCart.push(gitem);
            }
        }
        try {
            localStorage.setItem(userCartKey, JSON.stringify(userCart));
            localStorage.removeItem(GUEST_CART_KEY);
            if (typeof syncCartToServer === 'function') syncCartToServer(userCart);
        } catch (e4) {}
    }

    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function postJson(url) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: '{}'
        }).then(function (res) {
            return res.json().then(function (data) {
                return Object.assign({ httpStatus: res.status }, data);
            });
        });
    }

    // Starts the 20-minute checkout hold and resolves with the server's JSON
    // response ({success, already_active, seconds_remaining} or {success:false,
    // locked, error} when the account is temporarily locked out -- see
    // gowns.views.reservation_hold_start). Never rejects: a network hiccup must
    // not block checkout, so it resolves success:true as a fail-open default.
    window.startReservationHold = function () {
        var body = document && document.body;
        var url = body && body.dataset ? body.dataset.holdStartUrl : '';
        if (!url) return Promise.resolve({ success: true });
        return postJson(url).catch(function () {
            return { success: true };
        });
    };

    // Proceed refused because the account is temporarily locked: the server's message says why and until when.
    function holdRefusedTitle(result) {
        if (result && result.locked) return 'Your account is temporarily locked';
        if (result && result.account_blocked) return 'Your account is paused';
        if (result && result.limit_reached) return 'Too many bookings waiting';
        return 'Please wait before trying again';
    }

    function alertMessage(title, message) {
        if (window.arabelaAlert) {
            window.arabelaAlert({ title: title, message: message });
        } else {
            window.alert(message);
        }
    }

    function describeHeldItems(items) {
        var names = (items || []).map(function (it) { return (it && it.name) || 'a gown'; });
        if (!names.length) return 'your previous selection';
        return names.length === 1
            ? names[0]
            : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
    }

    // Same items, ignoring array order -- used to tell "you're re-confirming the
    // exact thing you already have held" apart from "you picked something else",
    // so re-clicking Proceed on an unchanged cart never nags with the swap dialog.
    function sameSelection(itemsA, itemsB) {
        if (!Array.isArray(itemsA) || !Array.isArray(itemsB) || itemsA.length !== itemsB.length) return false;
        function keyOf(it) { return [it.id, it.name, it.size, it.rental, it.event || '', it.qty].join('|'); }
        var a = itemsA.map(keyOf).sort();
        var b = itemsB.map(keyOf).sort();
        for (var i = 0; i < a.length; i++) {
            if (a[i] !== b[i]) return false;
        }
        return true;
    }

    // Orchestrates starting a hold for a freshly-built checkout `payload`
    // (see saveCartAndProceedToReservation / selection.html's proceedReservationFromSelection),
    // and only writes it into sessionStorage[storageKey] once it's actually safe to
    // proceed. Resolves true when the caller should clear the cart and navigate,
    // false when the attempt was refused or the customer backed out (nothing is
    // changed in either case -- any existing hold is left exactly as it was).
    //
    // Handles two things a plain startReservationHold() call can't on its own:
    //   - a temporary cancellation lockout (accounts.services.sync_cancellation_flag)
    //     refuses outright, shown as a plain alert;
    //   - an already-running hold for a DIFFERENT selection would otherwise be
    //     silently replaced under the SAME countdown (start_reservation_hold never
    //     restarts an existing clock) -- confirmed with the customer instead, and
    //     only released + restarted (a fresh 20:00, counted as one abandoned hold,
    //     same as pressing Cancel on the banner) if they agree.
    function beginHold(storageKey, payload, askedForTerms) {
        var oldPayload = null;
        try {
            // This tab's copy, else the backup (a hold can have been started from another tab).
            var parsed = JSON.parse(sessionStorage.getItem(storageKey) || readBackup(storageKey) || 'null');
            if (parsed && Array.isArray(parsed.items)) oldPayload = parsed;
        } catch (err) {}

        function commit() {
            window.arabelaSaveReservationSnapshot(storageKey, payload);
            return true;
        }

        function releaseThenRestart() {
            var body = document && document.body;
            var releaseUrl = body && body.dataset ? body.dataset.holdReleaseUrl : '';
            var releasePromise = releaseUrl ? postJson(releaseUrl).catch(function () {}) : Promise.resolve();
            return releasePromise.then(window.startReservationHold).then(function (fresh) {
                if (!fresh.success) {
                    alertMessage(holdRefusedTitle(fresh), fresh.error || 'Please try again later.');
                    return false;
                }
                return commit();
            });
        }

        return window.startReservationHold().then(function (result) {
            if (!result.success && result.terms_required && !askedForTerms) {
                // The page thought the customer had agreed but the server says not (an old page, another tab): ask now, then try once more.
                return askForTerms().then(function (agreed) {
                    return agreed ? beginHold(storageKey, payload, true) : false;
                });
            }
            if (!result.success) {
                alertMessage(holdRefusedTitle(result), result.error || 'Too many cancelled or abandoned reservations. Please try again later.');
                return false;
            }

            if (!result.already_active || !oldPayload || sameSelection(oldPayload.items, payload.items)) {
                return commit();
            }

            var secondsLeft = Math.max(0, result.seconds_remaining || 0);
            var clock = String(Math.floor(secondsLeft / 60)).padStart(2, '0') + ':' + String(secondsLeft % 60).padStart(2, '0');

            return window.arabelaConfirm({
                title: 'You already have a selection held',
                message: 'You still have ' + describeHeldItems(oldPayload.items) + ' held with ' + clock + ' left. '
                    + 'Picking a new gown will release that hold -- counted the same as cancelling it -- '
                    + 'and start a fresh 20-minute window for your new selection instead. Continue?',
                confirmText: 'Start fresh',
                cancelText: 'Keep my current hold'
            }).then(function (ok) {
                return ok ? releaseThenRestart() : false;
            });
        });
    }

    // The Terms pop-up (templates/partials/terms_gate.html) exists only for a customer who has not agreed yet.
    function askForTerms() {
        return typeof window.arabelaRequireTerms === 'function' ? window.arabelaRequireTerms() : Promise.resolve(false);
    }

    // Before the countdown starts the customer must have read and agreed to the Terms and Conditions -- so the time spent reading never
    // runs down their 20-minute hold. Someone who has already agreed (or a page without the pop-up) goes straight on.
    window.beginReservationHold = function (storageKey, payload) {
        var body = document && document.body;
        var needsTerms = body && body.dataset && body.dataset.termsNeeded === 'true';
        if (!needsTerms) return beginHold(storageKey, payload, false);
        return askForTerms().then(function (agreed) {
            return agreed ? beginHold(storageKey, payload, true) : false;
        });
    };

    window.saveCartAndProceedToReservation = function (e) {
        if (e && e.preventDefault) e.preventDefault();
        var items = [];
        var sub = 0;
        var itemCount = 0;
        // Parallel to `items`: each entry's index in the stored cart. Not the same
        // as the position in `items`, because hidden rows are skipped below -- and
        // products.html writes back by cart index, so getting this wrong would edit
        // the wrong gown.
        var cartIndexes = [];
        var rows = document.querySelectorAll('#cart-drawer .cart-line');
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            if (row.classList.contains('hidden')) continue;
            var cartIndex = parseInt(row.getAttribute('data-index'), 10);
            cartIndexes.push(Number.isInteger(cartIndex) ? cartIndex : i);
            var unit = parseFloat(row.getAttribute('data-unit-price') || '0', 10);
            var qtyEl = row.querySelector('.cart-qty');
            var qty = Math.max(1, parseInt(qtyEl && qtyEl.textContent, 10) || 1);
            var img = row.querySelector('img');
            var h3 = row.querySelector('h3');
            var sizeEl = row.querySelector('.text-on-secondary-container');
            var rental = row.getAttribute('data-rental') || '';
            var lineTotal = unit * qty;
            sub += lineTotal;
            itemCount += qty;
            items.push({
                name: h3 ? h3.textContent.trim() : '',
                size: sizeEl ? sizeEl.textContent.trim() : '',
                image: img ? img.getAttribute('src') : '',
                imageAlt: img ? (img.getAttribute('alt') || '') : '',
                unitPrice: unit,
                qty: qty,
                lineTotal: lineTotal,
                rental: rental,
                // The event day: the free days (and what comes out of the deposit) are counted from it.
                event: row.getAttribute('data-event') || '',
                // Carried through so the Order Summary can link each row back to
                // its gown page. base.html's renderCartDrawer writes these; without
                // them there is no valid product URL to rebuild later.
                id: row.getAttribute('data-id') || '',
                collection: row.getAttribute('data-collection') || ''
            });
        }
        if (!items.length) {
            // This file is served statically so it can't include the dialog
            // partial; fall back to the native alert if it somehow isn't loaded.
            if (window.arabelaAlert) {
                window.arabelaAlert({
                    title: 'Your selection is empty',
                    message: 'Add at least one item to your selection before continuing.'
                });
            } else {
                window.alert('Add at least one item to your selection before continuing.');
            }
            return false;
        }
        // Refuse to hand off a bag that can't be booked. Checkout would otherwise
        // take the whole form and the payment upload before failing, with no route
        // back to the gown. Deliberately BEFORE the hold starts and before the
        // drawer is cleared, so nothing is committed and the cart survives intact
        // -- which is also why ?cart_item_index= still resolves from here.
        // What can't be booked yet is a gown with no dates, or one that comes in several sizes and has none chosen.
        var gaps = [];
        for (var u = 0; u < items.length; u++) {
            var lacksSize = window.arabelaNeedsSize(items[u]);
            var lacksDates = !window.arabelaHasRentalDates(items[u]);
            if (lacksSize || lacksDates) {
                gaps.push({
                    name: items[u].name,
                    url: buildProductUrl(items[u], cartIndexes[u], 'cart_item_index'),
                    needsSize: lacksSize,
                    needsDates: lacksDates
                });
            }
        }
        if (gaps.length) {
            window.arabelaPromptForGaps(gaps);
            return false;
        }
        // Never more of a gown than exists (static/js/cart-lines.js): checkout would refuse the extra one at the very last step.
        var storedBag = [];
        try { storedBag = JSON.parse(localStorage.getItem('arabela_cart_' + userKey) || '[]'); } catch (errBag) { storedBag = []; }
        if (window.arabelaCartLines) {
            var tooMany = window.arabelaCartLines.overbooked(storedBag);
            if (tooMany.length) {
                alertMessage('Too many of one gown', window.arabelaCartLines.overbookedMessage(tooMany));
                return false;
            }
        }
        for (var m = 0; m < items.length; m++) {
            var bagLine = storedBag[cartIndexes[m]];
            if (bagLine && Number(bagLine.maxQty) > 0) items[m].maxQty = Number(bagLine.maxQty);
        }
        var deposit = itemCount * DEPOSIT_PER_ITEM;
        var payload = {
            items: items,
            subtotal: sub,
            itemCount: itemCount,
            deposit: deposit,
            total: sub + deposit,
            savedAt: Date.now()
        };
        var a = e && e.currentTarget;
        var href = (a && a.href) ? a.href : '';
        var body = document && document.body;
        var isAuthenticated = body && body.dataset ? body.dataset.authenticated : '';
        var loginUrl = body && body.dataset ? body.dataset.loginUrl : '';
        if (isAuthenticated === 'false' && loginUrl) {
            // Not signed in yet -- stash the bag so it survives the round trip
            // through login, same as before. No hold to start (or lockout to
            // check) until there is an actual account to attach either to.
            try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload)); } catch (err) {}
            var nextParam = encodeURIComponent(href || '/');
            window.location.href = loginUrl + '?next=' + nextParam;
            return false;
        }
        // Starts the hold (or confirms replacing an existing one) and only writes
        // `payload` into sessionStorage once that's actually settled.
        window.beginReservationHold(STORAGE_KEY, payload).then(function (proceed) {
            if (!proceed) return;
            // These items are now committed to the pending reservation, not sitting
            // in the shop cart anymore -- clear the drawer so its badge/contents
            // don't keep showing them (e.g. after a refresh) while the hold is live.
            // Mirrors reservation_hold_banner.html's clearHeldSelection(), which does
            // the same thing when a hold is cancelled instead of completed.
            try {
                if (typeof setDrawerCart === 'function') {
                    setDrawerCart([]);
                    if (typeof renderCartDrawer === 'function') renderCartDrawer();
                    if (typeof updateCartTotals === 'function') updateCartTotals();
                } else {
                    localStorage.setItem('arabela_cart_' + userKey, '[]');
                }
            } catch (err) {}
            if (href) window.location.href = href;
        });
        return false;
    };

    window.initReservationOrderSummary = function () {
        var root = document.getElementById('reservation-items-root');
        if (!root) return;

        maybeMigrateGuestReservationPayload();
        maybeMergeGuestDrawerCart();

        var subEl = document.getElementById('reservation-rental-subtotal');
        var depEl = document.getElementById('reservation-security-deposit');
        var totalEl = document.getElementById('reservation-total-investment');
        var countEl = document.getElementById('reservation-items-count');
        var gcashDep = document.getElementById('gcash-deposit-amount');

        var raw = null;
        try {
            raw = sessionStorage.getItem(STORAGE_KEY);
        } catch (e) {}
        if (!raw) {
            // This tab has no copy but the hold may still be alive (another tab / a restored tab).
            raw = readBackup(STORAGE_KEY);
            if (raw) {
                try { sessionStorage.setItem(STORAGE_KEY, raw); } catch (eRestore) {}
            }
        }

        function setTotals(subtotal, deposit, total, itemCount) {
            // Booked days that cost more than the deposit are paid now, with it (showDeduction worked out how much).
            var over = Number(window.arabelaCheckoutOver) || 0;
            if (subEl) subEl.textContent = formatPesoSummary(subtotal);
            if (depEl) depEl.textContent = formatPesoSummary(deposit);
            if (totalEl) totalEl.textContent = formatPesoSummary(total + over);
            if (countEl) {
                var n = itemCount || 0;
                if (!n) countEl.textContent = '';
                else if (n === 1) countEl.textContent = '1 item reserved';
                else countEl.textContent = n + ' items reserved';
            }
            if (gcashDep) gcashDep.textContent = 'Amount: ' + formatPesoInt(deposit + over);
            // The checkout page (templates/reservation.html) shows what each way of paying costs, so it is told the same numbers.
            if (typeof window.onReservationTotals === 'function') window.onReservationTotals(subtotal, deposit, total, itemCount, over);
        }

        // The days outside the free days come out of the deposit when the gowns are returned: the red line under the deposit, and what comes
        // back. What they cost MORE than the deposit is paid now, with it. window.arabelaCheckoutExtraFee and window.arabelaCheckoutOver are
        // what the page sends as the amounts it showed.
        function showDeduction(list, deposit) {
            var fee = 0;
            var days = 0;
            (list || []).forEach(function (it) {
                if (!window.arabelaHasRentalDates(it)) return;
                var info = window.arabelaExtraDays(it);
                var copies = Math.max(1, Number(it.qty) || 1);
                fee += info.fee * copies;
                days += info.days * copies;
            });
            window.arabelaCheckoutExtraFee = fee;
            window.arabelaCheckoutOver = Math.max(0, fee - deposit);
            var refundNote = document.getElementById('reservation-deposit-note');
            if (refundNote) refundNote.textContent = fee > 0 ? '(Refundable upon return, less the deduction below)' : '(Fully refundable upon return)';
            var box = document.getElementById('reservation-deduction');
            if (!box) return;
            if (!(fee > 0)) { box.hidden = true; return; }
            var back = Math.max(0, deposit - fee);
            var owed = Math.max(0, fee - deposit);
            var put = function (id, text) { var el = document.getElementById(id); if (el) el.textContent = text; };
            var daysText = days + ' extra day' + (days === 1 ? '' : 's') + ' outside the free days, ' + formatPesoInt(FEE_PER_DAY) + ' each.';
            put('reservation-deduction-label', owed > 0 ? formatPesoInt(fee) + ' for your extra days' : '− ' + formatPesoInt(fee) + ' off your security deposit');
            put('reservation-deduction-note', owed > 0
                ? daysText + ' That is more than your ' + formatPesoInt(deposit) + ' deposit, which covers part of it. Charged even if you pick up later or return earlier.'
                : daysText + ' Taken from the deposit when the gown is returned, even if you pick up later or return earlier.');
            put('reservation-deposit-back', formatPesoSummary(back));
            var owedEl = document.getElementById('reservation-extra-owed');
            if (owedEl) {
                owedEl.hidden = !(owed > 0);
                owedEl.textContent = owed > 0 ? 'Paid now over the deposit: ' + formatPesoInt(owed) + '. You pay ' + formatPesoInt(deposit + owed) + ' to reserve.' : '';
            }
            var wasHidden = box.hidden;
            box.hidden = false;
            if (wasHidden) { box.classList.remove('is-in'); void box.offsetWidth; box.classList.add('is-in'); }
        }

        var data = null;
        if (raw) {
            try {
                data = JSON.parse(raw);
            } catch (e2) {
                return;
            }
        }

        if (!data || !Array.isArray(data.items) || !data.items.length) {
            // Nothing to check out. Never leave the customer on a form with a zero total and a Confirm
            // button that cannot work -- say what happened and take them home.
            root.innerHTML =
                '<p class="text-sm text-secondary">No items in your selection. Return to the shop and add pieces first.</p>';
            showDeduction([], 0);
            setTotals(0, 0, 0, 0);
            var confirmBtn = document.getElementById('confirm-rental-btn');
            if (confirmBtn) confirmBtn.disabled = true;
            if (holdIsAlive()) {
                window.arabelaLeaveCheckout('We could not find your selection',
                    'Your gowns are no longer saved on this page, so there is nothing to confirm. Please pick your gowns again -- your timer keeps running, and picking them again continues it.');
            } else {
                window.arabelaLeaveCheckout('Your selection is empty',
                    'There is nothing to reserve right now. Browse the gowns and add what you like first.');
            }
            return;
        }

        var items = data.items || [];
        var html = '';
        var gaps = [];
        var listOver = window.arabelaOverDeposit(items) > 0;
        for (var j = 0; j < items.length; j++) {
            var it = items[j];
            var qtyNote =
                it.qty > 1
                    ? ' <span class="text-secondary font-normal normal-case">(\u00d7' + it.qty + ')</span>'
                    : '';
            var dated = window.arabelaHasRentalDates(it);
            var lacksSize = window.arabelaNeedsSize(it);
            // Round trip back to this exact row of the snapshot. The drawer cart is
            // deliberately empty once checkout has begun, so ?cart_item_index= can't
            // resolve from here -- edit_reservation_item addresses the snapshot.
            var editUrl = buildProductUrl(it, j, 'edit_reservation_item');
            if (!dated || lacksSize) gaps.push({ name: it.name, url: editUrl, needsDates: !dated, needsSize: lacksSize });

            // The date line is the whole point of this row: either it states the
            // rental window, or it is the control that goes and sets one.
            var info = window.arabelaExtraDays(it);
            var dateLine = dated
                ? '<p class="mt-1 flex items-center gap-1.5 text-[0.75rem] text-secondary">'
                    + '<span class="material-symbols-outlined" style="font-size: 0.9rem;">calendar_today</span>'
                    + escapeHtml(info.pickup && info.ret ? shortDay(info.pickup) + ' – ' + info.ret.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : it.rental)
                    + (editUrl ? '<span class="res-item-edit text-[0.6875rem] font-medium underline underline-offset-4">Change</span>' : '')
                    + '</p>'
                : '<span class="mt-1 inline-flex items-center gap-1.5 text-[0.75rem] font-semibold text-[#b45309]">'
                    + '<span class="material-symbols-outlined" style="font-size: 0.9rem;">event</span>'
                    + (editUrl ? 'Set rental date &rarr;' : 'Rental date needed')
                    + '</span>';
            // The event, and the day the gown is collected (the first date of the window; only 1 PM - 5 PM).
            var pickupLine = dated
                ? '<p class="mt-0.5 text-[0.6875rem] text-secondary/80">'
                    + (info.event ? 'Event ' + escapeHtml(shortDay(info.event)) + ' &middot; ' : '')
                    + 'Pick-up ' + escapeHtml(info.pickup ? shortDay(info.pickup) : String(it.rental).split(' - ')[0]) + ', 1:00 PM to 5:00 PM only</p>'
                : '';
            // Days outside the free days: what this gown takes out of the deposit when it is returned (never added to what is paid now).
            var deduction = dated ? window.arabelaDeductionText(it, it.qty, listOver) : '';
            if (deduction) {
                pickupLine += '<p class="mt-1 inline-flex items-center gap-1 rounded-[4px] px-1.5 py-0.5 text-[0.6875rem] font-semibold res-item-deduct">'
                    + '<span class="material-symbols-outlined" style="font-size: 0.85rem;">' + (listOver ? 'add_circle' : 'remove_circle') + '</span>'
                    + (listOver ? '' : '&minus; ') + escapeHtml(deduction) + '</p>';
            }

            // A gown that comes in several sizes and has none chosen: the size line is the control that goes and chooses one.
            var sizeLine = lacksSize
                ? '<p class="mt-0.5"><span class="inline-flex items-center gap-1.5 text-[0.75rem] font-semibold text-[#b45309]">'
                    + '<span class="material-symbols-outlined" style="font-size: 0.9rem;">straighten</span>'
                    + (editUrl ? 'Select size &rarr;' : 'Size needed')
                    + '</span></p>'
                : '<p class="mt-0.5 text-[0.75rem] text-secondary">Size: ' + escapeHtml(window.arabelaSizeLabel(it.size)) + '</p>';

            var inner =
                '<div class="w-16 h-20 shrink-0 overflow-hidden rounded-[6px] border border-black/10 bg-surface-container-low">' +
                '<img class="w-full h-full object-cover" alt="' +
                attrEscape(it.imageAlt || it.name) +
                '" src="' +
                attrEscape(it.image) +
                '">' +
                '</div>' +
                '<div class="min-w-0 flex-1">' +
                '<p class="text-sm font-medium text-on-surface leading-snug">' +
                escapeHtml(it.name) +
                '</p>' +
                sizeLine +
                dateLine +
                pickupLine +
                '</div>' +
                '<p class="shrink-0 text-sm font-medium text-on-surface tabular-nums">' +
                formatPesoSummary(it.lineTotal) +
                qtyNote +
                '</p>';

            // Only linkable when the line kept its slug -- older snapshots saved
            // before this shipped have no id, and a broken link is worse than none.
            if (editUrl) {
                html += '<a href="' + attrEscape(editUrl) + '"'
                    + ' class="res-item-row' + (dated && !lacksSize ? '' : ' res-item-row--needs-date') + ' flex gap-4 items-start no-underline"'
                    + ' aria-label="' + attrEscape((lacksSize ? 'Choose a size for ' : dated ? 'Change rental dates for ' : 'Set a rental date for ') + (it.name || 'this gown')) + '">'
                    + inner + '</a>';
            } else {
                html += '<div class="flex gap-4 items-start">' + inner + '</div>';
            }
        }
        root.innerHTML = html;

        // The saved selection remembers the deposit it was made with; the checkout always shows what the owner asks NOW.
        var liveDeposit = (Number(data.itemCount) || 0) * DEPOSIT_PER_ITEM;
        showDeduction(items, liveDeposit);                     // first: the totals count what is over the deposit
        setTotals(data.subtotal, liveDeposit, (Number(data.subtotal) || 0) + liveDeposit, data.itemCount);

        // Tell the page which gowns still block checkout (no dates, or a size still to choose), so Confirm Rental can be
        // held back up front instead of failing after the form and the upload.
        if (typeof window.onReservationDateGaps === 'function') {
            window.onReservationDateGaps(gaps);
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', window.initReservationOrderSummary);
    } else {
        window.initReservationOrderSummary();
    }

    // A checkout page restored with the Back button is the page as it was, not as it is now: look again,
    // so a selection that was released in the meantime is not still sitting there ready to submit.
    window.addEventListener('pageshow', function (event) {
        if (event.persisted && document.getElementById('reservation-items-root')) {
            window.initReservationOrderSummary();
        }
    });

    // The held selection's backup disappears when the hold ends in ANOTHER tab (cancelled, expired or
    // submitted there). Any checkout still open here is now showing something that no longer exists.
    window.addEventListener('storage', function (event) {
        if (!document.getElementById('reservation-items-root')) return;
        if (event.key !== backupKeyFor(STORAGE_KEY) || event.newValue !== null) return;
        try { sessionStorage.removeItem(STORAGE_KEY); } catch (err) {}
        var confirmBtn = document.getElementById('confirm-rental-btn');
        if (confirmBtn) confirmBtn.disabled = true;
        window.arabelaLeaveCheckout('This selection was already released',
            'It was cancelled, expired or submitted in another tab, so there is nothing left to book here.');
    });
})();
