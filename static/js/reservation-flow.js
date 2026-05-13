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
                existing.qty = (Number(existing.qty) || 1) + (Number(gitem.qty) || 1);
            } else {
                userCart.push(gitem);
            }
        }
        try {
            localStorage.setItem(userCartKey, JSON.stringify(userCart));
            localStorage.removeItem(GUEST_CART_KEY);
        } catch (e4) {}
    }

    window.saveCartAndProceedToReservation = function (e) {
        if (e && e.preventDefault) e.preventDefault();
        var items = [];
        var sub = 0;
        var itemCount = 0;
        var rows = document.querySelectorAll('#cart-drawer .cart-line');
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            if (row.classList.contains('hidden')) continue;
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
                rental: rental
            });
        }
        if (!items.length) {
            window.alert('Add at least one item to your selection before continuing.');
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
        try {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
        } catch (err) {}
        var a = e && e.currentTarget;
        var href = (a && a.href) ? a.href : '';
        var body = document && document.body;
        var isAuthenticated = body && body.dataset ? body.dataset.authenticated : '';
        var loginUrl = body && body.dataset ? body.dataset.loginUrl : '';
        if (isAuthenticated === 'false' && loginUrl) {
            var nextParam = encodeURIComponent(href || '/');
            window.location.href = loginUrl + '?next=' + nextParam;
            return false;
        }
        if (href) window.location.href = href;
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
        var gcashFull = document.getElementById('gcash-full-amount');

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
            if (gcashFull) gcashFull.textContent = 'Amount: ' + formatPesoInt(total);
        }

        if (!raw) {
            root.innerHTML =
                '<p class="text-[0.6875rem] uppercase tracking-widest text-secondary">No items in your selection. Return to the shop and add pieces first.</p>';
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
        for (var j = 0; j < items.length; j++) {
            var it = items[j];
            var qtyNote =
                it.qty > 1
                    ? ' <span class="text-secondary font-normal normal-case">(\u00d7' + it.qty + ')</span>'
                    : '';
            html +=
                '<div class="flex gap-6 items-start">' +
                '<div class="w-24 h-32 bg-surface-container flex-shrink-0 overflow-hidden rounded-lg">' +
                '<img class="w-full h-full object-cover grayscale hover:grayscale-0 transition-all duration-700" alt="' +
                attrEscape(it.imageAlt || it.name) +
                '" src="' +
                attrEscape(it.image) +
                '">' +
                '</div>' +
                '<div class="flex flex-col justify-between h-full py-1">' +
                '<div>' +
                '<h3 class="font-headline text-lg leading-tight uppercase tracking-tight">' +
                escapeHtml(it.name) +
                '</h3>' +
                '<p class="text-[0.6875rem] uppercase tracking-widest text-secondary mt-1">' +
                escapeHtml(it.size) +
                '</p>' +
                '<p class="text-[0.6875rem] uppercase tracking-widest text-primary mt-1 font-semibold">' +
                formatPesoSummary(it.lineTotal) +
                qtyNote +
                '</p>' +
                '</div>' +
                '<div class="mt-4 flex items-center gap-2">' +
                '<span class="material-symbols-outlined text-sm text-secondary">calendar_today</span>' +
                '<span class="text-[0.6875rem] uppercase tracking-widest font-medium">' +
                escapeHtml(it.rental || 'TBD') +
                '</span>' +
                '</div>' +
                '</div></div>';
        }
        root.innerHTML = html;

        setTotals(data.subtotal, data.deposit, data.total, data.itemCount);
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', window.initReservationOrderSummary);
    } else {
        window.initReservationOrderSummary();
    }
})();
