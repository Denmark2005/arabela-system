/* The wait while a payment receipt is checked (the server reads the picture; it can take up to about 20 seconds).
 *
 *   arabelaReceiptChecking.start()  -- the page's own loading screen is showing: put the message in #receipt-checking-text and change it as time
 *                                      passes ("Checking...", "Still reading it...", "Taking longer than usual...").
 *   arabelaReceiptChecking.stop()   -- the answer came: stop changing the message.
 *   arabelaReceiptChecking.show()   -- a page with no loading screen of its own: build one (white, spinner, message) and start it.
 *   arabelaReceiptChecking.hide()   -- take that one away again.
 * Nothing here talks to the server, and a page that has no #receipt-checking-text is left alone.
 */
(function () {
  var STEPS = [
    [0, 'Checking your receipt… this can take up to 20 seconds.'],
    [8000, 'Still reading it — almost done…'],
    [16000, 'Taking a little longer than usual. Please keep this page open.']
  ];
  var timers = [];
  var overlay = null;

  function clear() {
    timers.forEach(function (id) { window.clearTimeout(id); });
    timers = [];
  }

  function say(text) {
    var el = document.getElementById('receipt-checking-text');
    if (el) el.textContent = text;
  }

  function start() {
    clear();
    STEPS.forEach(function (step) {
      if (step[0] === 0) { say(step[1]); return; }
      timers.push(window.setTimeout(function () { say(step[1]); }, step[0]));
    });
  }

  function stop() { clear(); }

  function show() {
    if (!overlay) {
      var style = document.createElement('style');
      style.textContent = '@keyframes receipt-checking-spin { to { transform: rotate(360deg); } }';
      document.head.appendChild(style);
      overlay = document.createElement('div');
      overlay.id = 'receipt-checking-overlay';
      overlay.setAttribute('role', 'status');
      overlay.setAttribute('aria-live', 'polite');
      overlay.style.cssText = 'position:fixed;inset:0;z-index:999999;background:#ffffff;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;padding:24px;';
      var spinner = document.createElement('div');
      spinner.style.cssText = 'width:64px;height:64px;border:4px solid #e5e2e1;border-top-color:#0b0c0c;border-radius:50%;animation:receipt-checking-spin 1s linear infinite;';
      var message = document.createElement('p');
      message.id = 'receipt-checking-text';
      message.style.cssText = 'margin:0;max-width:320px;text-align:center;font-size:16px;line-height:1.5;color:#2f2e2c;';
      overlay.appendChild(spinner);
      overlay.appendChild(message);
    }
    if (!overlay.parentNode) document.body.appendChild(overlay);
    start();
  }

  function hide() {
    clear();
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
  }

  window.arabelaReceiptChecking = { start: start, stop: stop, show: show, hide: hide, steps: STEPS };
})();
