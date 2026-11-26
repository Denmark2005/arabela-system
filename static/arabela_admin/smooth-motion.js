/*
 * Smooth open and close, and a screen that stays where you are (admin panel). Loaded before bundle.js, so Alpine knows x-slide when it starts.
 *
 * 1. x-slide, put next to x-show: the box slides open and shut instead of popping in and out, taking the same 0.28s as the dropdowns in the
 *    All Bookings window. Nothing slides while the page first draws itself (or when a live update redraws a list), and "reduce motion" turns it off.
 * 2. data-stay-put on a page's scrolling column: when the page gets shorter (a row closes, a filter leaves fewer rows, a live update removes one)
 *    the browser would pull the view up to the new end of the page. The missing room is kept at the end instead, so nothing under the pointer moves;
 *    it is given back as the person scrolls up.
 * 3. Pop-ups stay put: a centred pop-up whose contents grew or shrank used to re-centre itself. From the first click or key press inside it, its top
 *    stays where it is and it only grows downward.
 */
(function () {
  'use strict';
  if (window.ArabelaMotion) { return; }

  var SLIDE_MS = 280;
  var SIDES = ['height', 'paddingTop', 'paddingBottom', 'marginTop', 'marginBottom', 'borderTopWidth', 'borderBottomWidth'];

  function reducedMotion() { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  function nextFrame(fn) { (document.visibilityState === 'visible' ? window.requestAnimationFrame : window.setTimeout)(fn); }

  function scrollBoxes(el) {
    var found = [];
    for (var node = el.parentElement; node; node = node.parentElement) {
      var overflow = window.getComputedStyle(node).overflowY;
      if (overflow === 'auto' || overflow === 'scroll') { found.push(node); }
    }
    return found;
  }

  // ------------------------------------------------------------------ x-slide
  // While a box slides, the browser's "scroll anchoring" must not move the view to follow whatever is below it. A box that starts above what is
  // on screen is the exception: there, anchoring is what keeps the screen still.
  function anchorsOff(boxes, el) {
    var top = el.getBoundingClientRect().top;
    boxes.forEach(function (box) {
      box.arabelaSliding = (box.arabelaSliding || 0) + 1;
      if (top >= box.getBoundingClientRect().top - 1) { box.style.overflowAnchor = 'none'; }
    });
  }
  function anchorsBack(boxes) {
    nextFrame(function () {
      nextFrame(function () {
        boxes.forEach(function (box) {
          box.arabelaSliding -= 1;
          if (box.arabelaSliding > 0) { return; }
          box.arabelaSliding = 0;
          box.style.overflowAnchor = '';
          if (box.arabelaStay) { box.arabelaStay.settle(); }      // the slides are over: give back room that is not needed
        });
      });
    });
  }
  // A box is about to shrink away: the page that stays put makes the room first, so the view never moves, not even for a moment.
  function makeRoom(boxes, frame) {
    var tall = parseFloat(frame.height);
    if (!(tall > 0)) { return; }                  // nothing on screen is going away
    var going = tall + parseFloat(frame.marginTop) + parseFloat(frame.marginBottom);
    boxes.forEach(function (box) { if (box.arabelaStay) { box.arabelaStay.shrinking(going); } });
  }

  // x-show calls these two instead of just showing or hiding (the same hook Alpine's own transitions use).
  function slide(el) {
    var running = null, round = 0, boxes = null, stylesWere = null;

    function asItIs() {      // the box's size on screen right now, also in the middle of a slide
      var style = window.getComputedStyle(el), frame = {};
      SIDES.forEach(function (side) { frame[side] = style[side]; });
      frame.height = el.getBoundingClientRect().height + 'px';
      return frame;
    }
    function shut() {
      var frame = {};
      SIDES.forEach(function (side) { frame[side] = '0px'; });
      return frame;
    }
    function tidy() {        // a finished or turned-around slide leaves nothing behind
      if (running) { running.onfinish = null; running.cancel(); running = null; }
      if (stylesWere) { el.style.overflow = stylesWere[0]; el.style.overflowAnchor = stylesWere[1]; stylesWere = null; }
      if (boxes) { anchorsBack(boxes); boxes = null; }
    }
    function run(from, to, stay, done) {
      [from, to].forEach(function (frame) { frame.boxSizing = 'border-box'; frame.minHeight = '0px'; });
      stylesWere = [el.style.overflow, el.style.overflowAnchor];
      el.style.overflow = 'hidden';
      el.style.overflowAnchor = 'none';          // the browser must never hold on to something inside a box that is sliding
      boxes = scrollBoxes(el);
      anchorsOff(boxes, el);
      running = el.animate([from, to], { duration: SLIDE_MS, easing: 'ease', fill: stay ? 'forwards' : 'none' });
      running.onfinish = done;
    }
    function still() { return reducedMotion() || document.visibilityState !== 'visible' || typeof el.animate !== 'function'; }

    el._x_transition = {
      'in': function (show) {
        var from = running ? asItIs() : null;      // a closing box that is opened again turns around from where it is
        round += 1;
        tidy();
        show();
        if (still()) { return; }
        var to = asItIs();
        if (parseFloat(to.height) > 0) { run(from || shut(), to, false, tidy); }
      },
      out: function (before, hidden) {
        var from = asItIs();
        var mine = ++round;                        // opened again before it finished: this closing must not hide it any more
        tidy();
        makeRoom(scrollBoxes(el), from);
        if (still() || !(parseFloat(from.height) > 0)) { nextFrame(function () { if (mine === round) { hidden(); } }); return; }
        run(from, shut(), true, function () {
          hidden();                                // x-show now hides it, before the next picture is drawn
          nextFrame(function () { if (mine === round) { tidy(); } });
        });
      }
    };
  }

  // ------------------------------------------------------------------ the page stays where you are
  function stayPut() {
    var box = document.querySelector('[data-stay-put]');       // the panel's own scrolling column, on the pages that ask for it
    var main = box && box.querySelector('main');
    if (!main) { return; }
    if (window.getComputedStyle(box).position === 'static') { box.style.position = 'relative'; }
    var room = document.createElement('div');       // empty space at the very end, only as tall as it must be
    room.setAttribute('data-stay-room', '');
    room.setAttribute('aria-hidden', 'true');
    room.style.cssText = 'flex:0 0 auto;height:0;overflow-anchor:none;pointer-events:none;';
    box.appendChild(room);
    var kept = 0, lastTop = box.scrollTop, lastLength = room.offsetTop;

    function keep(height) {
      height = Math.max(0, Math.ceil(height));
      if (height !== kept) { kept = height; room.style.height = height + 'px'; }
    }
    function check() {
      var length = room.offsetTop;                 // how long the page's own contents are
      var top = box.scrollTop;
      var end = Math.max(0, length + kept - box.clientHeight);
      if (length < lastLength && top < lastTop - 0.5 && top >= end - 1.5) {
        // it got shorter and the browser pulled the view up to the new end: make the room and go back
        keep(lastTop + box.clientHeight - length);
        box.scrollTop = lastTop;
      } else {
        lastTop = top;
        if (kept && !box.arabelaSliding) { keep(Math.min(kept, top + box.clientHeight - length)); }
      }
      lastLength = length;
    }
    box.arabelaStay = {
      settle: check,
      shrinking: function (going) {                // a sliding box is about to take `going` pixels away
        lastTop = box.scrollTop;
        var need = lastTop + box.clientHeight - (room.offsetTop - going);
        if (need > kept) { keep(need); }
      }
    };
    box.addEventListener('scroll', check, { passive: true });
    window.addEventListener('resize', check);
    window.addEventListener('beforeprint', function () { keep(0); });
    if (window.ResizeObserver) { new ResizeObserver(check).observe(main); }
  }

  // ------------------------------------------------------------------ pop-ups stay put
  // The panel's centred pop-ups. Not the ones that already start at the top (receipt, payment and booking windows) and not the photo viewers.
  var POPUPS = '.fixed.inset-0.items-center, .adm-dlg, .ar-overlay, .ca-overlay, .cb-overlay, .idc-win, .rr-overlay.rr-z2';
  var pinned = [];

  function showing(layer) {
    return layer.isConnected && layer.getClientRects().length > 0 && window.getComputedStyle(layer).visibility !== 'hidden';
  }
  function release(entry) {
    pinned.splice(pinned.indexOf(entry), 1);
    entry.watcher.disconnect();
    entry.layer.style.alignItems = entry.align;
    entry.layer.style.overflowY = entry.overflow;
    entry.card.style.marginTop = entry.margin;
    entry.card.style.maxHeight = entry.tallest;
  }
  function pin(event) {
    var target = event.target;
    var layer = target && target.closest ? target.closest(POPUPS) : null;
    if (!layer || pinned.some(function (entry) { return entry.layer === layer; }) || !showing(layer)) { return; }
    var card = target;
    while (card && card.parentElement !== layer) { card = card.parentElement; }
    if (!card) { return; }
    var cardStyle = window.getComputedStyle(card);
    if (cardStyle.position === 'fixed' || cardStyle.position === 'absolute') { return; }      // the dark layer behind the pop-up
    var layerStyle = window.getComputedStyle(layer);
    var down = card.offsetTop - parseFloat(layerStyle.paddingTop);
    if (!(down >= 0)) { return; }                                                             // taller than the screen: leave it alone
    var entry = { layer: layer, card: card, align: layer.style.alignItems, overflow: layer.style.overflowY, margin: card.style.marginTop, tallest: card.style.maxHeight };
    layer.style.alignItems = 'flex-start';
    card.style.marginTop = down + 'px';
    if (layerStyle.overflowY === 'visible') { layer.style.overflowY = 'auto'; }
    // A pop-up with its own height limit scrolls inside itself: its lower edge must stay on the screen.
    if (cardStyle.maxHeight !== 'none') { card.style.maxHeight = Math.max(240, layer.clientHeight - card.offsetTop - parseFloat(layerStyle.paddingBottom)) + 'px'; }
    entry.watcher = new MutationObserver(function () { if (pinned.indexOf(entry) !== -1 && !showing(layer)) { release(entry); } });   // closed: centred again next time
    entry.watcher.observe(layer, { attributes: true, attributeFilter: ['class', 'style', 'hidden', 'aria-hidden'] });
    pinned.push(entry);
  }

  document.addEventListener('alpine:init', function () { window.Alpine.directive('slide', slide); });
  document.addEventListener('alpine:initialized', stayPut);
  document.addEventListener('pointerdown', pin, true);
  document.addEventListener('keydown', pin, true);
  window.addEventListener('resize', function () { pinned.slice().forEach(release); });

  window.ArabelaMotion = { slideMs: SLIDE_MS };
})();
