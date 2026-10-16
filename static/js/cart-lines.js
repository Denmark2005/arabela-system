/* The rows of a customer's selection (the "bag"): the rules for what counts as the same row, and how many of one gown a bag may hold.

   Why this exists: Book Now used to add a NEW row every time it was pressed, so going Back from the selection and pressing it again
   (or just tapping it a few times) stacked identical rows of the same gown. Checkout gives every gown in the bag its own physical
   unit, so a gown that only exists once was refused at the very last step ("no longer available") after the whole form and the
   payment receipt were filled in.

   The rules, kept in ONE place so the product page, the drawer, the selection page and checkout cannot disagree:
     * the same gown + size + dates is ONE row; "more than one" is its quantity (capped at how many units exist);
     * a bag never holds more of a gown (in one size) than exists, whatever the dates -- each gown takes its own unit.

   Pure functions only (no page, no storage), so they can be tested on their own (gowns/test_cart_lines.py). */
(function (root) {
    'use strict';

    function text(v) { return String(v == null ? '' : v).trim(); }
    // 'Size: M' (the checkout snapshot's shape), 'M' and '-' (not chosen yet) all compare as the size they mean.
    function sizeOf(item) {
        var t = text(item && item.size).replace(/^Size:\s*/i, '').trim();
        return t === '-' ? '' : t;
    }
    function rentalOf(item) {
        var t = text(item && item.rental);
        return t === '-' ? '' : t;
    }
    function collectionOf(item) { return item && item.collection ? text(item.collection) : 'wedding'; }
    function nameOf(item) { return (item && item.name) || 'Untitled'; }
    // A row with no maxQty (an older bag, or stock that wasn't reported) is never capped: Infinity, so nothing that used to
    // work starts being refused.
    function maxOf(item) { var n = Number(item && item.maxQty); return n > 0 ? n : Infinity; }
    function qtyOf(item) { var n = Math.floor(Number(item && item.qty)); return n >= 1 ? n : 1; }

    // The same gown and size, whatever the dates.
    function sameGown(a, b) {
        if (!a || !b) return false;
        return text(a.id) === text(b.id) && nameOf(a) === nameOf(b) && collectionOf(a) === collectionOf(b) && sizeOf(a) === sizeOf(b);
    }

    // The same gown, size AND dates: the very same row.
    function sameLine(a, b) {
        return sameGown(a, b) && rentalOf(a) === rentalOf(b);
    }

    // How many of this gown (this size, any dates) the bag already holds.
    function heldOf(cart, item) {
        var total = 0;
        (cart || []).forEach(function (line) { if (sameGown(line, item)) total += qtyOf(line); });
        return total;
    }

    // Merges rows that are the very same row (their quantities add up, never past what exists) and brings every quantity into
    // 1..what exists. Returns { cart, changed }: `changed` tells the caller whether to save it back. Safe to run on every load:
    // a bag that is already tidy comes back unchanged.
    function tidy(cart) {
        if (!Array.isArray(cart)) return { cart: [], changed: false };
        var out = [];
        var changed = false;
        cart.forEach(function (line) {
            if (!line || typeof line !== 'object') { changed = true; return; }
            var twin = null;
            for (var i = 0; i < out.length; i++) {
                if (sameLine(out[i], line)) { twin = out[i]; break; }
            }
            if (!twin) { out.push(line); return; }
            if (!(Number(twin.maxQty) > 0) && Number(line.maxQty) > 0) twin.maxQty = line.maxQty;
            twin.qty = Math.min(qtyOf(twin) + qtyOf(line), maxOf(twin), maxOf(line));
            changed = true;
        });
        out.forEach(function (line) {
            var fixed = Math.min(qtyOf(line), maxOf(line));
            if (line.qty !== fixed) { line.qty = fixed; changed = true; }
        });
        return { cart: out, changed: changed };
    }

    // The gowns the bag holds more of than exist: [{ name, size, wanted, cap }]. Empty when the bag can be booked.
    function overbooked(cart) {
        var groups = [];
        (cart || []).forEach(function (line) {
            if (!line) return;
            var group = null;
            for (var i = 0; i < groups.length; i++) {
                if (sameGown(groups[i].sample, line)) { group = groups[i]; break; }
            }
            if (!group) {
                group = { sample: line, name: nameOf(line), size: sizeOf(line), wanted: 0, cap: Infinity };
                groups.push(group);
            }
            group.wanted += qtyOf(line);
            group.cap = Math.min(group.cap, maxOf(line));
        });
        return groups
            .filter(function (g) { return g.wanted > g.cap; })
            .map(function (g) { return { name: g.name, size: g.size, wanted: g.wanted, cap: g.cap }; });
    }

    // One plain sentence about an overbooked gown, shared by every place that warns about it.
    function overbookedMessage(over) {
        if (!over || !over.length) return '';
        var g = over[0];
        var where = g.size ? ' in size ' + g.size : '';
        var more = over.length > 1 ? ' (and ' + (over.length - 1) + ' more gown' + (over.length > 2 ? 's' : '') + ' like it)' : '';
        return g.name + ' is in your selection ' + g.wanted + ' times, but only ' + g.cap + (g.cap === 1 ? ' exists' : ' exist') + where
            + '. Please keep ' + g.cap + ' and remove the rest' + more + '.';
    }

    root.arabelaCartLines = {
        sameGown: sameGown,
        sameLine: sameLine,
        heldOf: heldOf,
        tidy: tidy,
        overbooked: overbooked,
        overbookedMessage: overbookedMessage
    };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
