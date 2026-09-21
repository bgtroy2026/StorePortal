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
 *   TRIPLESEAT_HOOK_TOKEN — auto-created on first use; the token in the webhook URL. Run showTripleseatWebhookUrl()
 *                    from the editor to print the URL to paste into Tripleseat (Settings → Tripleseat API & Webhooks).
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
 *   {"a":"view","s":<sid>,"p":<page>,"l":<loc>} → log one page view (which pages earn their keep)
 *   {"a":"acks","s":<sid>}         → acknowledgements on digest exceptions, for the locations this person covers
 *   {"a":"ack","s":<sid>,"loc","key","state","note","head"} → acknowledge / resolve / reopen one exception
 *
 *   {"a":"depletions_put","s":<sid>,"rows":[[...]]} → admin uploads the brand x market depletion roll-up
 *   {"a":"depletions_put","k":<hmac>,"rows":[[...]]} → same, unattended from the Mac (DEPLETIONS_PUT_KEY)
 *   {"a":"depletions","k":<key>}  → the same rows, for the nightly pipeline (HMAC of PORTAL_SECRET, like scorecard)
 *
 *   POST <exec URL>?hook=<TRIPLESEAT_HOOK_TOKEN>  (any JSON body) → a Tripleseat webhook delivery, appended to the
 *                                    "Tripleseat" tab. See the TRIPLESEAT WEBHOOKS section for what it keeps and drops.
 *   {"a":"tripleseat","k":<key>,"since":N,"limit":M} → rows N.. of that tab, for the nightly pipeline (HMAC like scorecard)
 *
 * Time-driven (no request): morningTick() runs every 15 minutes once installTriggers() has been run ONCE from
 * the editor. It starts the nightly refresh on GitHub, emails the morning digest when the build lands, and
 * emails an alert if it has not landed by 8 AM Central. See the MORNING section below.
 *
 * Extra Script Properties for that:
 *   GH_TOKEN          — fine-grained GitHub token, repo bgtroy2026/StorePortal, Actions: Read and write. REQUIRED for
 *                       the refresh to start from here. Without it the tick still emails digests and alerts.
 *   GH_TOKEN_EXPIRES  — YYYY-MM-DD, optional; a warning is emailed weekly from 14 days before it.
 *   DIGEST_MODE       — 'preview' (default: every digest goes to ALERT_TO, subject prefixed with who it was FOR)
 *                       or 'live' (each person gets their own).
 *   ALERT_TO          — where alerts and previews go. Defaults to the first Admin on the roster.
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
  try {
    // A Tripleseat webhook delivery names itself in the query string; everything else is the portal protocol.
    if (e && e.parameter && e.parameter.hook) out = doTripleseatHook(e);
    else out = handle(((e && e.postData && e.postData.contents) || '').trim());
  }
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
  if (req.a === 'view') return doView(String(req.s || ''), String(req.p || ''), String(req.l || ''));
  if (req.a === 'acks') return doAcks(String(req.s || ''));
  if (req.a === 'ack') return doAck(String(req.s || ''), req);
  if (req.a === 'depletions') return doDepletions(String(req.k || ''));
  if (req.a === 'depletions_put') return doDepletionsPut(String(req.s || ''), req);
  if (req.a === 'tripleseat') return doTripleseatRows(String(req.k || ''), Number(req.since || 0), Number(req.limit || 0));
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
  var views = { pages: [], by_person: [] };
  try { views = viewSummary(ss); } catch (ignored) {}
  return { ok: true, people: people, never: never, recent: recent, views: views, generated_at: new Date().toISOString() };
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


/** A cell that begins with = + - or @ is a FORMULA to Sheets, and appendRow evaluates it. Everything a signed-in
 *  user can type (a note) or send (a location, a headline) is neutralised before it is written. */
function cellSafe(v) { v = String(v == null ? '' : v); return /^[=+\-@\t\r]/.test(v) ? "'" + v : v; }

// =====================================================================================================
// PAGE VIEWS
// =====================================================================================================
// Sign-ins say who turned up; page views say what they came for. One row per page per session (the portal
// de-duplicates before calling), so this is a record of attention, not a click stream.

function doView(sid, page, loc) {
  var s = readSid(sid);
  if (!s) return { ok: false, error: 'expired session' };
  page = String(page || '').replace(/[^a-z0-9_:-]/gi, '').slice(0, 40);
  if (!page) return { ok: false, error: 'no page' };
  try {
    var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
    var sh = ss.getSheetByName('Views');
    if (!sh) { sh = ss.insertSheet('Views'); sh.appendRow(['When', 'Email', 'Role', 'Page', 'Location']); sh.setFrozenRows(1); }
    sh.appendRow([new Date(), s.e, s.r, page, String(loc || '').toLowerCase().replace(/[^a-z0-9-]/g, '').slice(0, 40)]);
  } catch (ignored) {}
  return { ok: true };
}

