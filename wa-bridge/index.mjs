// uni-sync WhatsApp bridge — RECEIVE-ONLY.
//
// Links to your WhatsApp as a "linked device" (like WhatsApp Web) and writes the
// text of incoming messages to a local spool file that the Python side reads.
//
// Ban-risk hardening (see README "WhatsApp safety"):
//   * never sends a message, reaction, read receipt, typing or presence update
//   * appears offline (markOnlineOnConnect=false) so your phone keeps notifying you
//   * no full-history download, no link previews, no media downloads; old messages never processed
//   * one persistent session — never re-pairs on its own
//   * slow exponential back-off on disconnects; stops completely on logout/ban codes
//   * group names fetched at most once a day per group, throttled
//   * PAUSE file = instant kill switch
//   * logs never contain message text

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  Browsers,
  isJidGroup,
  isJidBroadcast,
  isJidStatusBroadcast,
  isJidNewsletter,
  jidNormalizedUser,
  normalizeMessageContent,
  proto,
} from 'baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import fs from 'node:fs'
import path from 'node:path'

// ---------- config ----------
const args = process.argv.slice(2)
const argVal = (flag) => { const i = args.indexOf(flag); return i >= 0 ? (args[i + 1] || '') : null }

// One bridge process per WhatsApp number, e.g. --account personal / --account business
const ACCOUNT = (argVal('--account') || process.env.UNISYNC_WA_ACCOUNT || 'main').replace(/[^a-z0-9_-]/gi, '').toLowerCase() || 'main'
const DATA_DIR = process.env.UNISYNC_DATA || path.join(process.env.HOME, '.local/share/uni-sync')
const AUTH_DIR = path.join(DATA_DIR, `wa-auth-${ACCOUNT}`)
const SPOOL = path.join(DATA_DIR, `wa-spool-${ACCOUNT}.jsonl`)
const ALERTS = path.join(DATA_DIR, 'alerts.jsonl')
const GROUP_CACHE = path.join(DATA_DIR, `wa-groups-${ACCOUNT}.json`)
const PAUSE_FILE = path.join(DATA_DIR, 'PAUSE')                 // pauses every account
const PAUSE_ACCOUNT = path.join(DATA_DIR, `PAUSE-${ACCOUNT}`)   // pauses just this one
const paused = () => fs.existsSync(PAUSE_FILE) || fs.existsSync(PAUSE_ACCOUNT)
const MAX_AGE_SEC = 48 * 3600          // ignore anything older than 2 days
const GROUP_TTL_MS = 24 * 3600 * 1000  // refresh a group's name at most daily
const MAX_FAILS_PER_HOUR = 8           // then give up and alert you

fs.mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 })
fs.chmodSync(DATA_DIR, 0o700)
fs.chmodSync(AUTH_DIR, 0o700)

const PAIR_PHONE = argVal('--pair') !== null ? argVal('--pair').replace(/\D/g, '') : null

const log = pino({ level: process.env.LOG_LEVEL || 'warn' })
// The Signal library prints session/key objects with console.*; keep those out of the logs.
const out = (...m) => process.stdout.write(m.map(String).join(' ') + '\n')
console.log = console.info = console.debug = console.warn = console.error = () => {}
const say = (...m) => out(new Date().toISOString(), `[${ACCOUNT}]`, ...m)

function alert(kind, detail) {
  fs.appendFileSync(ALERTS, JSON.stringify({ ts: Date.now(), source: 'whatsapp', account: ACCOUNT, kind, detail: `[${ACCOUNT}] ${detail}` }) + '\n', { mode: 0o600 })
  say('ALERT', kind, detail)
}

// ---------- group-name cache (throttled) ----------
let groupCache = {}
try { groupCache = JSON.parse(fs.readFileSync(GROUP_CACHE, 'utf8')) } catch {}
let lastGroupFetch = 0
async function groupName(sock, jid) {
  const hit = groupCache[jid]
  if (hit && Date.now() - hit.at < GROUP_TTL_MS) return hit.name
  // at most one metadata query every 5 s, across all groups
  if (Date.now() - lastGroupFetch < 5000) return hit?.name || 'Group'
  lastGroupFetch = Date.now()
  try {
    const md = await sock.groupMetadata(jid)
    groupCache[jid] = { name: md.subject, at: Date.now() }
    fs.writeFileSync(GROUP_CACHE, JSON.stringify(groupCache), { mode: 0o600 })
    return md.subject
  } catch {
    return hit?.name || 'Group'
  }
}

