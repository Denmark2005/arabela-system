(function () {
    var STORAGE_KEY = 'arabela_reservation_cart';
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
            total: sub + deposit
        };
        try {
            sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
        } catch (err) {}
        var a = e && e.currentTarget;
        if (a && a.href) window.location.href = a.href;
        return false;
    };

    window.initReservationOrderSummary = function () {
        var root = document.getElementById('reservation-items-root');
        if (!root) return;

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
