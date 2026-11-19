/* The wait while a payment receipt is checked (the server reads the picture; about 5 to 8 seconds, and never more than about 15).
 *
 * The customer sees a plain white loading screen with a spinner and NO words (the owner's choice, 2026-11-18). Screen readers still hear
 * "Checking your receipt" (aria-label), nothing is printed on the screen.
 *
 *   arabelaReceiptChecking.start()  -- the page's own loading screen is showing. Nothing to do any more (there is no message to change as time
 *                                      passes); kept so the pages' calls keep working.
 *   arabelaReceiptChecking.stop()   -- the answer came. Same: kept so the pages' calls keep working.
 *   arabelaReceiptChecking.show()   -- a page with no loading screen of its own: build one (white, spinner) and show it.
 *   arabelaReceiptChecking.hide()   -- take that one away again.
 * Nothing here talks to the server.
 */
(function () {
  var overlay = null;

  function start() {}

  function stop() {}

  function show() {
    if (!overlay) {
      var style = document.createElement('style');
      style.textContent = '@keyframes receipt-checking-spin { to { transform: rotate(360deg); } }';
      document.head.appendChild(style);
      overlay = document.createElement('div');
      overlay.id = 'receipt-checking-overlay';
      overlay.setAttribute('role', 'status');
      overlay.setAttribute('aria-label', 'Checking your receipt');
      overlay.style.cssText = 'position:fixed;inset:0;z-index:999999;background:#ffffff;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;';
      var spinner = document.createElement('div');
      spinner.style.cssText = 'width:64px;height:64px;border:4px solid #e5e2e1;border-top-color:#0b0c0c;border-radius:50%;animation:receipt-checking-spin 1s linear infinite;';
      overlay.appendChild(spinner);
    }
    if (!overlay.parentNode) document.body.appendChild(overlay);
    start();
  }

  function hide() {
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
  }

  window.arabelaReceiptChecking = { start: start, stop: stop, show: show, hide: hide };
})();
