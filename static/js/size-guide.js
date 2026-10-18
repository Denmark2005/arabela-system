/*
 * The gown page's Size Guide: opens from the "Size Guide" link and shows the measurement table (drawn by templates/products.html from
 * gowns/size_guides.py). The row of the size chosen on the page is marked; the guide only reads the page's own size buttons
 * (aria-pressed) and never chooses a size itself. Nothing in it scrolls or slides: it fades in and fades out.
 */
(function () {
    'use strict';

    var root = document.getElementById('size-guide-root');
    var opener = document.getElementById('size-guide-open');
    if (!root || !opener) return;

    var panel = root.querySelector('.sg-panel');
    var closeButton = root.querySelector('.sg-close');
    var rows = Array.prototype.slice.call(root.querySelectorAll('tbody tr[data-size-code]'));
    var pageButtons = Array.prototype.slice.call(document.querySelectorAll('[data-product-size]'));

    var CLOSE_AFTER_MS = 260;
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
    var isOpen = false;
    var closeTimer = null;

    function pageSize() {
        for (var i = 0; i < pageButtons.length; i++) {
            if (pageButtons[i].getAttribute('aria-pressed') === 'true') return pageButtons[i].getAttribute('data-product-size');
        }
        return '';
    }

    function markChosenSize() {
        var chosen = pageSize();
        rows.forEach(function (row) {
            row.classList.toggle('is-selected', !!chosen && row.getAttribute('data-size-code') === chosen);
        });
    }

    function open() {
        if (isOpen) return;
        isOpen = true;
        clearTimeout(closeTimer);
        markChosenSize();
        root.hidden = false;
        root.classList.add('is-open');
        document.documentElement.classList.add('sg-lock');
        opener.setAttribute('aria-expanded', 'true');
        // two frames: the panel must be on screen in its starting state before the "visible" class starts the fade
        window.requestAnimationFrame(function () {
            window.requestAnimationFrame(function () {
                if (!isOpen) return;
                root.classList.add('is-visible');
                try { panel.focus({ preventScroll: true }); } catch (e) { panel.focus(); }
            });
        });
    }

    function finishClose() {
        root.classList.remove('is-open');
        root.hidden = true;
        document.documentElement.classList.remove('sg-lock');
    }

    function close() {
        if (!isOpen) return;
        isOpen = false;
        root.classList.remove('is-visible');
        opener.setAttribute('aria-expanded', 'false');
        clearTimeout(closeTimer);
        if (reduceMotion && reduceMotion.matches) finishClose();
        else closeTimer = setTimeout(finishClose, CLOSE_AFTER_MS);
        try { opener.focus({ preventScroll: true }); } catch (e) { opener.focus(); }
    }

    opener.addEventListener('click', open);

    root.addEventListener('click', function (event) {
        if (event.target.closest('[data-size-guide-close]')) close();
    });

    document.addEventListener('keydown', function (event) {
        if (!isOpen) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
        } else if (event.key === 'Tab') {
            // the only thing to reach is the close button, so keep the focus on it
            event.preventDefault();
            closeButton.focus();
        }
    });
})();