// ---------- 1:1 chat names (your own messages carry YOUR name, so remember the contact's) ----------
const DM_CACHE = path.join(DATA_DIR, `wa-dm-names-${ACCOUNT}.json`)
let dmNames = {}
try { dmNames = JSON.parse(fs.readFileSync(DM_CACHE, 'utf8')) } catch {}
function dmName(jid, pushName) {
  if (pushName && dmNames[jid] !== pushName) {
    dmNames[jid] = pushName
    try { fs.writeFileSync(DM_CACHE, JSON.stringify(dmNames), { mode: 0o600 }) } catch {}
  }
  return dmNames[jid] || 'Contact'
}

// ---------- text extraction ----------
// When you reply to a message, WhatsApp includes a copy of the original — keep it as context,
// so "cancelled" in reply to "OOP viva Thursday 2pm" can be understood.
function quotedOf(message) {
  const m = normalizeMessageContent(message)
  if (!m) return null
  for (const v of Object.values(m)) {
    const q = v && typeof v === 'object' ? v.contextInfo?.quotedMessage : null
    if (q) return textOf(q)
  }
  return null
}

function textOf(message) {
  const m = normalizeMessageContent(message)
  if (!m) return null
  const edited = m.protocolMessage?.editedMessage
  if (edited) return textOf(edited)
  return (
    m.conversation ||
    m.extendedTextMessage?.text ||
    m.imageMessage?.caption ||
    m.videoMessage?.caption ||
    m.documentMessage?.caption ||
    m.documentWithCaptionMessage?.message?.documentMessage?.caption ||
    (m.documentMessage?.fileName ? `[document] ${m.documentMessage.fileName}` : null) ||
    m.pollCreationMessage?.name || m.pollCreationMessageV3?.name ||
    null
  )
}

// ---------- main ----------
const WATCHDOG_MS = 3 * 3600_000
let lastActivity = Date.now()
const stats = { seen: 0, saved: 0 }
let failTimes = []
let attempt = 0
let stopped = false