// Page totals for the usage view: last 28 days, by page and by person-page.
function viewSummary(ss) {
  var sh = ss.getSheetByName('Views');
  var pages = {}, people = {};
  if (!sh || sh.getLastRow() < 2) return { pages: [], by_person: [] };
  var rows = sh.getRange(2, 1, sh.getLastRow() - 1, 5).getValues();
  var cutoff = Date.now() - 28 * 864e5;
  for (var i = 0; i < rows.length; i++) {
    var w = rows[i][0];
    if (!(w instanceof Date) || w.getTime() < cutoff) continue;
    var pg = String(rows[i][3] || ''), em = String(rows[i][1] || '').toLowerCase();
    var o = pages[pg] || (pages[pg] = { page: pg, views: 0, people: {} });
    o.views++; o.people[em] = 1;
    var k = em + '|' + pg;
    people[k] = (people[k] || 0) + 1;
  }
  var out = [];
  for (var p in pages) out.push({ page: p, views: pages[p].views, people: Object.keys(pages[p].people).length });
  out.sort(function (a, b) { return b.views - a.views; });
  var bp = [];
  for (var k2 in people) { var parts = k2.split('|'); bp.push([parts[0], parts[1], people[k2]]); }
  bp.sort(function (a, b) { return b[2] - a[2]; });
  return { pages: out, by_person: bp.slice(0, 80) };
}

// =====================================================================================================
// EXCEPTION ACKNOWLEDGEMENTS
// =====================================================================================================
// The digest tells a director what needs them. This is how they answer it: acknowledged (I have seen it),
// resolved (it is dealt with, and here is what I did), or reopened. Filed against the rule's STABLE KEY rather
// than its wording, because the sentence changes every night as the numbers move and the key does not.
// Append-only: the latest row per location+key is the current state, and the history is the record of what
// was done about it -- which is the point.

var ACK_STATES = { ack: 1, resolved: 1, open: 1 };

function rosterLocations(ss, email) {
  var rows = rosterSheet(ss).getDataRange().getValues();
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0]).toLowerCase().trim() !== email) continue;
    var role = String(rows[i][2] || '');
    var locs = String(rows[i][3] || '').toLowerCase().replace(/\s+/g, '');
    if (/^(leadership|admin)/i.test(role) || locs === 'all' || !locs) return 'all';
    return locs.split(',').filter(Boolean);
  }
  return [];
}

function ackSheet(ss) {
  var sh = ss.getSheetByName('Exceptions');
  if (!sh) {
    sh = ss.insertSheet('Exceptions');
    sh.appendRow(['When', 'Email', 'Name', 'Location', 'Key', 'State', 'Note', 'Headline']);
    sh.setFrozenRows(1);
  }
  return sh;
}

function doAck(sid, req) {
  var s = readSid(sid);
  if (!s) return { ok: false, error: 'expired session' };
  var loc = String(req.loc || '').toLowerCase().replace(/[^a-z0-9-]/g, '').slice(0, 40);
  var key = String(req.key || '').replace(/[^A-Za-z0-9_: -]/g, '').slice(0, 80);
  var state = String(req.state || '');
  if (!loc || !key || !ACK_STATES[state]) return { ok: false, error: 'bad request' };
  var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
  var mine = rosterLocations(ss, s.e);
  if (mine !== 'all' && mine.indexOf(loc) === -1) return { ok: false, error: 'not your location' };
  var note = String(req.note || '').slice(0, 500), head = String(req.head || '').slice(0, 200);
  ackSheet(ss).appendRow([new Date(), s.e, cellSafe(s.n), loc, cellSafe(key), state, cellSafe(note), cellSafe(head)]);
  return { ok: true, ack: { loc: loc, key: key, state: state, note: note, by: s.n || s.e, at: new Date().toISOString() } };
}

function doAcks(sid) {
  var s = readSid(sid);
  if (!s) return { ok: false, error: 'expired session' };
  var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
  var mine = rosterLocations(ss, s.e);
  var sh = ss.getSheetByName('Exceptions');
  var latest = {}, history = [];
  if (sh && sh.getLastRow() > 1) {
    var rows = sh.getRange(2, 1, sh.getLastRow() - 1, 8).getValues();
    var cutoff = Date.now() - 60 * 864e5;
    for (var i = 0; i < rows.length; i++) {
      var w = rows[i][0], loc = String(rows[i][3] || '');
      if (!(w instanceof Date) || w.getTime() < cutoff) continue;
      if (mine !== 'all' && mine.indexOf(loc) === -1) continue;
      var a = { loc: loc, key: String(rows[i][4] || ''), state: String(rows[i][5] || ''), note: String(rows[i][6] || ''),
                head: String(rows[i][7] || ''), by: String(rows[i][2] || rows[i][1] || ''), at: w.toISOString() };
      latest[loc + '|' + a.key] = a;               // rows are in time order, so the last one wins
      history.push(a);
    }
  }
  var out = [];
  for (var k in latest) out.push(latest[k]);
  return { ok: true, acks: out, history: history.slice(-60).reverse() };
}

