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
 *   {"a":"signin","t":<id token>}  → sign-in → { ok, profile:{...}, bundles:[{label,id,key}], detail:{loc:{id,key}}, sid }
 *                                    `detail` keys are for the per-location drilldown bundles, fetched lazily
 *                                    bundles[0] is the data bundle; leadership/admin also get the "scorecard" bundle
 *   {"a":"ping","s":<sid>}         → log a portal open
 *   {"a":"scorecard","k":<key>}    → the Leadership Scorecard tab, for the nightly pipeline (no user session)
 *   {"a":"usage","s":<sid>}        → portal usage (admins only): who has signed in, how often, and who never has
 */

var ALLOWED_DOMAINS = ['biggrove.com', 'biggrovebrewery.com'];
// Non-secret defaults (Script Properties of the same name override these). PORTAL_SECRET has NO default — it must be a Script Property.
var DEFAULTS = {
  CLIENT_ID: '169287489700-t5en62nibhp9ttn24vfimd7k974dgl9i.apps.googleusercontent.com',   // "BG Sales" web client in the Big Grove Portal GCP project
  SHEET_ID:  '1R_W3gWel6rEdQZguf84b7UDJlz5Etn5V4L6AAq6Kg_Q',                                   // "Store Director Roster" sheet
  SCORECARD_SHEET_ID: '1m8aHNL4kmRyKij8aG_4QhgSFUffn2FLT03pTrEtQpZk',                          // "Leadership Scorecard" workbook
  SCORECARD_TAB: 'All Company Scorecard',
  SITE_URL: 'https://bgtroy2026.github.io/StorePortal'   // read for the current bundle epoch (manifest.json)
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
  if (req.a === 'usage') return doUsage(String(req.s || ''));
  return { ok: false, error: 'unknown action' };
}

/**
 * Serve the Leadership Scorecard workbook to the nightly pipeline.
 *
 * This is the one action with no signed-in user behind it, so it is gated not by a Google identity but by
 * proof of the shared PORTAL_SECRET - the same secret the pipeline already holds to build bundles. It is
 * strictly read-only.
 *
 * EVERY visible tab is returned, not just the headline scorecard: the workbook is the company's own record
 * and the parts that matter are spread across it. Hidden tabs are skipped (hidden usually means working
 * scratch, not something to publish), and each tab is capped - a runaway sheet would otherwise blow past
 * Apps Script's response limit and bloat the encrypted bundle every portal visitor downloads. A capped tab
 * says so, so the portal can show that rather than quietly presenting a partial grid as complete.
 *
 * The primary tab is additionally returned in structured form (owner / metric / goal / weekly cells) so the
 * portal can chart it; the rest are returned as display grids, shown as the sheet formats them.
 */
var SC_MAX_ROWS = 300, SC_MAX_COLS = 60;

function doScorecard(k) {
  var secret = prop('PORTAL_SECRET');
  if (!secret) return { ok: false, error: 'PORTAL_SECRET script property is not set' };
  var want = Utilities.base64Encode(Utilities.computeHmacSha256Signature('scorecard', secret, Utilities.Charset.UTF_8));
  if (!k || k !== want) return { ok: false, error: 'bad key' };

  var id = prop('SCORECARD_SHEET_ID'), primary = prop('SCORECARD_TAB');
  var ss = SpreadsheetApp.openById(id);
  var sheets = ss.getSheets(), tabs = [], structured = null, header = [];

  for (var i = 0; i < sheets.length; i++) {
    var sh = sheets[i];
    if (sh.isSheetHidden()) continue;
    var lastRow = sh.getLastRow(), lastCol = sh.getLastColumn();
    if (!lastRow || !lastCol) continue;                       // chart-only or empty tab
    var nR = Math.min(lastRow, SC_MAX_ROWS), nC = Math.min(lastCol, SC_MAX_COLS);
    var rng = sh.getRange(1, 1, nR, nC), disp = rng.getDisplayValues();

    // Trim fully blank trailing rows so a sheet padded to 1000 rows does not ship 900 empty ones.
    while (disp.length && disp[disp.length - 1].join('') === '') disp.pop();

    tabs.push({ name: sh.getName(), grid: disp, rows: lastRow, cols: lastCol,
                truncated: (lastRow > nR || lastCol > nC) });

    if (sh.getName() === primary) {
      var vals = rng.getValues(), head = disp[0], weeks = [];
      for (var c = 3; c < head.length; c++) if (String(head[c]).trim()) weeks.push({ i: c, label: String(head[c]).trim() });
      var rows = [];
      for (var r = 1; r < vals.length && r < disp.length; r++) {
        var metric = String(disp[r][1] || '').trim();
        if (!metric) continue;                                // spacer rows carry no metric name
        var cells = {};
        for (var w = 0; w < weeks.length; w++) {
          var v = vals[r][weeks[w].i], d = String(disp[r][weeks[w].i] || '').trim();
          if (d === '') continue;                             // leave gaps as gaps rather than zeros
          cells[weeks[w].label] = { v: (typeof v === 'number' ? v : null), d: d };
        }
        rows.push({ owner: String(disp[r][0] || '').trim(), metric: metric,
                    goal: { v: (typeof vals[r][2] === 'number' ? vals[r][2] : null), d: String(disp[r][2] || '').trim() },
                    cells: cells });
      }
      structured = rows; header = weeks.map(function (w) { return w.label; });
    }
  }
  return { ok: true, workbook: ss.getName(), sheet: primary, tabs: tabs,
           header: header, rows: structured || [], fetched_at: new Date().toISOString() };
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
  return { ok: true, profile: { email: email, name: name, role: role, locations: access.locations },
           bundles: access.bundles, detail: access.detail || {}, sid: makeSid(email, name, role) };
}

