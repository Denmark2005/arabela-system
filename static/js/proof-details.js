/* The GCash reference number and the amount a customer types from their receipt (the server's rules are in gowns/typed_payment.py; this file
   gives the same answers on the page so a box is checked as it is typed, and the server still has the last word).

     arabelaProofDetails.cleanReference(text)      -> the digits of whatever was typed or pasted
     arabelaProofDetails.formatReference(text)     -> '5046 933 479702' (the way GCash prints it), at most 13 digits
     arabelaProofDetails.referenceProblem(text)    -> '' or the sentence to show under the box
     arabelaProofDetails.parseAmount(text)         -> a number with at most two decimals, or null
     arabelaProofDetails.amountProblem(text, due)  -> '' or the sentence to show under the box
     arabelaProofDetails.peso(number)              -> '₱2,000'
     arabelaProofDetails.attach(referenceInput)    -> regroups the digits while the customer types or pastes

   Pure functions (no page, no storage), so they can be tested on their own (gowns/test_proof_details_js.py). */
(function (root) {
    'use strict';

    var REFERENCE_DIGITS = 13;
    var MAX_AMOUNT = 1000000;

    function text(value) { return String(value == null ? '' : value); }

    function cleanReference(value) {
        var s = text(value);
        if (typeof s.normalize === 'function') s = s.normalize('NFKC');       // full-width digits from some phone keyboards
        return s.replace(/\D/g, '');
    }

    function formatReference(value) {
        var digits = cleanReference(value).slice(0, REFERENCE_DIGITS);
        var parts = [digits.slice(0, 4), digits.slice(4, 7), digits.slice(7)];
        return parts.filter(function (part) { return part.length; }).join(' ');
    }

    function referenceProblem(value) {
        var digits = cleanReference(value);
        if (!digits) return 'Please type the ' + REFERENCE_DIGITS + '-digit reference number from your GCash receipt.';
        if (digits.length !== REFERENCE_DIGITS) {
            return 'A GCash reference number has ' + REFERENCE_DIGITS + ' digits, and you typed ' + digits.length + '. Please check the Ref No. on your receipt.';
        }
        return '';
    }

    function parseAmount(value) {
        var s = text(value);
        if (typeof s.normalize === 'function') s = s.normalize('NFKC');
        s = s.trim().replace(/^(?:php|p|₱)\s*/i, '').replace(/,/g, '').replace(/\s/g, '');
        if (!/^\d{1,9}(?:\.\d{1,2})?$/.test(s)) return null;
        var amount = Math.round(parseFloat(s) * 100) / 100;
        return amount > 0 && amount <= MAX_AMOUNT ? amount : null;
    }

    function peso(amount) {
        var n = Number(amount) || 0;
        var whole = Math.abs(n - Math.round(n)) < 0.005;
        return '₱' + n.toLocaleString('en-US', whole ? { maximumFractionDigits: 0 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function amountProblem(value, due) {
        if (!text(value).trim()) return 'Please type the amount you paid.';
        var amount = parseAmount(value);
        if (amount === null) return 'Please type the amount as numbers only, like 2000 or 2,000.00.';
        if (amount < Number(due) - 0.005) {
            return 'You typed ' + peso(amount) + ', but the deposit for this booking is ' + peso(due) + '. Please check the amount you paid.';
        }
        return '';
    }

    // Regroups the digits as they are typed or pasted: "5046933479702" becomes "5046 933 479702". Only digits are ever kept, at most 13.
    function attach(input) {
        if (!input || input.getAttribute('data-proof-reference-attached')) return;
        input.setAttribute('data-proof-reference-attached', '1');
        input.addEventListener('input', function () {
            var formatted = formatReference(input.value);
            if (formatted !== input.value) {
                input.value = formatted;
                try { input.setSelectionRange(formatted.length, formatted.length); } catch (e) { /* some input types refuse a caret */ }
            }
        });
    }

    root.arabelaProofDetails = {
        cleanReference: cleanReference, formatReference: formatReference, referenceProblem: referenceProblem,
        parseAmount: parseAmount, amountProblem: amountProblem, peso: peso, attach: attach, REFERENCE_DIGITS: REFERENCE_DIGITS
    };
})(typeof window !== 'undefined' ? window : this);