// =====================================================================================================
// TRIPLESEAT WEBHOOKS
// =====================================================================================================
// Tripleseat POSTs a JSON body to a target URL whenever an event, lead or booking is created, changed or deleted.
// This web app is that target: <exec URL>?hook=<TRIPLESEAT_HOOK_TOKEN>. Every delivery is appended, as received,
// to a "Tripleseat" tab of the roster workbook, and the nightly pipeline collects the tail of that tab on proof of
// PORTAL_SECRET (action "tripleseat") -- the route the scorecard and depletions already travel. The pipeline, not
// this script, decides what an event means: this end keeps the notification and nothing more.
//
// Why here rather than a proper server: the portal has no server, and this script is already its Google-hosted
// backend. Two things a receiver here cannot do, and what stands in for them:
//  - Read request headers. Tripleseat signs each delivery (X-Signature: t=..,v1=HMAC of the body) but doPost never
//    sees headers, so the signature cannot be checked. The URL carries a random token instead: a delivery without
//    it is dropped unread. The tab is append-only and the pipeline treats it as untrusted input, so the worst a
//    forged delivery could do is add a wrong event, never remove or alter one.
//  - Answer 200 directly. Apps Script answers every POST with a 302 to script.googleusercontent.com, after the body
//    has been recorded. Tripleseat counts deliveries it considers failed and may disable an endpoint after too many,
//    so whether it treats the 302 as a failure is the first thing to read off the Webhooks tab after the first few
//    deliveries. (Enabling an endpoint there resets its failure count.)
//
// Privacy: leads and contacts carry guests' details. Email addresses, phone numbers and postal addresses are
// stripped from every object before the row is written, whatever it is; the portal never shows them and the
// workbook should not hold them either.

var HOOK_HEADER = ['when', 'action', 'kind', 'object_id', 'ts_location_id', 'event_date', 'status', 'json'];
var HOOK_MAX_JSON = 45000;              // a Sheets cell holds 50,000 characters
var HOOK_PAGE_MAX = 300;                // rows per pipeline call; payloads run a few KB each
var HOOK_PII = /email|phone|address|zip_code|ssn|card/i;

function hookToken() {
  var p = PropertiesService.getScriptProperties();
  var t = p.getProperty('TRIPLESEAT_HOOK_TOKEN');
  if (!t) { t = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, ''); p.setProperty('TRIPLESEAT_HOOK_TOKEN', t); }
  return t;
}

/** Run from the editor: prints the URL to paste as the Tripleseat webhook target. The token is created the first
 *  time this runs, so run it once BEFORE adding the webhook, and again only if the token should change. */
function showTripleseatWebhookUrl() {
  var base = '';
  try { base = ScriptApp.getService().getUrl() || ''; } catch (ignored) {}
  if (!base || /\/dev$/.test(base)) base = '<the deployed /exec URL of this web app>';
  var url = base + '?hook=' + hookToken();
  Logger.log('Tripleseat webhook target URL:\n' + url);
  return url;
}

/** Remove anything that looks like a guest's contact detail, at any depth. Arrays keep their shape (an array of
 *  phone numbers becomes an empty array rather than vanishing) so a parser that counts them does not break. */
function scrubPii(v) {
  if (Array.isArray(v)) return v.map(scrubPii);
  if (v && typeof v === 'object') {
    var out = {};
    for (var k in v) {
      if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
      if (HOOK_PII.test(k)) continue;
      out[k] = scrubPii(v[k]);
    }
    return out;
  }
  return v;
}

/** The object inside a delivery and the words that describe it, for the summary columns. Tripleseat's payload shape
 *  is not documented beyond "a JSON payload describing the change", so every plausible shape is accepted and the
 *  raw JSON is kept regardless -- these columns exist so the tab is readable by a person and filterable by the
 *  pipeline, not because anything depends on them. */