// ---- which bundle(s) this person may open, and the keys (derived, never stored) --------------------------

function portalAccess(props, role, locs) {
  var secret = prop('PORTAL_SECRET');
  if (!secret) throw new Error('PORTAL_SECRET script property is not set');
  // The scorecard workbook carries exec compensation, per-manager audit scores and company financials, so its
  // key goes ONLY to leadership/admin. Note this is deliberately keyed off the ROLE, not off having the "all"
  // bundle: a director covering several taprooms also receives "all", and must not receive this.
  var leadership = /^(leadership|admin)/i.test(role);
  var extra = leadership ? [bundleFor(secret, 'scorecard')] : [];
  var all = leadership || locs === 'all' || !locs;
  if (all) {
    var every = manifest().locations || [];
    return { locations: 'all', bundles: [bundleFor(secret, 'all')].concat(extra), detail: detailBundles(secret, every) };
  }
  var list = locs.split(',').filter(Boolean);
  // A director covering several taprooms used to receive the "all" bundle, filtered in the browser. That is
  // not scoping: anyone who opens devtools sees every location. They now get one bundle PER location they
  // cover, so the other taprooms' data is absent from what they download rather than merely hidden.
  var locBundles = list.map(function (l) { return bundleFor(secret, 'loc:' + l); });
  return { locations: list.join(','), bundles: locBundles.concat(extra), detail: detailBundles(secret, list) };
}

/**
 * Keys for the per-location DETAIL bundles (count sheets and the like). Handed out at sign-in but kept in
 * their own map rather than in `bundles`, because the portal must not download them to open the front page --
 * they are fetched only when somebody drills in. Same derivation, same epoch, so revocation covers them too.
 */
function detailBundles(secret, locs) {
  var out = {};
  (locs || []).forEach(function (l) {
    var b = bundleFor(secret, 'loc:' + l + ':detail');
    out[l] = { id: b.id, key: b.key };
  });
  return out;
}

/**
 * Current bundle epoch, read from the published manifest so there is ONE source of truth. Configuring it
 * separately here would mean two settings that can drift apart, and the failure mode of drift is silent:
 * keys that decrypt nothing, which looks like a broken portal rather than a misconfiguration.
 * Cached briefly so a burst of sign-ins does not fetch it repeatedly.
 */
function manifest() {
  var cache = CacheService.getScriptCache();
  var hit = cache.get('manifest');
  if (hit) { try { return JSON.parse(hit); } catch (ignored) {} }
  var m = { epoch: '1', locations: [] };
  try {
    var url = prop('SITE_URL');
    if (url) {
      var r = UrlFetchApp.fetch(url.replace(/\/$/, '') + '/data/manifest.json', { muteHttpExceptions: true });
      if (r.getResponseCode() === 200) {
        var j = JSON.parse(r.getContentText());
        m = { epoch: String(j.epoch || '1'), locations: j.locations || [] };
      }
    }
  } catch (ignored) {}
  cache.put('manifest', JSON.stringify(m), 300);
  return m;
}

