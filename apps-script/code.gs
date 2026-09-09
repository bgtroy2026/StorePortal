/**
 * Big Grove Store Director Portal — sign-in backend (Google Apps Script)
 *
 * Same design as the Sales Portal backend (Portal Login), deployed as a SEPARATE Apps Script project so the
 * two portals have independent rosters and secrets.
 *
 * Deploy: New deployment → Web app → Execute as: Me → Who has access: Anyone.
 * To UPDATE without changing the URL: Deploy → Manage deployments → ✏️ Edit → Version: New version → Deploy.
 *
 * Script Properties required (Project Settings → Script properties):
 *   CLIENT_ID      — the OAuth client ID (…apps.googleusercontent.com), same one pasted into site/index.html CFG
 *   SHEET_ID       — ID of the "Store Director Roster" Google Sheet
 *   PORTAL_SECRET  — EXACTLY the same value as the PORTAL_SECRET GitHub Actions secret. Bundle keys are derived
 *                    from it (HMAC-SHA256(secret, label)); nothing else has to be shared between the two systems.
 *   LOG_SECRET     — auto-created on first use (session-token signing key)
 *
 * Roster sheet, first tab, headers in row 1:  email | name | role | locations
 *   role:      Admin, Leadership, or Director (case-insensitive)
 *   locations: for Directors — comma-separated location slugs from config/locations.json (e.g. "solon" or
 *              "solon,iowa-city"). Admin/Leadership see everything ("all").
 * A Director with ONE location gets the bundle for that location only (other taprooms' data is not in the file
 * they download). A Director with several locations gets the "all" bundle plus a client-side allow-list.
 *
 * Usage logging: every sign-in and portal open is appended to a "Logins" tab (auto-created):
 *   When | Email | Name | Role | Event
 *
 * Request protocol (POST body, Content-Type text/plain to avoid CORS preflight):
 *   {"a":"signin","t":<id token>}  → sign-in → { ok, profile:{email,name,role,locations}, bundles:[{label,id,key}], sid }
 *   {"a":"ping","s":<sid>}         → log a portal open
 */

var ALLOWED_DOMAINS = ['biggrove.com', 'biggrovebrewery.com'];
var SESSION_HOURS = 24;

