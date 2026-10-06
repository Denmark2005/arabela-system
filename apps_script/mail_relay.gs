/**
 * Arabela mail relay -- paste this into https://script.google.com (New project) while signed in to the SHOP's Gmail.
 *
 * Why it exists: Render's free plan blocks the normal Gmail connection (SMTP). This small web app lets the website
 * send its emails over HTTPS instead, and Gmail sends them from the shop's own address. Free; Google allows about
 * 100 emails a day on a normal Gmail account (1,500 on Google Workspace).
 *
 * Setup (about 5 minutes):
 *   1. Replace PASTE-A-LONG-RANDOM-SECRET-HERE below with a long random password (30+ letters/numbers). Keep a copy.
 *   2. Click Deploy > New deployment > type "Web app".  Execute as: Me.  Who has access: Anyone.  Deploy.
 *   3. Click "Authorize access" and allow it (Google may say "unverified app" -- Advanced > Go to project). This lets
 *      the script send email AS YOU.
 *   4. Copy the Web app URL (it ends in /exec).
 *   5. On Render > your service > Environment, add:
 *        EMAIL_BACKEND         = accounts.email_backends.AppsScriptRelayBackend
 *        EMAIL_RELAY_URL       = (the /exec URL)
 *        EMAIL_RELAY_SECRET    = (the same secret you typed below)
 *        CUSTOMER_EMAILS_ENABLED = true        (turn this on LAST, after the test email arrives)
 *
 * If you ever edit this script, use Deploy > Manage deployments > Edit > New version (the /exec URL stays the same).
 */
var SECRET = 'PASTE-A-LONG-RANDOM-SECRET-HERE';

function doPost(e) {
  try {
    if (!SECRET || SECRET === 'PASTE-A-LONG-RANDOM-SECRET-HERE') {
      return reply_({ ok: false, error: 'The secret has not been set in the script yet.' });
    }
    var data = JSON.parse(e.postData.contents);
    if (data.secret !== SECRET) {
      return reply_({ ok: false, error: 'secret does not match' });
    }
    if (!data.to || !data.subject) {
      return reply_({ ok: false, error: 'missing recipient or subject' });
    }
    var options = {
      htmlBody: String(data.html || ''),
      name: String(data.name || 'Arabela')
    };
    // Pictures the email refers to as cid:... (the Arabela logo) arrive as base64 and are attached inline.
    var inline = data.inline || {};
    var keys = Object.keys(inline);
    if (keys.length) {
      options.inlineImages = {};
      keys.forEach(function (key) {
        options.inlineImages[key] = Utilities.newBlob(Utilities.base64Decode(inline[key]), 'image/png', key + '.png');
      });
    }
    MailApp.sendEmail(String(data.to), String(data.subject), String(data.text || ''), options);
    return reply_({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return reply_({ ok: false, error: String(err) });
  }
}

// Opening the /exec link in a browser just shows that the relay is alive.
function doGet() {
  return reply_({ ok: true, message: 'Arabela mail relay is running.' });
}

function reply_(body) {
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}