function hookSummary(obj) {
  var s = { action: '', kind: '', id: '', loc: '', date: '', status: '' };
  if (!obj || typeof obj !== 'object') return s;
  s.action = String(obj.action || obj.trigger || obj.trigger_action || obj.webhook_action || obj.event_type || obj.type || '').slice(0, 60);
  var kinds = ['event', 'lead', 'booking', 'contact', 'account', 'room', 'document', 'payment', 'guest_room_block'];
  var inner = null;
  // A wrapper ({"action":..,"event":{..}}) has no id of its own; a bare object does. An event carries a nested
  // "contact", so the wrapper keys are only consulted when the top level is not itself a record.
  var bare = obj.id !== undefined && obj.id !== null;
  for (var i = 0; !bare && i < kinds.length; i++) {
    if (obj[kinds[i]] && typeof obj[kinds[i]] === 'object') { s.kind = kinds[i]; inner = obj[kinds[i]]; break; }
  }
  if (!inner && !bare && obj.data && typeof obj.data === 'object') {
    for (var j = 0; j < kinds.length; j++) {
      if (obj.data[kinds[j]] && typeof obj.data[kinds[j]] === 'object') { s.kind = kinds[j]; inner = obj.data[kinds[j]]; break; }
    }
    if (!inner) { inner = obj.data; s.kind = String(obj.object_type || obj.data.type || '').toLowerCase(); }
  }
  if (!inner) {
    inner = obj;
    s.kind = String(obj.object_type || obj.kind || '').toLowerCase();
    if (!s.kind) {
      if (obj.event_date_iso8601 !== undefined || (obj.grand_total !== undefined && obj.event_date !== undefined)) s.kind = 'event';
      else if (obj.lead_form !== undefined || obj.turned_down_at !== undefined || obj.event_description !== undefined) s.kind = 'lead';
      else if (obj.start_date !== undefined && obj.end_date !== undefined) s.kind = 'booking';
    }
  }
  s.id = inner.id != null ? String(inner.id) : '';
  s.loc = inner.location_id != null ? String(inner.location_id) : (inner.location && inner.location.id != null ? String(inner.location.id) : '');
  s.date = String(inner.event_date_iso8601 || inner.event_date || inner.start_date || '').slice(0, 10);
  s.status = String(inner.status || '').slice(0, 40);
  return s;
}

/** A delivery too big for a cell keeps its scalar fields and loses its largest arrays (line items, payments,
 *  documents) until it fits. Each dropped key is recorded so the pipeline can tell a small event from a trimmed one. */
function trimHook(obj) {
  var text = JSON.stringify(obj);
  if (text.length <= HOOK_MAX_JSON) return text;
  // Trim inside the object the delivery is about (the "event" of {"event": {...}}) when that is where the bulk
  // is; otherwise trim the top level itself.
  var host = obj, hostSize = 0;
  for (var k in obj) {
    if (obj[k] && typeof obj[k] === 'object' && !Array.isArray(obj[k])) {
      var sz = JSON.stringify(obj[k]).length;
      if (sz > hostSize && sz > text.length / 2) { host = obj[k]; hostSize = sz; }
    }
  }
  host._trimmed = [];
  for (var guard = 0; guard < 30 && JSON.stringify(obj).length > HOOK_MAX_JSON; guard++) {
    var big = null, size = 0;
    for (var key in host) {
      if (key === '_trimmed' || host[key] == null || typeof host[key] !== 'object') continue;
      var len = JSON.stringify(host[key]).length;
      if (len > size) { size = len; big = key; }
    }
    if (!big) break;
    host._trimmed.push(big + ':' + size);
    host[big] = Array.isArray(host[big]) ? [] : null;
  }
  text = JSON.stringify(obj);
  return text.length > HOOK_MAX_JSON ? text.slice(0, HOOK_MAX_JSON - 20) + '"...TRUNCATED"}' : text;
}

function hookSheet(ss) {
  var sh = ss.getSheetByName('Tripleseat');
  if (!sh) {
    sh = ss.insertSheet('Tripleseat');
    sh.appendRow(HOOK_HEADER); sh.setFrozenRows(1);
    sh.getRange(1, 1, 1, HOOK_HEADER.length).setFontWeight('bold');
    sh.getRange('D:G').setNumberFormat('@');                  // ids and dates stay text; Sheets would otherwise turn them into numbers and dates
  }
  return sh;
}

