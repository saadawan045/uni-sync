/* uni-sync phone app. Reads your "University" Google Calendar (the one uni-sync fills) straight
   from Google with your own sign-in. No server: the data goes from Google to this page only. */
'use strict';

const CFG = window.UNISYNC_CONFIG || {};
const SCOPE = 'https://www.googleapis.com/auth/calendar.app.created';
const API = 'https://www.googleapis.com/calendar/v3';
const FEED_ID = 'usfeed0001';
const DUE_KINDS = new Set(['quiz', 'exam', 'assignment', 'deadline', 'presentation', 'commitment']);
const PLAN_KINDS = new Set(['social', 'appointment', 'birthday', 'commitment', 'meeting', 'event']);

/* ------------------------------------------------------------------ storage */
const store = {
  get(k, d) { try { const v = localStorage.getItem('us.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('us.' + k, JSON.stringify(v)); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem('us.' + k); } catch { /* ignore */ } },
};

const S = {
  mode: store.get('mode', null),          // 'live' | 'demo' | null
  calId: store.get('calId', ''),
  token: null, tokenExp: 0,
  data: store.get('cache', null),         // { events: [...raw], feed: {...}, fetchedAt }
  dueTab: 'open', msg: null, err: null, busy: false, needCal: false,
};
let tokenClient = null;

/* ------------------------------------------------------------------ dom helper */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') continue;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
}
const svg = (paths, cls) => {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true'); if (cls) s.setAttribute('class', cls);
  s.innerHTML = paths; // constant markup only
  return s;
};

/* ------------------------------------------------------------------ time */
const pad = (n) => String(n).padStart(2, '0');
const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const FULLDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const dayDiff = (a, b) => Math.round((startOfDay(a) - startOfDay(b)) / 864e5);
function rel(ms) {
  const m = Math.round(Math.abs(ms) / 6e4);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min`;
  const hrs = Math.round(m / 60);
  if (hrs < 36) return `${hrs} h`;
  return `${Math.round(hrs / 24)} days`;
}
function ago(t) { return t ? (Date.now() - t < 6e4 ? 'just now' : rel(Date.now() - t) + ' ago') : 'never'; }
function parseDay(s) { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); }

/* ------------------------------------------------------------------ events */
const clean = (s) => (s || '').replace(/^[^\p{L}\p{N}]+/u, '').replace(/^LATE\s*[:·-]?\s*/i, '').trim();
function norm(e) {
  const p = (e.extendedProperties || {}).private || {};
  const allDay = !!(e.start && e.start.date);
  const start = allDay ? parseDay(e.start.date) : new Date(e.start.dateTime);
  const end = allDay ? parseDay(e.end.date) : new Date(e.end.dateTime);
  const raw = e.summary || '';
  return {
    id: e.id, raw, title: clean(raw) || 'Untitled', kind: p.kind || 'other', source: p.source || 'manual',
    location: e.location || '', desc: e.description || '', start, end, allDay,
    done: raw.startsWith('✅'), late: /LATE/.test(raw), feed: p.unisync === 'feed',
  };
}
function chip(ev) {
  const k = ev.kind;
  if (k === 'quiz') return ['k-test', 'Quiz'];
  if (k === 'exam') return ['k-test', 'Exam'];
  if (k === 'assignment' || k === 'deadline') return ['k-due', 'Due'];
  if (k === 'presentation') return ['k-test', 'Present'];
  if (PLAN_KINDS.has(k)) return ['k-plan', 'Plan'];
  if (k === 'class') return /\blab\b/i.test(ev.title) ? ['k-lab', 'Lab'] : ['k-class', 'Class'];
  return ['k-other', k === 'other' ? 'Event' : k[0].toUpperCase() + k.slice(1)];
}
const SRC = {
  canvas: ['CV', '#3A2219', '#FFB38A'], webwork: ['WW', '#14302A', '#7FE0C2'], whatsapp: ['WA', '#1C2540', '#A9C1FF'],
  email: ['EM', '#2B2140', '#D7B8FF'], portal: ['NIT', '#262B35', '#C2C8D3'], manual: ['ME', '#262B35', '#C2C8D3'],
};
function quoteOf(desc) {
  const m = /“([^”]+)”/.exec(desc || '');
  return m ? m[1] : '';
}

function events() {
  return ((S.data && S.data.events) || []).map(norm).filter((e) => !e.feed && e.raw)
    .sort((a, b) => a.start - b.start);
}
function feedData() { return (S.data && S.data.feed) || { items: [], resolved: [], updated: 0 }; }

/* ------------------------------------------------------------------ Google */
function tokenValid() { return S.token && Date.now() < S.tokenExp - 60e3; }
window.gsiReady = () => {
  if (!CFG.clientId || !window.google || !google.accounts || !google.accounts.oauth2) return;
  tokenClient = google.accounts.oauth2.initTokenClient({
    client_id: CFG.clientId, scope: SCOPE,
    callback: (r) => {
      if (r.error) { S.err = 'Google sign-in didn’t finish. Try again.'; render(); return; }
      S.token = r.access_token; S.tokenExp = Date.now() + (r.expires_in || 3600) * 1000;
      store.set('consented', true);
      if (S.mode !== 'live') { S.mode = 'live'; store.set('mode', 'live'); }
      refresh();
    },
    error_callback: () => { S.err = 'The Google sign-in window was closed or blocked.'; render(); },
  });
  render();
};
function signIn() {
  if (!tokenClient) { S.err = 'Google sign-in is still loading — try again in a second.'; render(); return; }
  S.err = null;
  tokenClient.requestAccessToken({ prompt: store.get('consented', false) ? '' : 'consent' });
}
async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    ...opts, headers: { Authorization: 'Bearer ' + S.token, 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (r.status === 401) { S.token = null; throw new Error('signin'); }
  if (!r.ok && !(opts.okStatus || []).includes(r.status)) throw new Error(`Google said ${r.status}`);
  return r.status === 204 ? null : r.json().catch(() => null);
}
async function findCalendar() {
  if (S.calId) return true;
  try {
    const res = await api('/users/me/calendarList?minAccessRole=owner&maxResults=250');
    const hit = (res.items || []).find((c) => (c.summary || '').trim().toLowerCase() === 'university');
    if (hit) { S.calId = hit.id; store.set('calId', hit.id); return true; }
  } catch (e) { if (e.message === 'signin') throw e; }
  return false;
}
async function refresh() {
  if (S.mode === 'demo') { S.data = demoData(); render(); return; }
  if (!tokenValid()) { signIn(); return; }
  S.busy = true; S.err = null; render();
  try {
    if (!(await findCalendar())) { S.needCal = true; S.busy = false; render(); return; }
    S.needCal = false;
    const cal = encodeURIComponent(S.calId);
    const from = addDays(startOfDay(new Date()), -1).toISOString();
    const to = addDays(startOfDay(new Date()), 31).toISOString();
    const [list, feedEv] = await Promise.all([
      api(`/calendars/${cal}/events?singleEvents=true&orderBy=startTime&maxResults=250&timeMin=${from}&timeMax=${to}`),
      api(`/calendars/${cal}/events/${FEED_ID}`, { okStatus: [404, 410] }),
    ]);
    let feed = { items: [], resolved: [], updated: 0 };
    try { if (feedEv && feedEv.description) feed = JSON.parse(feedEv.description); } catch { /* keep empty */ }
    S.data = { events: (list.items || []).filter((e) => e.status !== 'cancelled'), feed, fetchedAt: Date.now() };
    store.set('cache', S.data);
  } catch (e) {
    S.err = e.message === 'signin' ? null : 'Couldn’t reach Google. Showing what was saved on this phone.';
    if (e.message !== 'signin' && /404/.test(e.message)) { S.calId = ''; store.del('calId'); }
  }
  S.busy = false; render();
}
async function resolveCheck(item, add) {
  if (S.mode === 'demo') {
    S.data.feed.resolved.push(item.id);
    S.msg = add ? `Added “${item.title.split(' — ')[0]}” to your calendar.` : 'Ignored.';
    render(); return;
  }
  if (!tokenValid()) { signIn(); return; }
  const cal = encodeURIComponent(S.calId);
  try {
    if (add && item.event) await api(`/calendars/${cal}/events`, { method: 'POST', body: JSON.stringify(item.event), okStatus: [409] });
    const cur = await api(`/calendars/${cal}/events/${FEED_ID}`);
    const f = JSON.parse(cur.description || '{}');
    f.resolved = [...new Set([...(f.resolved || []), item.id])];
    await api(`/calendars/${cal}/events/${FEED_ID}`, { method: 'PATCH', body: JSON.stringify({ description: JSON.stringify(f) }) });
    S.msg = add ? 'Added to your calendar.' : 'Ignored. It won’t show again.';
    await refresh();
  } catch (e) {
    S.err = e.message === 'signin' ? 'Sign in again, then retry.' : 'Couldn’t save that. Check your connection and try again.';
    render();
  }
}
function signOut() {
  if (S.token && window.google) google.accounts.oauth2.revoke(S.token, () => {});
  for (const k of ['mode', 'calId', 'cache', 'consented']) store.del(k);
  Object.assign(S, { mode: null, calId: '', token: null, data: null, msg: null, err: null });
  location.hash = '#today'; render();
}

/* ------------------------------------------------------------------ views */
function syncPill() {
  if (S.mode === 'demo') return h('button', { class: 'pill', type: 'button', onclick: () => refresh() }, h('span', { class: 'dot' }), 'Demo data');
  const f = feedData();
  const laptop = f.updated ? f.updated * 1000 : 0;
  const stale = !laptop || Date.now() - laptop > 3 * 3600e3;
  const label = S.busy ? 'Syncing…' : !tokenValid() ? 'Tap to refresh' : laptop ? `Synced ${ago(laptop)}` : 'Up to date';
  return h('button', { class: 'pill', type: 'button', onclick: () => refresh(),
    'aria-label': `${label}. ${laptop ? 'Laptop last synced ' + ago(laptop) : ''}. Refresh` },
  h('span', { class: 'dot' + (stale && !S.busy ? ' stale' : '') }), label);
}
function header(eyebrow, title) {
  return h('header', { class: 'top' }, h('div', {}, h('div', { class: 'eyebrow' }, eyebrow), h('h1', {}, title)), syncPill());
}
function notices() {
  const out = [];
  if (S.mode === 'demo') out.push(h('div', { class: 'banner' }, 'You’re looking at sample data. ',
    h('a', { href: '#', onclick: (e) => { e.preventDefault(); signOut(); } }, 'Use your own calendar')));
  if (S.err) out.push(h('div', { class: 'error', role: 'alert' }, S.err));
  if (S.msg) { out.push(h('div', { class: 'status', role: 'status' }, S.msg)); setTimeout(() => { S.msg = null; }, 50); }
  return out;
}

function viewToday() {
  const now = new Date();
  const evs = events();
  const today = evs.filter((e) => (e.allDay ? e.start <= startOfDay(now) && e.end > startOfDay(now) : dayDiff(e.start, now) === 0));
  const timed = evs.filter((e) => !e.allDay && e.end > now && !e.done);
  const next = timed[0];
  const soonTest = evs.find((e) => (e.kind === 'quiz' || e.kind === 'exam') && e.start > now
    && e.start - now < 48 * 3600e3 && e !== next && !e.done);
  const body = [header(`${FULLDAYS[now.getDay()]} · ${now.getDate()} ${MONTHS[now.getMonth()]}`, 'Today'), ...notices()];

  if (next) {
    const live = next.start <= now;
    const dd = dayDiff(next.start, now);
    const label = live ? `Now · ends ${hm(next.end)}` : dd === 0 ? `Next up · in ${rel(next.start - now)}`
      : `Next up · ${dd === 1 ? 'tomorrow' : DAYS[next.start.getDay()]} ${hm(next.start)}`;
    const total = next.end - next.start;
    const pct = live ? Math.min(100, Math.round((now - next.start) / total * 100)) : 0;
    const [cc, cl] = chip(next);
    body.push(h('section', { class: 'card next', 'aria-label': 'Next up' },
      h('div', { class: 'row' }, h('span', { class: 'label' }, label), h('span', { class: 'chip ' + cc }, cl)),
      h('div', {}, h('h3', {}, next.title),
        h('div', { class: 'when' }, `${hm(next.start)} – ${hm(next.end)}${next.location ? ' · ' + next.location : ''}`)),
      live ? h('div', { class: 'bar', role: 'progressbar', 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-label': 'Time elapsed' },
        h('i', { style: `width:${pct}%` })) : null));
  }
  if (soonTest) {
    const dd = dayDiff(soonTest.start, now);
    const q = quoteOf(soonTest.desc);
    body.push(h('a', { class: 'alert', href: '#due' },
      h('span', { class: 'ico' }, svg('<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>')),
      h('span', { style: 'flex:1;min-width:0' },
        h('b', {}, `${soonTest.title} ${dd === 0 ? 'today' : 'tomorrow'}${soonTest.allDay ? '' : ', ' + hm(soonTest.start)}`),
        h('span', { class: 's' }, q ? `“${q.slice(0, 70)}”` : ''))));
  }

  body.push(h('h2', { class: 'sec' }, 'Schedule'));
  if (!today.length) body.push(h('div', { class: 'empty' }, S.data ? 'Nothing on your calendar today.' : 'Pull to refresh once you’re signed in.'));
  else body.push(h('div', { class: 'list' }, today.map((e) => {
    const [cc, cl] = chip(e);
    const meta = [e.location, e.done ? 'done' : '', e.source === 'whatsapp' ? 'from WhatsApp' : ''].filter(Boolean).join(' · ');
    return h('div', { class: 'slot' + (!e.allDay && e.end < now || e.done ? ' done' : '') },
      h('div', { class: 't' }, h('b', {}, e.allDay ? 'All day' : hm(e.start)), e.allDay ? null : h('span', {}, hm(e.end))),
      h('div', { class: 'item' }, h('span', { class: 'txt' }, h('span', { class: 'ttl' }, e.title), meta ? h('span', { class: 'meta' }, meta) : null),
        h('span', { class: 'chip ' + cc }, cl)));
  })));
  return body;
}

function dueCard(e, now) {
  const [b, bg, fg] = SRC[e.source] || SRC.manual;
  const dd = dayDiff(e.start, now);
  let when = e.allDay ? DAYS[e.start.getDay()] : (dd === 0 ? hm(e.start) : `${DAYS[e.start.getDay()]} ${hm(e.start)}`);
  let sub = dd === 0 ? 'today' : dd === 1 ? 'tomorrow' : dd > 1 ? `in ${dd} days` : `${-dd} days ago`;
  if (!e.allDay && e.start > now && e.start - now < 36 * 3600e3) sub = `in ${rel(e.start - now)}`;
  if (e.done) { when = 'Done'; sub = ''; }
  if (e.late) { when = 'Late'; sub = 'still open'; }
  const NAMES = { canvas: 'Canvas', webwork: 'WeBWorK', whatsapp: 'WhatsApp', email: 'Email', portal: 'Timetable', manual: 'Calendar' };
  const meta = [NAMES[e.source] || 'Calendar', quoteOf(e.desc) ? `“${quoteOf(e.desc).slice(0, 40)}”` : e.location].filter(Boolean).join(' · ');
  return h('div', { class: 'due' + (e.late ? ' late' : '') },
    h('span', { class: 'src', style: `background:${bg};color:${fg}`, 'aria-hidden': 'true' }, b),
    h('span', { class: 'txt', style: 'flex:1;min-width:0;display:flex;flex-direction:column;gap:3px' },
      h('span', { class: 'ttl', style: 'font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis' }, e.title),
      h('span', { style: 'font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis' }, meta)),
    h('span', { class: 'right' }, h('b', { style: e.late ? 'color:var(--late)' : e.done ? 'color:var(--ok)' : '' }, when), sub ? h('span', {}, sub) : null));
}
function viewDue() {
  const now = new Date();
  const all = events().filter((e) => DUE_KINDS.has(e.kind) || e.late || e.done);
  const open = all.filter((e) => !e.done && (e.late || e.end > now || (e.allDay && dayDiff(e.start, now) >= 0)));
  const done = all.filter((e) => e.done);
  const body = [header('Canvas, WeBWorK and your chats', 'Due'), ...notices()];
  const tab = (key, label, n) => h('button', { type: 'button', 'aria-pressed': String(S.dueTab === key),
    onclick: () => { S.dueTab = key; render(); } }, `${label} · ${n}`);
  body.push(h('div', { class: 'seg', role: 'group', 'aria-label': 'Show' }, tab('open', 'To do', open.length), tab('done', 'Submitted', done.length)));
  const groups = S.dueTab === 'done' ? [['Submitted', 'var(--ok)', done]] : [
    ['Missed, still open', 'var(--late)', open.filter((e) => e.late)],
    ['Today', 'var(--accent)', open.filter((e) => !e.late && dayDiff(e.start, now) <= 0)],
    ['Tomorrow', 'var(--accent)', open.filter((e) => !e.late && dayDiff(e.start, now) === 1)],
    ['This week', 'var(--muted)', open.filter((e) => !e.late && dayDiff(e.start, now) > 1 && dayDiff(e.start, now) <= 7)],
    ['Later', 'var(--muted)', open.filter((e) => !e.late && dayDiff(e.start, now) > 7)],
  ];
  let any = false;
  for (const [label, color, list] of groups) {
    if (!list.length) continue;
    any = true;
    body.push(h('section', { class: 'group' }, h('h2', { class: 'sec', style: `color:${color}` }, label), list.map((e) => dueCard(e, now))));
  }
  if (!any) body.push(h('div', { class: 'empty' }, S.dueTab === 'done' ? 'Nothing submitted in the last day or so.' : 'Nothing due. Enjoy it.'));
  return body;
}

function splitSrc(src) {
  const i = src.indexOf(': “');
  return i < 0 ? [src, ''] : [src.slice(0, i), src.slice(i + 3).replace(/”$/, '')];
}
function viewUpdates() {
  const f = feedData();
  const resolved = new Set(f.resolved || []);
  const items = (f.items || []).filter((i) => !resolved.has(i.id));
  const checks = items.filter((i) => i.type === 'check');
  const notes = items.filter((i) => i.type !== 'check');
  const body = [header('What changed, and where it came from', 'Updates'), ...notices()];
  for (const c of checks) {
    const [where, quote] = splitSrc(c.src || '');
    body.push(h('section', { class: 'check', 'aria-label': 'Please check' },
      h('span', { class: 'label' }, 'Please check · not added yet'),
      h('b', {}, c.title.replace(/\s+\(\w+, \d+% sure\)$/, '') + '?'),
      quote ? h('blockquote', {}, `“${quote}”`, h('br'), h('span', { style: 'font-size:12px;color:#A8996F' }, where)) : h('span', { class: 'eyebrow' }, where),
      h('div', { class: 'btns' },
        c.event ? h('button', { class: 'btn primary', type: 'button', onclick: () => resolveCheck(c, true) }, 'Add it')
          : h('span', { class: 'eyebrow', style: 'align-self:center' }, 'Fix it in Google Calendar if needed'),
        h('button', { class: 'btn ghost', type: 'button', onclick: () => resolveCheck(c, false) }, c.event ? 'Ignore' : 'Got it'))));
  }
  if (!notes.length && !checks.length) body.push(h('div', { class: 'empty' }, 'No changes yet. When a chat or email moves a class or adds a quiz, it shows up here with the message it came from.'));
  let lastDay = '';
  for (const n of notes) {
    const d = new Date(n.t * 1000);
    const dd = dayDiff(d, new Date());
    const day = dd === 0 ? 'Today' : dd === -1 ? 'Yesterday' : `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}`;
    if (day !== lastDay) { body.push(h('h2', { class: 'sec' }, day)); lastDay = day; }
    const [where, quote] = splitSrc(n.src || '');
    body.push(h('article', { class: 'note' },
      h('div', { class: 'row' }, h('span', { class: 'verb v-' + n.verb }, n.verb), h('span', { class: 'eyebrow', style: 'font-size:12px' }, hm(d))),
      h('span', { class: 'ttl' }, n.title.replace(/^(Added|Moved|Removed)\s+/, '')),
      where ? h('span', { class: 'from' }, quote ? `“${quote}” · ${where}` : where) : null));
  }
  if (S.mode === 'live') body.push(h('p', { class: 'eyebrow', style: 'text-align:center;margin:12px 0 0' },
    h('a', { href: '#', onclick: (e) => { e.preventDefault(); signOut(); } }, 'Sign out of this phone')));
  return body;
}

function viewWelcome() {
  const can = !!CFG.clientId;
  return [h('section', { class: 'welcome' },
    h('div', { class: 'eyebrow' }, 'uni-sync'),
    h('h1', {}, 'Your uni schedule, in one place.'),
    h('p', {}, 'Classes from the portal, deadlines from Canvas and WeBWorK, and the quizzes and cancellations announced in WhatsApp groups. uni-sync on your laptop keeps it up to date; this app shows it.'),
    S.err ? h('div', { class: 'error', role: 'alert' }, S.err) : null,
    h('button', { class: 'btn primary wide', type: 'button', disabled: !can, onclick: signIn }, 'Sign in with Google'),
    h('button', { class: 'btn ghost wide', type: 'button', onclick: () => { S.mode = 'demo'; store.set('mode', 'demo'); S.data = demoData(); render(); } }, 'Try it with sample data'),
    h('small', {}, can
      ? 'It can only see the calendar uni-sync created for you. Nothing is sent anywhere except between your phone and Google.'
      : 'Sign-in isn’t set up on this copy yet (the maintainer adds a Google client ID in config.js). The demo works.'))];
}
function viewNeedCal() {
  let val = '';
  return [header('One more step', 'Connect'), ...notices(), h('section', { class: 'card' },
    h('p', { style: 'margin:0;color:var(--text2);line-height:1.5' }, 'Couldn’t find your University calendar automatically. On your laptop run ', h('b', {}, 'uni-sync app-link'), ' and open the link it prints on this phone, or paste the Calendar ID here.'),
    h('label', { class: 'f' }, 'Calendar ID', h('input', { class: 'f', autocomplete: 'off', placeholder: '…@group.calendar.google.com', oninput: (e) => { val = e.target.value.trim(); } })),
    h('button', { class: 'btn primary wide', type: 'button', onclick: () => { if (val) { S.calId = val; store.set('calId', val); refresh(); } } }, 'Connect'))];
}

/* ------------------------------------------------------------------ demo */
function demoData() {
  const t0 = startOfDay(new Date());
  const at = (dayOff, h_, m) => new Date(t0.getFullYear(), t0.getMonth(), t0.getDate() + dayOff, h_, m);
  const ev = (id, title, kind, source, s, e, loc, desc) => ({ id, summary: title, location: loc || '', description: desc || '', status: 'confirmed',
    start: { dateTime: s.toISOString() }, end: { dateTime: e.toISOString() }, extendedProperties: { private: { unisync: '1', kind, source } } });
  const allday = (id, title, kind, source, off) => ({ id, summary: title, status: 'confirmed', start: { date: iso(addDays(t0, off)) }, end: { date: iso(addDays(t0, off + 1)) },
    extendedProperties: { private: { unisync: '1', kind, source } } });
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const nowH = new Date().getHours();
  const base = Math.min(Math.max(nowH - 1, 8), 15);
  const events = [
    ev('d1', 'Public Speaking & Presentations', 'class', 'portal', at(0, base - 1, 0), at(0, base - 1, 50), 'Ground floor, A-003'),
    ev('d2', 'Object Oriented Programming', 'class', 'portal', at(0, base + 1, 0), at(0, base + 2, 15), 'Room A-106'),
    ev('d3', 'Application of ICT Lab', 'class', 'portal', at(0, base + 3, 0), at(0, base + 5, 0), 'Computer Lab, A-106'),
    ev('d4', '🍽️ Dinner with Ali', 'social', 'whatsapp', at(0, 20, 0), at(0, 21, 30), '', 'From WhatsApp · Friends: “kal 8 baje dinner pe milte hain”'),
    ev('d5', '📝 DSA quiz: Chapters 4–5', 'quiz', 'whatsapp', at(1, 10, 0), at(1, 11, 0), '', 'From WhatsApp · BSCS-S3: “kal DSA ka quiz hai 10 baje, ch 4 and 5”'),
    ev('d6', '📐 Calculus homework set 6', 'assignment', 'webwork', at(0, 23, 59), at(1, 0, 0), '', 'WeBWorK · 60% done'),
    ev('d7', '📌 Speech outline draft', 'assignment', 'canvas', at(4, 23, 59), at(5, 0, 0), '', 'Canvas · Public Speaking'),
    allday('d8', '⚠️ LATE OOP Lab 7: Inheritance', 'assignment', 'canvas', 0),
    ev('d9', '✅ Calculus homework set 5', 'assignment', 'webwork', at(-1, 23, 59), at(0, 0, 0), '', 'WeBWorK · 100%'),
  ];
  const now = Date.now() / 1000;
  return {
    fetchedAt: Date.now(), events,
    feed: { v: 1, updated: now - 120, resolved: [], items: [
      { id: 'c1', t: now - 900, type: 'check', verb: 'Check', title: 'Physics viva — Thu 14:00 (add, 40% sure)',
        src: 'WhatsApp · BSCS-S3 · Hamza: “viva shayad thursday ko hai 2 baje, confirm karta hun”', event: {} },
      { id: 'n1', t: now - 4000, type: 'note', verb: 'Moved', title: 'Moved OOP lecture: Tue 10:30 → Tue 13:00',
        src: 'WhatsApp · BSCS-S3: “Tuesday ki OOP class 1 baje hogi”' },
      { id: 'n2', t: now - 9000, type: 'note', verb: 'Cancelled', title: 'ICT Lab, Thursday — removed',
        src: 'Email from Lab coordinator: “No lab this Thursday”' },
      { id: 'n3', t: now - 20000, type: 'note', verb: 'Added', title: 'Added DSA quiz — tomorrow 10:00',
        src: 'WhatsApp · BSCS-S3: “kal DSA ka quiz hai 10 baje, ch 4 and 5”' },
    ] },
  };
}

/* ------------------------------------------------------------------ router */
function route() {
  const hsh = location.hash || '#today';
  const m = /cal=([^&]+)/.exec(hsh);
  if (m) { S.calId = decodeURIComponent(m[1]); store.set('calId', S.calId); history.replaceState(null, '', '#today'); }
  const tab = ['today', 'due', 'updates'].find((t) => hsh.startsWith('#' + t)) || 'today';
  return tab;
}
function render() {
  const tab = route();
  const view = document.getElementById('view');
  let content;
  if (!S.mode) content = viewWelcome();
  else if (S.needCal) content = viewNeedCal();
  else content = tab === 'due' ? viewDue() : tab === 'updates' ? viewUpdates() : viewToday();
  view.replaceChildren(...content);
  document.querySelector('.tabs').hidden = !S.mode;
  for (const a of document.querySelectorAll('.tabs a')) {
    if (a.dataset.tab === tab) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  const f = feedData();
  const pending = (f.items || []).filter((i) => i.type === 'check' && !(f.resolved || []).includes(i.id)).length;
  const badge = document.getElementById('badge');
  badge.hidden = !S.mode || !pending; badge.textContent = pending;
  if (badge.hidden) badge.removeAttribute('aria-label'); else badge.setAttribute('aria-label', `${pending} to check`);
}
window.addEventListener('hashchange', () => { render(); document.getElementById('view').focus({ preventScroll: true }); window.scrollTo(0, 0); });
document.addEventListener('visibilitychange', () => { if (!document.hidden && S.mode === 'live' && tokenValid()) refresh(); });
if (S.mode === 'demo') S.data = demoData();
render();
if ('serviceWorker' in navigator && location.protocol === 'https:') navigator.serviceWorker.register('sw.js').catch(() => {});
setInterval(() => { if (!document.hidden) render(); }, 60e3); // keep "in 12 min" fresh
