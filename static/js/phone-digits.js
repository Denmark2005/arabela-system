/*
 * Mobile-number boxes: digits only, at most 11 (09XXXXXXXXX). Any input marked data-phone-digits is cleaned as the number is typed,
 * pasted or autofilled, so a letter, a space, a dash or a "+" never stays in the box and a 12th digit cannot be typed.
 * A number written with the country code (+63 917 123 4567, 639171234567) is turned into 09171234567, so a paste or a browser
 * autofill is not cut to a wrong 11 digits.
 *
 * This only keeps wrong characters out of the box. The page's own checks ("Invalid number.") and the server's rules are unchanged.
 * It listens on the document in the capture phase, so the box already holds the cleaned value when the page's own input handlers
 * (the New Booking form, the checkout's remembered answers) read it.
 */
(function () {
    'use strict';

    var MAX_DIGITS = 11;

    function digitsOf(text) {
        return String(text == null ? '' : text).replace(/\D/g, '');
    }

    function clean(raw) {
        var digits = digitsOf(raw);
        // Only a whole number arriving at once (paste, autofill) can be 12 digits: the box stops typing at 11.
        if (digits.length === 12 && digits.indexOf('63') === 0) {
            digits = '0' + digits.slice(2);
        }
        return digits.slice(0, MAX_DIGITS);
    }

    function onChange(event) {
        var box = event.target;
        if (!box || !box.hasAttribute || !box.hasAttribute('data-phone-digits')) return;
        var raw = box.value;
        var cleaned = clean(raw);
        if (cleaned === raw) return;

        var caret = null;
        try { caret = box.selectionStart; } catch (err) { caret = null; }
        var countryCodeFixed = digitsOf(raw).length === 12 && digitsOf(raw).indexOf('63') === 0;

        box.value = cleaned;

        // Keep the cursor where the customer was typing (a letter typed in the middle should not throw it to the end).
        if (caret !== null && document.activeElement === box) {
            var at = countryCodeFixed ? cleaned.length : Math.min(digitsOf(raw.slice(0, caret)).length, cleaned.length);
            try { box.setSelectionRange(at, at); } catch (err2) { /* some keyboards do not allow it: the cursor simply stays at the end */ }
        }
    }

    document.addEventListener('input', onChange, true);
    document.addEventListener('change', onChange, true);       // a browser autofill can arrive as a change only
})();