function doTripleseatHook(e) {
  var want = hookToken();
  var got = String((e && e.parameter && e.parameter.hook) || '');
  if (!got || got.length !== want.length || got !== want) return { ok: false, error: 'bad hook' };
  var body = ((e.postData && e.postData.contents) || '').trim();
  if (!body) return { ok: false, error: 'empty delivery' };
  var obj = null;
  try { obj = JSON.parse(body); } catch (err) { obj = null; }
  var sum = hookSummary(obj);
  var json = obj ? trimHook(scrubPii(obj)) : cellSafe(body.slice(0, HOOK_MAX_JSON));
  var lock = LockService.getScriptLock();
  try { lock.waitLock(10000); } catch (ignored) { /* write anyway: a lost row is worse than an interleaved append */ }
  try {
    hookSheet(SpreadsheetApp.openById(prop('SHEET_ID'))).appendRow([new Date(), cellSafe(sum.action), cellSafe(sum.kind), cellSafe(sum.id),
                                                                    cellSafe(sum.loc), cellSafe(sum.date), cellSafe(sum.status), json]);
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
  return { ok: true };
}

/** Rows [since, since+limit) of the Tripleseat tab for the pipeline, plus the total so it knows when to stop. The
 *  tab is append-only: `since` is a row count the pipeline remembers, so a sort or a deleted row in the tab would
 *  make it skip or repeat notifications. Leave the tab alone. */
function doTripleseatRows(k, since, limit) {
  var secret = prop('PORTAL_SECRET');
  if (!secret) return { ok: false, error: 'PORTAL_SECRET script property is not set' };
  var want = Utilities.base64Encode(Utilities.computeHmacSha256Signature('tripleseat', secret, Utilities.Charset.UTF_8));
  if (!k || k !== want) return { ok: false, error: 'bad key' };
  var sh = SpreadsheetApp.openById(prop('SHEET_ID')).getSheetByName('Tripleseat');
  var total = sh ? Math.max(0, sh.getLastRow() - 1) : 0;
  since = Math.max(0, Math.floor(Number(since) || 0));
  limit = Math.min(HOOK_PAGE_MAX, Math.max(1, Math.floor(Number(limit) || HOOK_PAGE_MAX)));
  if (!sh || since >= total) return { ok: true, header: HOOK_HEADER, rows: [], total: total, fetched_at: new Date().toISOString() };
  var n = Math.min(limit, total - since);
  var vals = sh.getRange(2 + since, 1, n, HOOK_HEADER.length).getValues();
  var rows = vals.map(function (r) {
    return r.map(function (v, i) {
      if (v instanceof Date) return v.toISOString();
      if (i === 7) return String(v == null ? '' : v);              // JSON: verbatim, however Sheets displays it
      return String(v == null ? '' : v);
    });
  });
  return { ok: true, header: HOOK_HEADER, rows: rows, total: total, since: since, fetched_at: new Date().toISOString() };
}

// =====================================================================================================
// MORNING: start the refresh, email the digest, raise the alarm
// =====================================================================================================
// Why this lives here. The refresh used to be started by a scheduled task that needed Troy's Mac, and between
// 15 and 19 Sep 2026 it fired every morning, reported success, and started nothing -- it had no access to the
// machine holding its token. Every one of those days the data arrived on GitHub's own late cron, after lunch.
// Apps Script time triggers run on Google's side with no computer involved, and this project is already the
// portal's backend, so the job moved here.
//
// One function on a 15-minute trigger rather than three timed ones: Apps Script fires "at 5am" anywhere in a
// one-hour window, and the three jobs depend on each other (no digest before the build lands, no alarm if it
// did). A tick that looks at the clock and at what has already happened today is simpler and cannot race.

var REPO = 'bgtroy2026/StorePortal';
var TZ = 'America/Chicago';

/** Run ONCE from the editor (it will ask for permission to send mail and call GitHub). Safe to re-run. */
function installTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { if (t.getHandlerFunction() === 'morningTick') ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('morningTick').timeBased().everyMinutes(15).create();
  var msg = 'morningTick installed (every 15 min). GH_TOKEN: ' + (prop('GH_TOKEN') ? 'set' : 'MISSING - the refresh cannot start from here until it is added') +
            ' | DIGEST_MODE: ' + (prop('DIGEST_MODE') || 'preview') + ' | alerts to: ' + alertTo();
  Logger.log(msg); return msg;
}

function centralNow() {
  var d = new Date();
  return { date: Utilities.formatDate(d, TZ, 'yyyy-MM-dd'), hm: Number(Utilities.formatDate(d, TZ, 'HHmm')), dow: Number(Utilities.formatDate(d, TZ, 'u')) };
}

function alertTo() {
  var to = prop('ALERT_TO');
  if (to) return to;
  try {
    var rows = rosterSheet(SpreadsheetApp.openById(prop('SHEET_ID'))).getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) if (/^admin/i.test(String(rows[i][2] || ''))) return String(rows[i][0]).trim();
  } catch (ignored) {}
  return Session.getEffectiveUser().getEmail();
}