function currentEpoch() { return manifest().epoch; }

function bundleFor(secret, label) {
  var salted = label + ':' + currentEpoch();
  var dig = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, salted, Utilities.Charset.UTF_8);
  var hex = dig.map(function (b) { return ('0' + (b & 255).toString(16)).slice(-2); }).join('');
  var key = Utilities.base64Encode(Utilities.computeHmacSha256Signature(salted, secret, Utilities.Charset.UTF_8));
  return { label: label, id: hex.slice(0, 16), key: key };
}

// ---- portal-open ping -----------------------------------------------------------------------------------

function doPing(sid) {
  var s = readSid(sid);
  if (!s) return { ok: false, error: 'expired session' };
  logEvent(s.e, s.n, s.r, 'open');
  return { ok: true };
}

/**
 * Portal usage, for admins only.
 *
 * Served live rather than baked into the nightly bundle: usage changes by the hour, and a figure that is up to
 * 24 hours stale would be read as current. It is also the one view whose subject is the team rather than the
 * business, so it is gated on the session's own role -- not on a shared key, and not on the client simply
 * choosing to show a button.
 *
 * The roster is joined in deliberately: who has NEVER signed in is the question worth answering when a portal
 * is new, and a log of events alone can only show you the people who already turned up.
 */
function doUsage(sid) {
  var s = readSid(sid);
  if (!s) return { ok: false, error: 'not signed in' };
  if (!/^admin/i.test(String(s.r || ''))) return { ok: false, error: 'not permitted' };

  var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
  var log = ss.getSheetByName('Logins');
  var by = {}, recent = [];
  if (log && log.getLastRow() > 1) {
    var rows = log.getRange(2, 1, log.getLastRow() - 1, 5).getValues();
    for (var i = 0; i < rows.length; i++) {
      var when = rows[i][0], email = String(rows[i][1] || '').toLowerCase(), ev = String(rows[i][4] || '');
      if (!email) continue;
      var o = by[email] || (by[email] = { email: email, name: rows[i][2], role: rows[i][3], signins: 0, opens: 0, last: null, days: {} });
      if (ev === 'signin') o.signins++; else o.opens++;
      if (when instanceof Date) {
        if (!o.last || when > o.last) o.last = when;
        o.days[Utilities.formatDate(when, Session.getScriptTimeZone(), 'yyyy-MM-dd')] = 1;
      }
    }
    var tail = rows.slice(Math.max(0, rows.length - 40));
    for (var t = tail.length - 1; t >= 0; t--) {
      recent.push([tail[t][0] instanceof Date ? tail[t][0].toISOString() : String(tail[t][0]),
                   String(tail[t][1] || ''), String(tail[t][2] || ''), String(tail[t][4] || '')]);
    }
  }

  var people = [], never = [];
  var rs = rosterSheet(ss);
  if (rs && rs.getLastRow() > 1) {
    var r = rs.getRange(2, 1, rs.getLastRow() - 1, 4).getValues();
    for (var k = 0; k < r.length; k++) {
      var em = String(r[k][0] || '').toLowerCase().trim();
      if (!em) continue;
      var hit = by[em];
      if (hit) {
        people.push({ email: em, name: r[k][1] || hit.name, role: r[k][2], locations: r[k][3],
                      signins: hit.signins, opens: hit.opens, activeDays: Object.keys(hit.days).length,
                      last: hit.last ? hit.last.toISOString() : null });
        delete by[em];
      } else {
        never.push({ email: em, name: r[k][1], role: r[k][2], locations: r[k][3] });
      }
    }
  }
  // Anyone in the log but not on the roster -- removed since, or signed in under a different address.
  for (var left in by) {
    people.push({ email: left, name: by[left].name, role: by[left].role, locations: '(not on roster)',
                  signins: by[left].signins, opens: by[left].opens, activeDays: Object.keys(by[left].days).length,
                  last: by[left].last ? by[left].last.toISOString() : null });
  }
  people.sort(function (a, b) { return String(b.last || '').localeCompare(String(a.last || '')); });
  return { ok: true, people: people, never: never, recent: recent, generated_at: new Date().toISOString() };
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
