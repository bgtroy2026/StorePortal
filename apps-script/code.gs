/**
 * Big Grove Store Director Portal — sign-in backend (Google Apps Script)
 *
 * Same design as the Sales Portal backend (Portal Login), deployed as a SEPARATE Apps Script project so the
 * two portals have independent rosters and secrets.
 *
 * Deploy: New deployment → Web app → Execute as: Me → Who has access: Anyone.
 * To UPDATE without changing the URL: Deploy → Manage deployments → ✏️ Edit → Version: New version → Deploy.
 *
 * Script Properties (Project Settings → Script properties):
 *   PORTAL_SECRET  — REQUIRED. EXACTLY the same value as the PORTAL_SECRET GitHub Actions secret. Bundle keys are derived
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
 *   {"a":"scorecard","k":<key>}    → the Leadership Scorecard tab, for the nightly pipeline (no user session)
 */

var ALLOWED_DOMAINS = ['biggrove.com', 'biggrovebrewery.com'];
// Non-secret defaults (Script Properties of the same name override these). PORTAL_SECRET has NO default — it must be a Script Property.
var DEFAULTS = {
  CLIENT_ID: '169287489700-t5en62nibhp9ttn24vfimd7k974dgl9i.apps.googleusercontent.com',   // "BG Sales" web client in the Big Grove Portal GCP project
  SHEET_ID:  '1R_W3gWel6rEdQZguf84b7UDJlz5Etn5V4L6AAq6Kg_Q',                                   // "Store Director Roster" sheet
  SCORECARD_SHEET_ID: '1m8aHNL4kmRyKij8aG_4QhgSFUffn2FLT03pTrEtQpZk',                          // "Leadership Scorecard" workbook
  SCORECARD_TAB: 'All Company Scorecard'
};
function prop(name) { return PropertiesService.getScriptProperties().getProperty(name) || DEFAULTS[name] || null; }
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
  if (req.a === 'scorecard') return doScorecard(String(req.k || ''));
  return { ok: false, error: 'unknown action' };
}

/**
 * Serve the Leadership Scorecard tab to the nightly pipeline.
 *
 * This is the one action with no signed-in user behind it, so it is not gated by a Google identity but
 * by proof of the shared PORTAL_SECRET — the same secret the pipeline already holds to build bundles.
 * It is strictly read-only and returns a single named tab, so a leaked key exposes no more than that tab.
 *
 * Both raw and displayed values are returned: the raw ones are what charts and comparisons need, while
 * the displayed ones preserve the sheet's own currency/percent formatting so the portal can show a figure
 * exactly as leadership is used to reading it.
 */
function doScorecard(k) {
  var secret = prop('PORTAL_SECRET');
  if (!secret) return { ok: false, error: 'PORTAL_SECRET script property is not set' };
  var want = Utilities.base64Encode(Utilities.computeHmacSha256Signature('scorecard', secret, Utilities.Charset.UTF_8));
  if (!k || k !== want) return { ok: false, error: 'bad key' };

  var id = prop('SCORECARD_SHEET_ID'), name = prop('SCORECARD_TAB');
  var ss = SpreadsheetApp.openById(id);
  var sh = ss.getSheetByName(name);
  if (!sh) return { ok: false, error: 'no tab named ' + name + ' in ' + ss.getName() };

  var rng = sh.getDataRange(), vals = rng.getValues(), disp = rng.getDisplayValues();
  if (!vals.length) return { ok: false, error: 'tab ' + name + ' is empty' };

  // Row 1 is the header: columns A-C are owner / metric / goal, everything after is a week.
  var head = disp[0], weeks = [];
  for (var c = 3; c < head.length; c++) if (String(head[c]).trim()) weeks.push({ i: c, label: String(head[c]).trim() });

  var rows = [];
  for (var r = 1; r < vals.length; r++) {
    var metric = String(disp[r][1] || '').trim();
    if (!metric) continue;                                  // spacer rows carry no metric name
    var cells = {};
    for (var w = 0; w < weeks.length; w++) {
      var v = vals[r][weeks[w].i], d = String(disp[r][weeks[w].i] || '').trim();
      if (d === '') continue;                               // leave gaps as gaps rather than zeros
      cells[weeks[w].label] = { v: (typeof v === 'number' ? v : null), d: d };
    }
    rows.push({ owner: String(disp[r][0] || '').trim(), metric: metric,
                goal: { v: (typeof vals[r][2] === 'number' ? vals[r][2] : null), d: String(disp[r][2] || '').trim() },
                cells: cells });
  }
  return { ok: true, sheet: name, workbook: ss.getName(), header: weeks.map(function (w) { return w.label; }),
           rows: rows, fetched_at: new Date().toISOString() };
}

function doSignin(token) {
  if (!token) return { ok: false, error: 'no token' };
  var props = PropertiesService.getScriptProperties();
  var resp = UrlFetchApp.fetch('https://oauth2.googleapis.com/tokeninfo', { method: 'post', payload: { id_token: token }, muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) return { ok: false, error: 'invalid token' };
  var info = JSON.parse(resp.getContentText());
  if (info.aud !== prop('CLIENT_ID')) return { ok: false, error: 'wrong app' };
  if (Number(info.exp) * 1000 < Date.now()) return { ok: false, error: 'expired token' };
  if (String(info.email_verified) !== 'true') return { ok: false, error: 'unverified email' };

  var email = String(info.email || '').toLowerCase().trim();
  var domain = email.split('@')[1] || '';
  if (ALLOWED_DOMAINS.indexOf(domain) === -1) return { ok: false, error: 'outside domain' };

  var rows = rosterSheet(SpreadsheetApp.openById(prop('SHEET_ID'))).getDataRange().getValues();
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
  var secret = prop('PORTAL_SECRET');
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
    var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
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

/** Run ONCE from the editor: writes the roster header row + Troy as Admin if the sheet is empty, and reports which
 *  settings are in place. Also triggers the authorization prompt for Sheets/UrlFetch scopes. */
function setupRoster() {
  var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
  var sh = ss.getSheets()[0];
  if (String(sh.getRange(1, 1).getValue()).toLowerCase().trim() !== 'email') {
    sh.setName('Roster');
    sh.getRange(1, 1, 2, 4).setValues([['email', 'name', 'role', 'locations'], ['troy@biggrovebrewery.com', 'Troy Myler', 'Admin', 'all']]);
    sh.getRange(1, 1, 1, 4).setFontWeight('bold'); sh.setFrozenRows(1); sh.setColumnWidths(1, 4, 220);
  }
  var msg = 'Roster sheet OK: ' + ss.getName() + '\nCLIENT_ID: ' + prop('CLIENT_ID') + '\nPORTAL_SECRET: ' + (prop('PORTAL_SECRET') ? 'set' : 'MISSING - add it under Project Settings > Script properties');
  Logger.log(msg); return msg;
}

/** Run once from the editor to sanity-check key derivation against the Python side:
 *  python -c "from sdp.bundle import bundle_id,key_b64; print(bundle_id('all'), key_b64('<secret>','all'))"   */
function testDerivation() {
  var b = bundleFor(prop('PORTAL_SECRET'), 'all');
  Logger.log(b.id + ' ' + b.key);
}
