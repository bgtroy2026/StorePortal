/* Seed the portal's Tripleseat history from an "Event Details" report export — or a LEADS report export.
 *
 * WHY. The webhook route (sdp/tripleseat.py, route 2) only ever sees an event once someone touches it, and the
 * public key cannot read events at all. This reshapes a report export into the same rows the webhook would have
 * produced and posts them to the same Apps Script, so the warehouse loads them through the same code path
 * (sdp/transform.py load_tripleseat_webhooks) with source='seed' — until a live delivery replaces the row.
 * First run 2026-09-22: 2,956 events, Aug 2025 to Dec 2027, seven POSTs, a minute. Re-run it whenever a gap
 * opens (the webhook was off for a while, the tab was lost, an older year is wanted): rows apply newest-state
 * first, so re-seeding can never roll a live update back (see _hook_effective in sdp/transform.py).
 *
 * HOW (all in the browser, nothing leaves it but the rows, nothing is downloaded to disk):
 *   1. In Tripleseat: Reports → Events → "Event Details". Locations: all. Status: Prospect, Tentative, Definite,
 *      Closed (leave Lost out unless you want lost business in the portal — it would show as LOST). Date range:
 *      custom, from the first month you want through the furthest booked date. Export → CSV.
 *      Columns the tool reads (the report's defaults plus the money fields): Event Id, Name, Status, Date,
 *      Start Time, End Time, Date Created, Definite/Tentative/Lost/Closed Date, Deleted At, Location, Event
 *      Style, Rooms, Guests, Guar. Guests, Type, Source, Lead Id, Lead Form, Referred By, Market Segment,
 *      Booking Id/Name/Status, Event F&B Min, Deposit, Event Actual, Event Grand Total, Amount Due, Rental Fee,
 *      Price Per Person. Missing columns just leave those fields empty.
 *   2. Open Reports → History (biggrovebrewery.tripleseat.com/reports/history) once the export says Complete.
 *   3. Open the browser console on that page, paste this whole file, press Return. It asks for the public key
 *      (Settings → Tripleseat API — the catalog resolves room and event-type ids) and the webhook token (run
 *      showTripleseatWebhookUrl() in the Apps Script editor; the part after `?hook=`). Neither is stored.
 *   4. It fetches the newest completed Event report from the History table, builds the rows, and prints a
 *      DRY RUN summary: rows per location, rooms or types it could not resolve, the first row. Nothing is sent.
 *   5. If the summary looks right: `await TS_SEED.post()` sends the rows in batches of 400 and prints each
 *      answer ({ok, appended, total}). Then run the workflow with source = tripleseat (backfill = true if you
 *      are re-seeding over rows the warehouse already holds).
 *
 * LEADS. The same tool seeds lead history when the newest completed export on Reports → History is a leads report
 * (a "Lead Id" column and no "Event Id" column): run Reports → Leads → Lead Details with a Created-date range and
 * every location, export to CSV, then paste this file exactly as above. Rows go up as SEED_LEAD and load into
 * ts_leads (name, company, taproom, status, created / event date, guests, type, style, source, lead form,
 * converted / turned-down dates). Email, phone and free-text columns are never read, let alone posted.
 *
 * Contacts, emails and phone numbers are not in the events report; the Apps Script strips them anyway (scrubPii).
 */