function morningTick() {
  var p = PropertiesService.getScriptProperties();
  var now = centralNow(), notes = [];
  // Each step is isolated: a failure starting the refresh must not stop the alarm that says it never landed.
  function step(name, fn) { try { fn(); } catch (e) { notes.push(name + ' FAILED: ' + e); } }

  // 1. Start the refresh: first tick at or after 5:15 AM Central, once per day.
  step('dispatch', function () {
    if (!(now.hm >= 515 && now.hm < 1200) || p.getProperty('DISPATCHED_ON') === now.date) return;
    var r = dispatchRefresh();
    notes.push('dispatch: ' + r);
    if (/^(started|already)/.test(r)) { p.setProperty('DISPATCHED_ON', now.date); return; }
    if (now.hm >= 600 && p.getProperty('DISPATCH_ALERT_ON') !== now.date) {
      p.setProperty('DISPATCH_ALERT_ON', now.date);
      mail(alertTo(), 'Store Director Portal: the morning refresh could not be started',
           'The 5:15 AM refresh was not started: ' + r + '\n\nGitHub\'s own late schedule will still run it around midday. ' +
           'If this mentions a token, create a new fine-grained token for ' + REPO + ' (Actions: Read and write) and put it in the GH_TOKEN script property.');
    }
  });

  // "Fresh" means built today AND carrying yesterday's trading. A code-only republish at 1 AM is built today and a
  // day stale; it must neither trigger the digest nor silence the alarm.
  var man = null;
  step('manifest', function () { man = siteManifest(); });
  var yesterday = Utilities.formatDate(new Date(Date.now() - 864e5), TZ, 'yyyy-MM-dd');
  var fresh = !!(man && String(man.built_local || '') === now.date && String(man.through || '') >= yesterday);

  // 2. Email the digest once, between 5:30 and noon. The day is marked BEFORE sending: if a send throws halfway
  //    down the roster, the cost is some people missing one email -- not everyone above them receiving it again
  //    every fifteen minutes for the rest of the morning.
  step('digest', function () {
    if (!fresh || now.hm < 530 || now.hm >= 1200 || p.getProperty('DIGEST_SENT_ON') === now.date) return;
    p.setProperty('DIGEST_SENT_ON', now.date);
    notes.push('digest: ' + sendDigests());
  });

  // 3. Raise the alarm if nothing fresh has landed by 8:00.
  step('stale', function () {
    if (fresh || now.hm < 800 || now.hm >= 1300 || p.getProperty('STALE_ALERT_ON') === now.date) return;
    p.setProperty('STALE_ALERT_ON', now.date);
    mail(alertTo(), 'Store Director Portal: no fresh data yet this morning',
         'It is past 8 AM Central and the portal is not showing yesterday. Last build: ' + (man ? man.built_at : 'unknown') +
         ', data through ' + (man ? man.through : 'unknown') + '.\n\nCheck https://github.com/' + REPO + '/actions for a failed or queued run.');
    notes.push('stale alert sent');
  });

  // 4. Token expiry, Mondays from 14 days out.
  step('token', function () {
    var exp = p.getProperty('GH_TOKEN_EXPIRES');
    if (!exp || now.dow !== 1 || now.hm < 800 || now.hm >= 815) return;
    var days = Math.round((new Date(exp + 'T12:00:00Z').getTime() - Date.now()) / 864e5);
    if (days <= 14) mail(alertTo(), 'Store Director Portal: the GitHub token expires in ' + days + ' days',
                         'The token that starts the morning refresh (GH_TOKEN) expires on ' + exp + '. Create a replacement for ' + REPO +
                         ' (Actions: Read and write), paste it into the GH_TOKEN script property, and update GH_TOKEN_EXPIRES. ' +
                         'The deploy token file on the Mac (.storeportal-token) was created at the same time and expires with it.');
  });
  if (notes.length) logEvent('system', 'morningTick', 'Admin', notes.join(' | '));
  return notes.join(' | ') || 'nothing to do';
}

function siteManifest() {
  try {
    var r = UrlFetchApp.fetch(prop('SITE_URL').replace(/\/$/, '') + '/data/manifest.json?t=' + Date.now(), { muteHttpExceptions: true });
    if (r.getResponseCode() !== 200) return null;
    var j = JSON.parse(r.getContentText());
    j.built_local = j.built_at ? Utilities.formatDate(new Date(j.built_at), TZ, 'yyyy-MM-dd') : '';
    return j;
  } catch (e) { return null; }
}

function dispatchRefresh() {
  var tok = prop('GH_TOKEN');
  if (!tok) return 'no GH_TOKEN script property';
  var h = { Authorization: 'Bearer ' + tok, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
  try {
    var runs = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/refresh.yml/runs?per_page=6', { headers: h, muteHttpExceptions: true });
    if (runs.getResponseCode() === 401 || runs.getResponseCode() === 403) return 'token rejected (HTTP ' + runs.getResponseCode() + ')';
    if (runs.getResponseCode() === 200) {
      var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
      var list = JSON.parse(runs.getContentText()).workflow_runs || [];
      for (var i = 0; i < list.length; i++) {
        var run = list[i];
        if (String(run.created_at).slice(0, 10) !== today) continue;
        if (run.status !== 'completed') return 'already running (#' + run.run_number + ')';
        // A code-only republish (build_only) is over in a minute and pulls nothing; it must not count as the
        // day's refresh. A real run takes many minutes.
        var mins = (new Date(run.updated_at).getTime() - new Date(run.run_started_at).getTime()) / 60000;
        if ((run.conclusion === 'success' || run.conclusion === 'failure') && mins > 8) return 'already done (#' + run.run_number + ', ' + run.conclusion + ')';
      }
    }
    var r = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/refresh.yml/dispatches',
      { method: 'post', headers: h, contentType: 'application/json', payload: JSON.stringify({ ref: 'main', inputs: {} }), muteHttpExceptions: true });
    return r.getResponseCode() === 204 ? 'started' : 'GitHub said HTTP ' + r.getResponseCode() + ' ' + r.getContentText().slice(0, 120);
  } catch (e) { return 'error: ' + e; }
}