function doPost(e) {
  var out;
  try { out = handle(((e && e.postData && e.postData.contents) || '').trim()); }
  catch (err) { out = { ok: false, error: 'server error: ' + err }; }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

function handle(body) {
  if (!body) return { ok: false, error: 'no token' };
  var req;
  try { req = JSON.parse(body); } catch (e) { return doSignin(body); }
  if (req.a === 'ping') return doPing(req.s);
  if (req.a === 'signin') return doSignin(String(req.t || ''));
  return { ok: false, error: 'unknown action' };
}

function doSignin(token) {
  if (!token) return { ok: false, error: 'no token' };
  var props = PropertiesService.getScriptProperties();
  var resp = UrlFetchApp.fetch('https://oauth2.googleapis.com/tokeninfo', { method: 'post', payload: { id_token: token }, muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) return { ok: false, error: 'invalid token' };
  var info = JSON.parse(resp.getContentText());
  if (info.aud !== props.getProperty('CLIENT_ID')) return { ok: false, error: 'wrong app' };
  if (Number(info.exp) * 1000 < Date.now()) return { ok: false, error: 'expired token' };
  if (String(info.email_verified) !== 'true') return { ok: false, error: 'unverified email' };

  var email = String(info.email || '').toLowerCase().trim();
  var domain = email.split('@')[1] || '';
  if (ALLOWED_DOMAINS.indexOf(domain) === -1) return { ok: false, error: 'outside domain' };

  var rows = rosterSheet(SpreadsheetApp.openById(props.getProperty('SHEET_ID'))).getDataRange().getValues();
  var hit = null;
  for (var i = 1; i < rows.length; i++) if (String(rows[i][0]).toLowerCase().trim() === email) { hit = rows[i]; break; }
  if (!hit) return { ok: false, error: 'not on the roster' };

  var name = String(hit[1] || info.name || '');
  var role = String(hit[2] || 'Director').trim();
  var locs = String(hit[3] || '').toLowerCase().replace(/\s+/g, '');
  var access = portalAccess(props, role, locs);
  logEvent(email, name, role, 'signin');
  return { ok: true, profile: { email: email, name: name, role: role, locations: access.locations }, bundles: access.bundles, sid: makeSid(email, name, role) };
}

// ---- which bundle(s) this person may open, and the keys (derived, never stored) --------------------------

function portalAccess(props, role, locs) {
  var secret = props.getProperty('PORTAL_SECRET');
  if (!secret) throw new Error('PORTAL_SECRET script property is not set');
  var all = /^(leadership|admin)/i.test(role) || locs === 'all' || !locs;
  if (all) return { locations: 'all', bundles: [bundleFor(secret, 'all')] };
  var list = locs.split(',').filter(Boolean);
  if (list.length === 1) return { locations: list[0], bundles: [bundleFor(secret, 'loc:' + list[0])] };
  return { locations: list.join(','), bundles: [bundleFor(secret, 'all')] };  // multi-site director: full bundle, client-side allow-list
}

function bundleFor(secret, label) {
  var dig = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, label, Utilities.Charset.UTF_8);
  var hex = dig.map(function (b) { return ('0' + (b & 255).toString(16)).slice(-2); }).join('');
  var key = Utilities.base64Encode(Utilities.computeHmacSha256Signature(label, secret, Utilities.Charset.UTF_8));
  return { label: label, id: hex.slice(0, 16), key: key };
}

// ---- portal-open ping -----------------------------------------------------------------------------------

function doPing(sid) {
  var s = readSid(sid);
  if (!s) return { ok: false, error: 'expired session' };
  logEvent(s.e, s.n, s.r, 'open');
  return { ok: true };
}

// ---- helpers --------------------------------------------------------------------------------------------

function rosterSheet(ss) {
  var shs = ss.getSheets();
  for (var i = 0; i < shs.length; i++) if (String(shs[i].getRange(1, 1).getValue()).toLowerCase().trim() === 'email') return shs[i];
  return shs[0];
}

function logEvent(email, name, role, event) {
  try {
    var ss = SpreadsheetApp.openById(PropertiesService.getScriptProperties().getProperty('SHEET_ID'));
    var sh = ss.getSheetByName('Logins');
    if (!sh) { sh = ss.insertSheet('Logins'); sh.appendRow(['When', 'Email', 'Name', 'Role', 'Event']); sh.setFrozenRows(1); }
    sh.appendRow([new Date(), email, name, role, event]);
  } catch (ignored) {}
}

function logSecret() {
  var p = PropertiesService.getScriptProperties();
  var s = p.getProperty('LOG_SECRET');
  if (!s) { s = Utilities.getUuid() + Utilities.getUuid(); p.setProperty('LOG_SECRET', s); }
  return s;
}

function makeSid(email, name, role) {
  var payload = JSON.stringify({ e: email, n: name, r: role, x: Date.now() + SESSION_HOURS * 3600 * 1000 });
  var b = Utilities.base64Encode(payload, Utilities.Charset.UTF_8);
  return b + '.' + Utilities.base64Encode(Utilities.computeHmacSha256Signature(b, logSecret()));
}

function readSid(sid) {
  sid = String(sid || '');
  var dot = sid.indexOf('.');
  if (dot === -1) return null;
  var b = sid.slice(0, dot), mac = sid.slice(dot + 1);
  if (mac !== Utilities.base64Encode(Utilities.computeHmacSha256Signature(b, logSecret()))) return null;
  var payload;
  try { payload = JSON.parse(Utilities.newBlob(Utilities.base64Decode(b)).getDataAsString('UTF-8')); } catch (e) { return null; }
  if (!payload || Number(payload.x) < Date.now()) return null;
  return payload;
}

/** Run once from the editor to sanity-check key derivation against the Python side:
 *  python -c "from sdp.bundle import bundle_id,key_b64; print(bundle_id('all'), key_b64('<secret>','all'))"   */
function testDerivation() {
  var b = bundleFor(PropertiesService.getScriptProperties().getProperty('PORTAL_SECRET'), 'all');
  Logger.log(b.id + ' ' + b.key);
}