(async () => {
  const EXEC = 'https://script.google.com/macros/s/AKfycbzaXWDdxKjR6s9n2AqDB2lePy2Gg4GxTMCmlS3bKhtTfev539hVoooSeBeC5xuFv2PBNQ/exec';
  const TZ = 'America/Chicago';                 // Tripleseat shows event times in the account's time zone
  const BATCH = 400;                            // the script accepts up to 500 rows per POST (SEED_MAX_ROWS)
  const API = 'https://api.tripleseat.com/v1';

  const publicKey = (prompt('Tripleseat public key (Settings → Tripleseat API):') || '').trim();
  const token = (prompt('Webhook token (the part after ?hook= from showTripleseatWebhookUrl()):') || '').trim();
  if (!publicKey || !token) throw new Error('both the public key and the webhook token are needed');

  // ---- catalog: location names, rooms and event types -> ids -------------------------------------------
  const norm = (s) => String(s || '').trim().toLowerCase();
  const locs = await (await fetch(`${API}/locations.json?public_key=${publicKey}`, { headers: { Accept: 'application/json' } })).json();
  const sites = await (await fetch(`${API}/sites.json?public_key=${publicKey}`, { headers: { Accept: 'application/json' } })).json();
  const cat = { locByName: {}, locName: {}, rooms: {}, types: {} };
  for (const w of locs) {
    const L = w.location || w;
    cat.locByName[norm(L.name)] = L.id;
    cat.locName[L.id] = L.name;
    for (const r of L.rooms || []) cat.rooms[`${L.id}|${norm(r.name)}`] = r.id;   // the list is already flat
  }
  for (const w of sites) {
    const S = w.site || w;
    for (const t of S.event_types || []) cat.types[norm(t.name)] = t.id;
  }

  // ---- the export: newest completed "Event report" on Reports -> History -------------------------------
  let link = null, createdText = '', reportType = '';
  for (const tr of document.querySelectorAll('table tr')) {
    const cells = [...tr.querySelectorAll('td')].map((td) => td.textContent.trim());
    const a = tr.querySelector('a[href*="download_report_export"]');
    if (a && /report/i.test(cells[0] || '') && /Complete/i.test(cells[2] || '')) { link = a.href; createdText = cells[1] || ''; reportType = cells[0]; break; }
  }
  if (!link) throw new Error('no completed report export on this page — run the export first, then open Reports → History');
  console.log('Using the newest completed export:', reportType, createdText.replace(/\s+/g, ' '));
  const csv = await (await fetch(link, { credentials: 'include' })).text();

  // "Mon, Sep 21, 2026 7:40 pm" in the account's zone -> when the snapshot was true (ISO UTC)
  const M = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
  const cm = /([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap]m)/i.exec(createdText.replace(/\s+/g, ' '));
  const exportedAt = cm ? localToUtc(+cm[3], M[cm[1].toLowerCase()], +cm[2], (+cm[4] % 12) + (/pm/i.test(cm[6]) ? 12 : 0), +cm[5]) : new Date();
  const exportDay = fmtDay(exportedAt);

  // ---- CSV -> rows ---------------------------------------------------------------------------------------
  const table = parseCsv(csv);
  const header = table.shift().map((h) => h.trim());
  const col = {};
  header.forEach((h, i) => { col[h in col ? `${h} (2)` : h] = i; });       // second "Name"/"Status" are the booking's
  const get = (row, name) => (col[name] === undefined ? '' : String(row[col[name]] ?? '').trim());
  const getAny = (row, names) => { for (const n of names) { if (col[n] !== undefined) { const v = get(row, n); if (v !== '') return v; } } return ''; };
  // Which report is this? The events report leads with "Event Id"; the Lead Details report leads with "Id" and is
  // the only one with a "Submitted" column. A leads export may also carry an Event Id (the event it converted to),
  // so the first column decides.
  const isLeads = header[0] !== 'Event Id' && (header[0] === 'Id' || header[0] === 'Lead Id' || col['Submitted'] !== undefined);
  if (isLeads) return buildLeads();
  const money = (s) => { const v = String(s || '').replace(/[$,\s]/g, ''); return v === '' ? null : v; };
  const int = (s) => { const v = String(s || '').replace(/[,\s]/g, ''); return v === '' ? null : parseInt(v, 10); };
  const iso = (mdy) => { const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})/.exec(mdy || ''); return m ? `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}` : null; };
  const when = new Date().toISOString();
  const stats = { rows: 0, skipped: 0, byLoc: {}, unmappedLoc: {}, roomMiss: {}, typeMiss: {}, noRoom: 0, multiRoom: 0 };
  const rows = [];
  for (const r of table) {
    const id = parseInt(get(r, 'Event Id'), 10);
    if (!id) { stats.skipped++; continue; }                                    // the "Grand Total" line and blanks
    const locName = get(r, 'Location');
    const locId = cat.locByName[norm(locName)];
    if (!locId) { stats.unmappedLoc[locName] = (stats.unmappedLoc[locName] || 0) + 1; continue; }
    const date = get(r, 'Date'), dateIso = iso(date);
    const status = get(r, 'Status').toUpperCase();
    const roomNames = get(r, 'Rooms').split(',').map((s) => s.trim()).filter(Boolean);
    const rooms = [];
    for (const nm of roomNames) {
      const rid = cat.rooms[`${locId}|${norm(nm)}`];
      if (rid) rooms.push({ id: rid, name: nm, location_id: locId });
      else stats.roomMiss[`${locName} / ${nm}`] = (stats.roomMiss[`${locName} / ${nm}`] || 0) + 1;
    }
    if (!rooms.length) stats.noRoom++; else if (rooms.length > 1) stats.multiRoom++;
    const typeName = get(r, 'Type') || null;
    const typeId = typeName ? cat.types[norm(typeName)] || null : null;
    if (typeName && !typeId) stats.typeMiss[typeName] = (stats.typeMiss[typeName] || 0) + 1;
    const changes = [];
    for (const [st, c] of [['TENTATIVE', 'Tentative Date'], ['DEFINITE', 'Definite Date'], ['LOST', 'Lost Date'], ['CLOSED', 'Closed Date']]) {
      const d = get(r, c);
      if (d) changes.push({ status: st, created_at: d });
    }
    changes.sort((a, b) => (iso(a.created_at) || '').localeCompare(iso(b.created_at) || ''));
    const source = get(r, 'Source');
    const leadId = int(get(r, 'Lead Id'));
    const ev = {
      id, name: get(r, 'Name'), status, location_id: locId, location: { id: locId, name: cat.locName[locId] },
      booking_id: int(get(r, 'Booking Id')), booking: { id: int(get(r, 'Booking Id')), name: get(r, 'Name (2)') || null, status: (get(r, 'Status (2)') || '').toUpperCase() || null },
      event_date: date, event_date_iso8601: dateIso,
      event_start_iso8601: dateIso ? localIso(dateIso, get(r, 'Start Time')) : null,
      event_end_iso8601: dateIso ? localIso(dateIso, get(r, 'End Time')) : null,
      event_style: get(r, 'Event Style') || null, event_type_id: typeId, event_type_name: typeName,
      guest_count: int(get(r, 'Guests')), guaranteed_guest_count: int(get(r, 'Guar. Guests')),
      food_and_beverage_min: money(get(r, 'Event F&B Min')), deposit_amount: money(get(r, 'Deposit')),
      actual_amount: money(get(r, 'Event Actual')), grand_total: money(get(r, 'Event Grand Total')),
      amount_due: money(get(r, 'Amount Due')), rental_fee: money(get(r, 'Rental Fee')), price_per_person: money(get(r, 'Price Per Person')),
      created_at: get(r, 'Date Created') || null, updated_at: null, deleted_at: get(r, 'Deleted At') || null,
      room_ids: rooms.map((x) => x.id), rooms,
      selected_lead_sources: source ? [{ lead_source_name: source }] : [],
      status_changes: changes,
      lead: leadId ? { id: leadId, lead_form: get(r, 'Lead Form') || null } : null,
      referred_by: get(r, 'Referred By') || null, market_segment: get(r, 'Market Segment') || null,
      seeded_from: `Event Details report export ${exportDay}`,
    };
    const wrapper = { webhook_trigger_type: 'SEED_EVENT', message: `Seeded from the Tripleseat Event Details report export of ${exportDay}`,
                      exported_at: exportedAt.toISOString(), event: ev };
    rows.push([when, 'SEED_EVENT', 'event', String(id), String(locId), dateIso || '', status, JSON.stringify(wrapper)]);
    stats.rows++;
    stats.byLoc[cat.locName[locId]] = (stats.byLoc[cat.locName[locId]] || 0) + 1;
  }
  stats.longestJson = Math.max(0, ...rows.map((x) => x[7].length));
  console.log('DRY RUN — nothing sent. Export created', createdText.replace(/\s+/g, ' '), '->', exportedAt.toISOString());
  console.log(JSON.stringify(stats, null, 1));
  console.log('first row:', rows[0]);
  if (Object.keys(stats.roomMiss).length || Object.keys(stats.typeMiss).length || Object.keys(stats.unmappedLoc).length)
    console.warn('Some rooms, types or locations did not resolve (see above). Unresolved rooms are dropped from that event; an unresolved location skips the event.');
  console.log('If this looks right:  await TS_SEED.post()');

  window.TS_SEED = {
    rows, stats, exportedAt,
    async post() {
      const url = `${EXEC}?hook=${token}&seed=1`;
      const log = [];
      for (let i = 0; i < rows.length; i += BATCH) {
        const slice = rows.slice(i, i + BATCH);
        const r = await fetch(url, { method: 'POST', body: JSON.stringify({ rows: slice }), headers: { 'Content-Type': 'text/plain;charset=utf-8' }, redirect: 'follow' });
        let j; try { j = await r.json(); } catch (e) { j = { ok: false, error: `HTTP ${r.status}, not JSON` }; }
        log.push({ from: i, n: slice.length, ...j });
        console.log(`rows ${i}-${i + slice.length - 1}:`, j);
        if (!j.ok) { console.error('stopped — fix and re-run post() from', i); break; }
      }
      return log;
    },
  };

  // ---- a LEADS export -----------------------------------------------------------------------------------
  function buildLeads() {
    const money = (s) => { const v = String(s || '').replace(/[$,\s]/g, ''); return v === '' ? null : v; };
    const int = (s) => { const v = String(s || '').replace(/[^\d-]/g, ''); return v === '' ? null : parseInt(v, 10); };
    const iso = (mdy) => { const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})/.exec(mdy || ''); return m ? `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}` : null; };
    const when = new Date().toISOString();
    const stats = { rows: 0, skipped: 0, byLoc: {}, unmappedLoc: {}, typeMiss: {}, byStatus: {}, columnsUsed: {}, noCreated: 0 };
    const rows = [];
    const NAMES = {
      first: ['First Name', 'First'], last: ['Last Name', 'Last'], name: ['Name', 'Contact', 'Contact Name', 'Lead Name'],
      company: ['Company', 'Company Name', 'Account'], status: ['Status', 'Lead Status'],
      created: ['Date Created', 'Created', 'Created Date', 'Created At', 'Submitted', 'Submitted On', 'Received'],
      eventDate: ['Event Date', 'Date', 'Date of Event'], guests: ['Guests', 'Guest Count', '# Guests', 'Number of Guests'],
      location: ['Location'], type: ['Nature Of Event', 'Event Type', 'Type'], style: ['Event Style', 'Style'],
      source: ['Source', 'Lead Source', 'Referred By'], form: ['Lead Form', 'Form'],
      title: ['Event Description', 'Event Name', 'Event Title', 'Title', 'Booking Description'],
      converted: ['Converted', 'Converted Date', 'Won Date', 'Date Won'],
      lost: ['Turned Down At', 'Turned Down Date', 'Lost Date', 'Date Lost', 'Turned Down'],
      eventId: ['Event Id', 'Converted To'], value: ['Budget', 'Estimated Value', 'Value', 'Amount'],
      segment: ['Market Segment'], referred: ['Referred By'],
    };
    Object.keys(NAMES).forEach((k) => { const hit = NAMES[k].find((n) => col[n] !== undefined); if (hit) stats.columnsUsed[k] = hit; });
    for (const r of table) {
      const id = parseInt(getAny(r, ['Lead Id', 'Id']), 10);
      if (!id) { stats.skipped++; continue; }
      const locName = getAny(r, NAMES.location);
      const locId = cat.locByName[norm(locName)];
      if (!locId) { stats.unmappedLoc[locName] = (stats.unmappedLoc[locName] || 0) + 1; continue; }
      const first = getAny(r, NAMES.first), last = getAny(r, NAMES.last);
      let fullName = getAny(r, NAMES.name);
      if (!first && !last && fullName) fullName = fullName.trim();
      const typeName = getAny(r, NAMES.type) || null;
      const typeId = typeName ? cat.types[norm(typeName)] || null : null;
      if (typeName && !typeId) stats.typeMiss[typeName] = (stats.typeMiss[typeName] || 0) + 1;
      const status = getAny(r, NAMES.status) || null;
      const eventDate = getAny(r, NAMES.eventDate), eventIso = iso(eventDate);
      const source = getAny(r, NAMES.source), form = getAny(r, NAMES.form);
      const lead = {
        id, first_name: first || (fullName ? fullName.split(' ')[0] : null), last_name: last || (fullName ? fullName.split(' ').slice(1).join(' ') : null),
        company: getAny(r, NAMES.company) || null, location_id: locId, location: { id: locId, name: cat.locName[locId] },
        status, event_date: eventDate || null, event_date_iso8601: eventIso, guest_count: int(getAny(r, NAMES.guests)),
        event_type_id: typeId, event_type_name: typeName, event_style: getAny(r, NAMES.style) || null,
        lead_source: source || null, selected_lead_sources: source ? [{ lead_source_name: source }] : [],
        lead_form: form || null, event_name: getAny(r, NAMES.title) || null,
        created_at: getAny(r, NAMES.created) || null, converted_at: getAny(r, NAMES.converted) || null,
        event_date_only: eventIso,
        turned_down_at: getAny(r, NAMES.lost) || null, event_id: int(getAny(r, NAMES.eventId)), budget: money(getAny(r, NAMES.value)),
        market_segment: getAny(r, NAMES.segment) || null, referred_by: getAny(r, NAMES.referred) || null,
        seeded_from: `Leads report export ${exportDay}`,
      };
      const wrapper = { webhook_trigger_type: 'SEED_LEAD', message: `Seeded from the Tripleseat leads report export of ${exportDay}`, exported_at: exportedAt.toISOString(), lead };
      rows.push([when, 'SEED_LEAD', 'lead', String(id), String(locId), eventIso || '', String(status || '').toUpperCase(), JSON.stringify(wrapper)]);
      stats.rows++;
      stats.byLoc[cat.locName[locId]] = (stats.byLoc[cat.locName[locId]] || 0) + 1;
      stats.byStatus[status || '?'] = (stats.byStatus[status || '?'] || 0) + 1;
      if (!lead.created_at) stats.noCreated++;
    }
    stats.longestJson = Math.max(0, ...rows.map((x) => x[7].length));
    console.log('DRY RUN (LEADS) — nothing sent. Export created', createdText.replace(/\s+/g, ' '), '->', exportedAt.toISOString());
    console.log('Columns found:', JSON.stringify(stats.columnsUsed), '\nColumns in the file:', header.join(' | '));
    console.log(JSON.stringify(stats, null, 1));
    console.log('first row:', rows[0]);
    if (!stats.columnsUsed.created) console.warn('No created-date column was recognised — the Incoming Leads chart needs one. Add "Date Created" to the report.');
    if (Object.keys(stats.unmappedLoc).length || Object.keys(stats.typeMiss).length)
      console.warn('Some locations or types did not resolve (see above). An unresolved location skips the lead.');
    console.log('If this looks right:  await TS_SEED.post()');
    window.TS_SEED = { rows, stats, exportedAt, kind: 'leads',
      async post() {
        const url = `${EXEC}?hook=${token}&seed=1`;
        const log = [];
        for (let i = 0; i < rows.length; i += BATCH) {
          const slice = rows.slice(i, i + BATCH);
          const r = await fetch(url, { method: 'POST', body: JSON.stringify({ rows: slice }), headers: { 'Content-Type': 'text/plain;charset=utf-8' }, redirect: 'follow' });
          let j; try { j = await r.json(); } catch (e) { j = { ok: false, error: `HTTP ${r.status}, not JSON` }; }
          log.push({ from: i, n: slice.length, ...j });
          console.log(`rows ${i}-${i + slice.length - 1}:`, j);
          if (!j.ok) { console.error('stopped — fix and re-run post() from', i); break; }
        }
        return log;
      } };
  }

  // ---- helpers -------------------------------------------------------------------------------------------
  function parseCsv(text) {
    const out = [], row = []; let field = '', q = false, i = 0;
    const push = () => { row.push(field); field = ''; };
    const endRow = () => { push(); out.push(row.splice(0)); };
    while (i < text.length) {
      const c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { field += '"'; i += 2; continue; } q = false; i++; continue; }
        field += c; i++; continue;
      }
      if (c === '"') { q = true; i++; continue; }
      if (c === ',') { push(); i++; continue; }
      if (c === '\r') { i++; continue; }
      if (c === '\n') { endRow(); i++; continue; }
      field += c; i++;
    }
    if (field !== '' || row.length) endRow();
    return out.filter((r) => r.length > 1 || (r[0] || '').trim() !== '');
  }
  function offsetMinutes(d) {                                                    // the zone's UTC offset at instant d
    const s = new Intl.DateTimeFormat('en-US', { timeZone: TZ, timeZoneName: 'longOffset' }).formatToParts(d).find((p) => p.type === 'timeZoneName').value;
    const m = /GMT([+-])(\d{1,2})(?::?(\d{2}))?/.exec(s);
    return m ? (m[1] === '-' ? -1 : 1) * (parseInt(m[2], 10) * 60 + parseInt(m[3] || '0', 10)) : 0;
  }
  function localToUtc(y, mo, d, h, mi) {                                          // wall-clock in TZ -> Date
    const guess = Date.UTC(y, mo, d, h, mi);
    const utc = guess - offsetMinutes(new Date(guess)) * 60000;
    return new Date(guess - offsetMinutes(new Date(utc)) * 60000);
  }
  function localIso(dateIso, timeText) {                                          // "5:30 PM" on dateIso -> ISO with the zone's offset
    const m = /^(\d{1,2}):(\d{2})\s*([AP]M)?$/i.exec(timeText || '');
    if (!m) return null;
    let h = parseInt(m[1], 10) % 12; if (/pm/i.test(m[3] || '')) h += 12; if (!m[3]) h = parseInt(m[1], 10);
    const [y, mo, d] = dateIso.split('-').map(Number);
    const off = offsetMinutes(localToUtc(y, mo - 1, d, h, +m[2]));
    const sign = off < 0 ? '-' : '+', a = Math.abs(off);
    return `${dateIso}T${String(h).padStart(2, '0')}:${m[2]}:00${sign}${String(Math.floor(a / 60)).padStart(2, '0')}:${String(a % 60).padStart(2, '0')}`;
  }
  function fmtDay(d) {                                                            // the export's calendar day in TZ
    const p = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(d);
    const g = (t) => p.find((x) => x.type === t).value;
    return `${g('year')}-${g('month')}-${g('day')}`;
  }
})();
