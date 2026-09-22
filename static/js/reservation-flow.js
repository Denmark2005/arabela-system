(function () {
    var body = document && document.body;
    var userKey = (body && body.dataset && body.dataset.userKey) ? body.dataset.userKey : 'guest';
    var STORAGE_KEY = 'arabela_reservation_cart_' + userKey;
    var GUEST_RESERVATION_KEY = 'arabela_reservation_cart_guest';
    var GUEST_CART_KEY = 'arabela_cart_guest';
    var DEPOSIT_PER_ITEM = 2000;

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

    // Display label for a size that was never chosen. products.html seeds the
    // sentinel '-' and the quick-add paths in base.html default to it too, so
    // both that and an empty string mean "not chosen yet".
    window.arabelaSizeLabel = function (size) {
        var t = String(size == null ? '' : size).replace(/^\s*Size:\s*/i, '').trim();
        return (!t || t === '-') ? 'TBD' : t;
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

    // The one dialog shown whenever undated gowns block a step forward. Uses
    // arabelaConfirm rather than arabelaAlert on purpose: an alert's single "Got
    // it" button is exactly what made the old checkout failure a dead end. This
    // names the gowns and its primary button navigates straight to the first one.
    // `entries` is [{name, url}, ...].
    window.arabelaPromptForDates = function (entries) {
        if (!entries || !entries.length) return;
        var names = entries.map(function (e) { return e.name || 'a gown'; });
        var listed = names.length === 1
            ? names[0]
            : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
        var many = names.length > 1;
        window.arabelaConfirm({
            title: many ? 'These gowns need rental dates' : 'This gown needs a rental date',
            message: listed + (many ? ' have no rental dates yet.' : ' has no rental date yet.')
                + ' Pick the date of your event and we will set the pick-up and return days for you.'
                + (many ? ' We will start with ' + names[0] + '.' : ''),
            confirmText: 'Set dates',
            cancelText: 'Not now'
        }).then(function (ok) {
            if (!ok) return;
            var target = entries[0].url;
            if (target) window.location.href = target;
        });
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
            normCartRental(a.rental) === normCartRental(b.rental)
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
        function keyOf(it) { return [it.id, it.name, it.size, it.rental, it.qty].join('|'); }
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
    window.beginReservationHold = function (storageKey, payload) {
        var oldPayload = null;
        try {
            var parsed = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
            if (parsed && Array.isArray(parsed.items)) oldPayload = parsed;
        } catch (err) {}

        function commit() {
            try { sessionStorage.setItem(storageKey, JSON.stringify(payload)); } catch (err) {}
            return true;
        }

        function releaseThenRestart() {
            var body = document && document.body;
            var releaseUrl = body && body.dataset ? body.dataset.holdReleaseUrl : '';
            var releasePromise = releaseUrl ? postJson(releaseUrl).catch(function () {}) : Promise.resolve();
            return releasePromise.then(window.startReservationHold).then(function (fresh) {
                if (!fresh.success) {
                    alertMessage('Please wait before trying again', fresh.error || 'Please try again later.');
                    return false;
                }
                return commit();
            });
        }

        return window.startReservationHold().then(function (result) {
            if (!result.success) {
                alertMessage('Please wait before trying again', result.error || 'Too many cancelled or abandoned reservations. Please try again later.');
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
        var undated = [];
        for (var u = 0; u < items.length; u++) {
            if (!window.arabelaHasRentalDates(items[u])) {
                undated.push({
                    name: items[u].name,
                    url: buildProductUrl(items[u], cartIndexes[u], 'cart_item_index')
                });
            }
        }
        if (undated.length) {
            window.arabelaPromptForDates(undated);
            return false;
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

        function setTotals(subtotal, deposit, total, itemCount) {
            if (subEl) subEl.textContent = formatPesoSummary(subtotal);
            if (depEl) depEl.textContent = formatPesoSummary(deposit);
            if (totalEl) totalEl.textContent = formatPesoSummary(total);
            if (countEl) {
                var n = itemCount || 0;
                if (!n) countEl.textContent = '';
                else if (n === 1) countEl.textContent = '1 item reserved';
                else countEl.textContent = n + ' items reserved';
            }
            if (gcashDep) gcashDep.textContent = 'Amount: ' + formatPesoInt(deposit);
        }

        if (!raw) {
            root.innerHTML =
                '<p class="text-sm text-secondary">No items in your selection. Return to the shop and add pieces first.</p>';
            setTotals(0, 0, 0, 0);
            return;
        }

        var data;
        try {
            data = JSON.parse(raw);
        } catch (e2) {
            return;
        }

        var items = data.items || [];
        var html = '';
        var undated = [];
        for (var j = 0; j < items.length; j++) {
            var it = items[j];
            var qtyNote =
                it.qty > 1
                    ? ' <span class="text-secondary font-normal normal-case">(\u00d7' + it.qty + ')</span>'
                    : '';
            var dated = window.arabelaHasRentalDates(it);
            // Round trip back to this exact row of the snapshot. The drawer cart is
            // deliberately empty once checkout has begun, so ?cart_item_index= can't
            // resolve from here -- edit_reservation_item addresses the snapshot.
            var editUrl = buildProductUrl(it, j, 'edit_reservation_item');
            if (!dated) undated.push({ name: it.name, url: editUrl });

            // The date line is the whole point of this row: either it states the
            // rental window, or it is the control that goes and sets one.
            var dateLine = dated
                ? '<p class="mt-1 flex items-center gap-1.5 text-[0.75rem] text-secondary">'
                    + '<span class="material-symbols-outlined" style="font-size: 0.9rem;">calendar_today</span>'
                    + escapeHtml(it.rental)
                    + (editUrl ? '<span class="res-item-edit text-[0.6875rem] font-medium underline underline-offset-4">Change</span>' : '')
                    + '</p>'
                : '<span class="mt-1 inline-flex items-center gap-1.5 text-[0.75rem] font-semibold text-[#b45309]">'
                    + '<span class="material-symbols-outlined" style="font-size: 0.9rem;">event</span>'
                    + (editUrl ? 'Set rental date &rarr;' : 'Rental date needed')
                    + '</span>';

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
                '<p class="mt-0.5 text-[0.75rem] text-secondary">Size: ' +
                escapeHtml(window.arabelaSizeLabel(it.size)) +
                '</p>' +
                dateLine +
                '</div>' +
                '<p class="shrink-0 text-sm font-medium text-on-surface tabular-nums">' +
                formatPesoSummary(it.lineTotal) +
                qtyNote +
                '</p>';

            // Only linkable when the line kept its slug -- older snapshots saved
            // before this shipped have no id, and a broken link is worse than none.
            if (editUrl) {
                html += '<a href="' + attrEscape(editUrl) + '"'
                    + ' class="res-item-row' + (dated ? '' : ' res-item-row--needs-date') + ' flex gap-4 items-start no-underline"'
                    + ' aria-label="' + attrEscape((dated ? 'Change rental dates for ' : 'Set a rental date for ') + (it.name || 'this gown')) + '">'
                    + inner + '</a>';
            } else {
                html += '<div class="flex gap-4 items-start">' + inner + '</div>';
            }
        }
        root.innerHTML = html;

        setTotals(data.subtotal, data.deposit, data.total, data.itemCount);

        // Tell the page which gowns still block checkout, so Confirm Rental can be
        // held back up front instead of failing after the form and the upload.
        if (typeof window.onReservationDateGaps === 'function') {
            window.onReservationDateGaps(undated);
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', window.initReservationOrderSummary);
    } else {
        window.initReservationOrderSummary();
    }
})();
