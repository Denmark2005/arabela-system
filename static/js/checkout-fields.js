/* What each checkout box starts with: the profile's saved default, or what the customer typed earlier.

   The rule (pure, so it can be tested on its own -- gowns/test_checkout_fields.py):
     * what the customer typed in THIS checkout is kept when the page reloads (a default they typed over is not put back);
     * a default saved in the profile wins over anything remembered from an EARLIER checkout -- and over what was typed before the
       default was changed;
     * with no default for a box, what was typed before comes back.
   A remembered value therefore carries the default it was typed against ("base"): it is only trusted while the profile still says the
   same thing for that box. A default that has changed since (or one that was added later) makes the remembered value stale. */
(function (root) {
    'use strict';

    function text(v) { return String(v == null ? '' : v).trim(); }

    // baseDefault: the profile's value for this box ('' when it has none).
    // kept: what the draft remembers for it -- { value, base }, or a bare string (the shape the first version of the draft saved,
    // which carries no default and so is only ever used when the profile has none).
    function chooseValue(baseDefault, kept) {
        var base = text(baseDefault);
        var entry = typeof kept === 'string' ? { value: kept, base: '' } : kept;
        if (entry && typeof entry.value === 'string' && text(entry.value) && text(entry.base) === base) {
            return entry.value;
        }
        return base;
    }

    root.arabelaCheckoutFields = { chooseValue: chooseValue };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
