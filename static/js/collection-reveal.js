(function () {
  'use strict';

  // Staggered reveal for the collection product grids: each card starts slightly low
  // and transparent, then settles into place a beat after the one before it, so a page
  // of gowns arrives as a wave instead of every card appearing at once. This is what
  // runs after the Previous/Next pagination links load a new page.
  //
  // Two details of THIS site make a plain CSS animation the wrong tool, and both are
  // the reason the reveal is driven from JavaScript instead:
  //
  // 1. base.html holds the whole document at `visibility: hidden` until
  //    document.fonts.ready and only then reveals it. A CSS animation would start at
  //    first paint and be over before the document was ever shown -- the visitor would
  //    see the finished grid and no animation at all. The wave is therefore started
  //    from that same fonts-ready signal, so it begins the moment the page is visible.
  // 2. collection-sort.js reorders a grid by MOVING the existing <a> nodes with
  //    appendChild. Re-inserting a node restarts a CSS animation, so every sort would
  //    replay the whole intro. A transition does not restart on a move, and stripping
  //    the helper classes and inline delays once the intro has finished leaves the DOM
  //    in a plain state that sorting can shuffle around freely.
  //
  // The hidden starting state itself lives in CSS behind `html.arb-stagger`, added by
  // an inline script in base.html's <head> so it is in force from the very first paint
  // -- setting it from here would briefly show the cards and then snap them away. That
  // head script also drops the class on a timer, so this file failing to load can
  // never leave a grid permanently invisible.

  var STAGGER_MS = 55;      // gap between one card starting and the next
  var MAX_DELAY_MS = 520;   // ceiling, so a long grid never crawls in for seconds
  var DURATION_MS = 700;    // keep in step with the CSS transition duration
  var CLEANUP_PAD_MS = 150;

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function isArmed() {
    return document.documentElement.classList.contains('arb-stagger');
  }

  function disarm() {
    document.documentElement.classList.remove('arb-stagger');
  }

  function root() {
    return document.querySelector('[data-collection-sort-root]');
  }

  function grids() {
    var r = root();
    if (!r) return [];
    return Array.prototype.slice.call(r.querySelectorAll('.grid'));
  }

  function cardsOf(grid) {
    return Array.prototype.slice.call(grid.children).filter(function (el) {
      return el.tagName === 'A';
    });
  }

  function revealAll() {
    // The head script's safety timer may already have shown everything (a very slow
    // page); re-hiding at this point would be a flicker for no reason.
    if (!isArmed()) return;

    if (prefersReducedMotion()) {
      disarm();
      return;
    }

    var longest = 0;
    var touched = [];

    grids().forEach(function (grid) {
      // all.html keeps one category panel visible and the rest at display:none. A card
      // in a hidden panel would spend its delay unseen and then be shown with the
      // animation already finished, so only what is actually on screen is animated --
      // the rest just become visible when the root class is dropped below.
      if (grid.offsetParent === null) return;

      cardsOf(grid).forEach(function (card, index) {
        var delay = Math.min(index * STAGGER_MS, MAX_DELAY_MS);
        if (delay > longest) longest = delay;
        card.style.transitionDelay = delay + 'ms';
        card.classList.add('arb-in');
        touched.push(card);
      });
    });

    if (!touched.length) {
      disarm();
      return;
    }

    window.setTimeout(function () {
      // Order matters here. Dropping the root class first lets the cards fall back to
      // their normal fully-visible styling in the same frame the helper classes go
      // away; removing `arb-in` while the root class still applied would snap every
      // card back to transparent for a frame.
      disarm();
      touched.forEach(function (card) {
        card.classList.remove('arb-in');
        card.style.transitionDelay = '';
      });
    }, longest + DURATION_MS + CLEANUP_PAD_MS);
  }

  function start() {
    // Two frames: the first lets the browser apply the hidden starting state, the
    // second starts the transition from it. Collapsed into one frame the browser
    // recalculates both at once and nothing appears to move.
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(revealAll);
    });
  }

  function boot() {
    if (!root()) {
      disarm();
      return;
    }
    // The same signal base.html uses to un-hide the document, so the wave starts when
    // the page actually becomes visible rather than behind a blank screen.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(start, start);
    } else {
      window.addEventListener('load', start);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