function mail(to, subject, body, html) {
  var o = { to: to, subject: subject, name: 'Big Grove Store Director Portal' };
  if (html) o.htmlBody = html; else o.body = body;
  if (html && body) o.body = body;
  MailApp.sendEmail(o);
}

/**
 * Email each person their morning digest.
 *
 * The pipeline publishes data/digest.bin beside the bundles: one HTML section per location plus a company
 * roll-up, encrypted with a key derived from PORTAL_SECRET exactly as the bundle keys are (label "digest").
 * Apps Script has no AES, so this file uses the SHA-256 counter-mode stream the Sales Portal's Monday digest
 * already uses -- confidentiality only, which is all a public Pages URL needs.
 *
 * Who gets what comes from the roster: a Director gets the sections for the locations on their row; Leadership
 * and Admin get the roll-up. In 'preview' mode (the default) NOBODY but ALERT_TO receives anything -- each
 * digest is sent there with the intended recipient in the subject, so the content can be judged before a
 * single director is emailed. Set the DIGEST_MODE script property to 'live' to switch over.
 */
function sendDigests() {
  var secret = prop('PORTAL_SECRET');
  if (!secret) return 'no PORTAL_SECRET';
  var b = bundleFor(secret, 'digest');
  var resp = UrlFetchApp.fetch(prop('SITE_URL').replace(/\/$/, '') + '/data/' + b.id + '.bin?t=' + Date.now(), { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) return 'digest file not published (HTTP ' + resp.getResponseCode() + ')';
  var payload = streamDecrypt(resp.getContent(), b.key);
  var live = String(prop('DIGEST_MODE') || 'preview').toLowerCase() === 'live';
  var preview = alertTo();
  var rows = rosterSheet(SpreadsheetApp.openById(prop('SHEET_ID'))).getDataRange().getValues();
  var sent = 0, skipped = [];
  for (var i = 1; i < rows.length; i++) {
    var email = String(rows[i][0] || '').trim(), name = String(rows[i][1] || '').trim();
    var role = String(rows[i][2] || 'Director').trim(), locs = String(rows[i][3] || '').toLowerCase().replace(/\s+/g, '');
    if (!email) continue;
    // The roster is typed by hand. An address outside the company cannot sign in, and must not be emailed figures.
    if (ALLOWED_DOMAINS.indexOf((email.toLowerCase().split('@')[1] || '')) === -1) { skipped.push(email + ' (outside domain)'); continue; }
    if (String(rows[i][4] || '').toLowerCase().trim() === 'no') { skipped.push(email + ' (opted out)'); continue; }   // optional 5th column: digest = no
    var all = /^(leadership|admin)/i.test(role) || locs === 'all' || !locs;
    var html, subject;
    if (all) { html = payload.all && payload.all.html; subject = payload.all && payload.all.subject; }
    else {
      var parts = [], heads = [];
      locs.split(',').filter(Boolean).forEach(function (l) { var sct = payload.locations[l]; if (sct) { parts.push(sct.html); heads.push(sct.subject); } });
      html = parts.join('<div style="height:28px"></div>');
      subject = heads.length === 1 ? heads[0] : ('Morning digest: ' + heads.length + ' taprooms, ' + payload.through_label);
    }
    if (!html) { skipped.push(email + ' (no section)'); continue; }
    html = payload.head + html + payload.foot;
    try {
      mail(live ? email : preview, (live ? '' : '[preview for ' + (name || email) + '] ') + subject, 'Open the portal: ' + prop('SITE_URL'), html);
      sent++;
    } catch (e) { skipped.push(email + ' (send failed: ' + e + ')'); }
  }
  logEvent('system', 'digest', 'Admin', 'digest sent ' + sent + (live ? '' : ' (preview to ' + preview + ')') + (skipped.length ? ' | skipped: ' + skipped.join(', ') : ''));
  return 'sent ' + sent + (live ? '' : ' (preview)');
}

function streamDecrypt(bytes, keyB64) {
  var key = Utilities.base64Decode(keyB64);
  var nonce = bytes.slice(0, 16), ct = bytes.slice(16), out = [], blk = null, bi = 0;
  for (var i = 0; i < ct.length; i++) {
    if (i % 32 === 0) {
      var ctr = [(bi >>> 24) & 255, (bi >>> 16) & 255, (bi >>> 8) & 255, bi & 255].map(function (x) { return (x << 24) >> 24; });
      blk = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, key.concat(nonce, ctr)); bi++;
    }
    out.push(((ct[i] & 255) ^ (blk[i % 32] & 255)) << 24 >> 24);
  }
  return JSON.parse(Utilities.ungzip(Utilities.newBlob(out, 'application/x-gzip', 'digest.json.gz')).getDataAsString('UTF-8'));
}