async function start() {
  if (paused()) {
    say('Paused — not connecting. Run: uni-sync pause --off')
    return setTimeout(start, 60_000)
  }

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)

  if (!state.creds.registered && !process.stdin.isTTY && !PAIR_PHONE) {
    alert('not_linked', `This WhatsApp account is not linked. Run: uni-sync wa-link ${ACCOUNT} <phone>`)
    stopped = true
    process.exit(0)
  }

  let version
  try { ({ version } = await fetchLatestBaileysVersion()) } catch {}

  const sock = makeWASocket({
    auth: state,
    version,
    logger: log,
    browser: Browsers.ubuntu('Chrome'),   // shows as "Chrome (Ubuntu)" in Linked devices
    markOnlineOnConnect: false,           // stay "offline" — no presence spam, phone keeps notifying
    syncFullHistory: false,
    // Allow WhatsApp's normal small startup sync (contacts, ID mappings, recent chats) — blocking it
    // all breaks sessions in Baileys 7. Never the FULL history download. Synced history is NOT
    // processed: only new incoming messages ('messages.upsert') are read.
    shouldSyncHistoryMessage: ({ syncType }) =>
      syncType !== proto.HistorySync.HistorySyncType.FULL && syncType !== proto.HistorySync.HistorySyncType.ON_DEMAND,
    generateHighQualityLinkPreview: false,
    emitOwnEvents: false,
    getMessage: async () => undefined,
    cachedGroupMetadata: async (jid) => undefined,
  })

  sock.ev.on('creds.update', saveCreds)

  let pairRequested = false
  sock.ev.on('connection.update', async (u) => {
    const { connection, lastDisconnect, qr } = u

    if (qr && !state.creds.registered) {
      if (PAIR_PHONE && !pairRequested) {
        pairRequested = true
        try {
          const code = await sock.requestPairingCode(PAIR_PHONE)
          say(`\n\n  Pairing code:  ${code}\n\n  On your phone: WhatsApp → Settings → Linked devices → Link a device →\n  "Link with phone number instead" → enter the code.\n`)
        } catch (e) { say('Could not get pairing code:', e.message) }
      } else if (!PAIR_PHONE) {
        say('Scan this QR in WhatsApp → Settings → Linked devices → Link a device:')
        qrcode.generate(qr, { small: true }, (q) => out(q))
      }
    }

    if (connection === 'open') {
      attempt = 0
      lastActivity = Date.now()
      say('Connected (receive-only).')
      // make sure we never show as online
      try { await sock.sendPresenceUpdate('unavailable') } catch {}
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      say('Disconnected, code', code)

      if (code === DisconnectReason.loggedOut) {
        // the saved session is dead — delete it so the next wa-link starts a fresh pairing
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }) } catch {}
        alert('logged_out', `WhatsApp unlinked this server (you removed it, or WhatsApp did). Bridge stopped. Re-link with: uni-sync wa-link ${ACCOUNT} <phone>`)
        stopped = true; return process.exit(0)
      }
      if (code === DisconnectReason.forbidden) {
        alert('forbidden', 'WhatsApp returned 403 (possible restriction). Bridge stopped — do NOT re-link for at least a few days.')
        stopped = true; return process.exit(0)
      }
      if (code === DisconnectReason.connectionReplaced) {
        alert('replaced', 'Another session with the same credentials connected. Bridge stopped to avoid a conflict loop.')
        stopped = true; return process.exit(0)
      }
      if (code === DisconnectReason.restartRequired) {
        return start() // normal right after pairing
      }

      const now = Date.now()
      failTimes = failTimes.filter((t) => now - t < 3600_000).concat(now)
      if (failTimes.length > MAX_FAILS_PER_HOUR) {
        alert('flapping', `Connection dropped ${failTimes.length} times in an hour (last code ${code}). Pausing 6 h.`)
        failTimes = []
        return setTimeout(start, 6 * 3600_000)
      }
      attempt++
      const delay = Math.min(30 * 60_000, 10_000 * 2 ** (attempt - 1)) * (0.8 + Math.random() * 0.4)
      say(`Reconnecting in ${Math.round(delay / 1000)} s`)
      setTimeout(start, delay)
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    stats.seen += messages.length
    if (type !== 'notify' && type !== 'append') return
    const me = [sock.user?.id, sock.user?.lid].filter(Boolean).map(jidNormalizedUser)

    for (const msg of messages) {
      try {
        const jid = msg.key?.remoteJid
        if (!jid || !msg.message) continue
        if (isJidStatusBroadcast(jid) || isJidNewsletter(jid) || isJidBroadcast(jid)) continue

        const isSelfChat = me.includes(jidNormalizedUser(jid))
        // Your own messages count too: "I'll be there at 8" or "I'll send the notes Friday" are your plans.
        const fromMe = !!msg.key.fromMe

        const ts = Number(msg.messageTimestamp || 0)
        if (ts && Date.now() / 1000 - ts > MAX_AGE_SEC) continue

        const text = textOf(msg.message)
        if (!text || text.trim().length < 3) continue

        const group = isJidGroup(jid)
        const rec = {
          account: ACCOUNT,
          id: msg.key.id,
          chat: jid,
          chat_name: group ? await groupName(sock, jid) : (isSelfChat ? 'Note to self' : dmName(jid, fromMe ? null : msg.pushName)),
          sender: fromMe ? 'Me' : (msg.pushName || ''),
          from_me: fromMe,
          is_group: !!group,
          self_note: isSelfChat,
          ts: ts || Math.floor(Date.now() / 1000),
          text,
          quoted: quotedOf(msg.message) || undefined,
        }
        fs.appendFileSync(SPOOL, JSON.stringify(rec) + '\n', { mode: 0o600 })
        stats.saved++
        say(`saved ${group ? 'group' : (isSelfChat ? 'note-to-self' : 'direct')} message${fromMe ? ' (sent by you)' : ''}`)
      } catch (e) {
        log.warn({ err: e?.message }, 'message handling failed')
      }
    }
  })

  // Any traffic from WhatsApp (messages, receipts, presence, chat updates) counts as "alive".
  sock.ev.process(async () => { lastActivity = Date.now() })

  // Watchdog: a connection that says "open" but has received nothing for hours is dead — reconnect.
  // Stats: one line every 30 min so the log shows messages are flowing (no message content logged).
  let statTick = 0
  const watchdog = setInterval(() => {
    if (++statTick % 3 === 0) {
      say(`last 30 min: ${stats.seen} message(s) received, ${stats.saved} saved for checking`)
      stats.seen = stats.saved = 0
    }
    if (Date.now() - lastActivity > WATCHDOG_MS) {
      say(`No activity from WhatsApp for ${Math.round((Date.now() - lastActivity) / 3600000)} h — reconnecting.`)
      clearInterval(watchdog)
      lastActivity = Date.now()
      try { sock.end(new Error('watchdog: no activity')) } catch {}
    }
  }, 10 * 60_000)
  sock.ev.on('connection.update', ({ connection }) => { if (connection === 'close') clearInterval(watchdog) })

  // kill switch: drop the connection within a minute of PAUSE appearing
  const pauseTimer = setInterval(() => {
    if (paused()) {
      clearInterval(pauseTimer)
      say('PAUSE file found — disconnecting (session kept, not logged out).')
      sock.end(undefined)
    }
  }, 60_000)
  sock.ev.on('connection.update', ({ connection }) => { if (connection === 'close') clearInterval(pauseTimer) })
}

process.on('SIGTERM', () => process.exit(0))
process.on('SIGINT', () => process.exit(0))
start().catch((e) => { out('fatal:', e?.stack || e); process.exit(1) })