/** Run from the editor to see exactly what tomorrow's digest would send, without sending it. */
function previewDigestToMe() {
  var p = PropertiesService.getScriptProperties(), was = p.getProperty('DIGEST_MODE');
  p.setProperty('DIGEST_MODE', 'preview');
  try { return sendDigests(); } finally { if (was) p.setProperty('DIGEST_MODE', was); else p.deleteProperty('DIGEST_MODE'); }
}


// =====================================================================================================
// DEPLETIONS: brand x taproom-market case equivalents
// =====================================================================================================
// The roll-up is built on the Mac from the VIP exports (tools/build_depletions.py). It is coarse -- no accounts,
// no prices -- but it is still the company's sales data, and the repository that builds the portal is public. So it
// does not travel through the repository at all. An Admin uploads it from the portal; it rests in a "Depletions"
// tab of the roster workbook; the nightly pipeline collects it exactly as it collects the scorecard, proving
// itself with an HMAC of PORTAL_SECRET. No new credential, no new permission, nothing public.

var DEP_HEADER = ['location_id', 'brand', 'premise', 'ce_ty', 'ce_ly', 'accounts', 'as_of'];

function doDepletionsPut(sid, req) {
  var rows = req.rows, s;
  if (!rows || !rows.length || rows.length > 6000) return { ok: false, error: 'expected 1-6000 rows' };
  if (req.k) {
    // Unattended upload from the Mac that builds the roll-up. The key never travels: the caller signs the
    // as-of date and row count with DEPLETIONS_PUT_KEY. It can write this one tab and nothing else.
    var pk = prop('DEPLETIONS_PUT_KEY');
    if (!pk || String(pk).length < 24) return { ok: false, error: 'machine upload is not set up' };
    var msg = 'depletions_put:' + String((rows[0] || [])[6]) + ':' + rows.length;
    var want = Utilities.base64Encode(Utilities.computeHmacSha256Signature(msg, pk, Utilities.Charset.UTF_8));
    if (String(req.k) !== want) return { ok: false, error: 'bad key' };
    s = { e: 'sales-portal-mac', n: 'Sales Portal refresh', r: 'machine' };
  } else {
    s = readSid(sid);
    if (!s) return { ok: false, error: 'expired session' };
    if (!/^admin/i.test(String(s.r || ''))) return { ok: false, error: 'not permitted' };
  }
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    if (!r || r.length < 7) return { ok: false, error: 'row ' + (i + 1) + ' is short' };
    var loc = String(r[0]).toLowerCase().replace(/[^a-z0-9-]/g, ''), prem = String(r[2]).toUpperCase() === 'ON' ? 'ON' : 'OFF';
    var ty = Number(r[3]), ly = Number(r[4]), acc = Number(r[5]);
    if (!loc || !String(r[1]).trim() || isNaN(ty) || isNaN(ly)) return { ok: false, error: 'row ' + (i + 1) + ' is malformed' };
    out.push([loc, cellSafe(String(r[1]).slice(0, 80)), prem, ty, ly, isNaN(acc) ? 0 : acc, cellSafe(String(r[6]).slice(0, 10))]);
  }
  var ss = SpreadsheetApp.openById(prop('SHEET_ID'));
  var sh = ss.getSheetByName('Depletions') || ss.insertSheet('Depletions');
  sh.clearContents();
  sh.getRange(1, 1, 1, DEP_HEADER.length).setValues([DEP_HEADER]);
  sh.getRange(1, 7, out.length + 1, 1).setNumberFormat('@');          // as_of stays text; Sheets would otherwise turn it into a date
  sh.getRange(1, 2, out.length + 1, 1).setNumberFormat('@');          // so does a brand that looks like a number
  sh.getRange(2, 1, out.length, DEP_HEADER.length).setValues(out);
  sh.setFrozenRows(1);
  logEvent(s.e, s.n, s.r, 'depletions upload: ' + out.length + ' rows, as of ' + out[0][6]);
  return { ok: true, rows: out.length, as_of: out[0][6] };
}

function doDepletions(k) {
  var secret = prop('PORTAL_SECRET');
  if (!secret) return { ok: false, error: 'PORTAL_SECRET script property is not set' };
  var want = Utilities.base64Encode(Utilities.computeHmacSha256Signature('depletions', secret, Utilities.Charset.UTF_8));
  if (!k || k !== want) return { ok: false, error: 'bad key' };
  var sh = SpreadsheetApp.openById(prop('SHEET_ID')).getSheetByName('Depletions');
  if (!sh || sh.getLastRow() < 2) return { ok: true, rows: [] };
  var vals = sh.getRange(2, 1, sh.getLastRow() - 1, DEP_HEADER.length).getDisplayValues();
  return { ok: true, header: DEP_HEADER, rows: vals, fetched_at: new Date().toISOString() };
}
