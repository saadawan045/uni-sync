# uni-sync

A self-hosted tool that keeps a **"University" Google Calendar** up to date from a student's timetable, Canvas, WeBWorK, email and WhatsApp. A planner app on the phone (I use Structured) imports that calendar.

Built for my own use as a CS student at NIT Lahore and running 24/7 on an old laptop turned Ubuntu server. The portal parser is written for NIT's student portal; other portals need changes in `unisync/portal.py`. Built with AI-assisted development (Claude Code); design, testing and running it on real data are my own.

```
NIT portal (timetable) ──┐
University email ────────┤──►  your server  ──►  Google Calendar "University"  ──►  Structured (Android)
WhatsApp (all chats) ────┘    (local AI,              │
                               no open ports)         └──►  email: changes, 7:00 digest, 21:30 tomorrow preview
```

- **Timetable**: logs into `nit.effii.app` 3× a day and puts the next 3 weeks of classes on the calendar. Changes are emailed to you.
- **WhatsApp + email**: messages that look schedule-related ("DSA quiz kal 10 baje", "Tuesday's class cancelled", "Assignment due Friday 11:59pm") go to a **local AI model** (Ollama on this server). It adds, moves or removes calendar events, and emails you what it did, quoting the message it acted on.
- **Not sure?** Anything the AI is under 60% sure about is *not* added. It's emailed to you under "please check".
- **Which messages reach the AI**: every message is saved, then a quick keyword filter (not AI) picks which go to the AI, since each AI check takes about a minute on this server. `filter` under `[llm]` controls how picky it is:
  - `"loose"` (the default) sends anything that mentions a time or a day.
  - `"strict"` also needs a plan word.
  - `"off"` sends everything; the AI may fall behind in busy groups.

  Notes to yourself always go through. Each check logs a line like `120 new message(s) … 6 looked like plans → AI`.
- **Personal plans too**: hangouts ("kal 8 baje dinner pe milte hain" → "Dinner with Ali"), appointments, birthdays, and things *you* promised ("I'll send the notes by Friday" → a 🤝 reminder). Your own messages are read for this, labelled "Me". A plan nobody confirmed is emailed as "please check" instead of added; once someone says "done"/"pakka"/👍 it goes on the calendar. Turn this off with `detect_personal = false` under `[llm]`.
- **Manual add**: send yourself a WhatsApp message (the "Message yourself" chat), e.g. `OOP viva Thursday 2pm`. It gets added.

**Privacy.** Chat text never leaves your server; the AI runs locally. Message text is deleted once processed, apart from each chat's last 8 messages, which are kept for up to 12 hours (in `wa-context.json`, readable only by you) so a plan and a reply hours later are read together. Set `context_hours = 0` to keep nothing. Only the event (title, time, the quoted line) goes to Google Calendar. The server opens no ports, so nothing on the internet can connect to it.

---

## Setup (about 45 minutes)

### 1. Get the code on the server and install

```bash
ssh <user>@<server-ip>
git clone https://github.com/<your-username>/uni-sync.git && cd uni-sync
./install.sh
```

This installs Node 22, Python packages, headless Chromium, Ollama with the `qwen2.5:3b` model (about 2 GB), the `uni-sync` command, and the background services.

### 2. Google Calendar (service account)

A service account is a robot Google login. It can only touch the one calendar you share with it, not the rest of your account.

1. **Create the calendar.** On calendar.google.com (computer), go to **Other calendars → + → Create new calendar** and name it **University**.
2. **Create a project.** At console.cloud.google.com, make a new project called `uni-sync`.
3. **Enable the API.** Go to **APIs & Services → Library → "Google Calendar API" → Enable**.
4. **Create the service account.** Go to **IAM & Admin → Service Accounts → Create**, name it `uni-sync`, and skip the role steps. Open it, then go to **Keys → Add key → JSON**. A file downloads.
5. **Copy the key to the server.**
   `scp ~/Downloads/uni-sync-*.json server@<server-ip>:~/.config/uni-sync/service-account.json`
6. **Share the calendar.** In Google Calendar, open **University → Settings and sharing → Share with specific people**. Add the service account's email (`uni-sync@….iam.gserviceaccount.com`) with **Make changes to events**.
7. **Copy the Calendar ID.** On the same settings page, under **Integrate calendar**, copy the **Calendar ID** into `config.toml` → `[google] calendar_id`.

### 3. Email

Microsoft has turned off password logins for university Outlook, so the server can't log into it directly. Instead, Outlook forwards to a Gmail account, and the server reads that.

1. **Turn on 2-Step Verification for Gmail.** Then go to myaccount.google.com → search **App passwords**, create one called `uni-sync`, and put the 16 letters in `imap_password`.
2. **Set up forwarding in Outlook on the web.** Go to **Settings → Mail → Forwarding**, turn on forwarding to that Gmail address, and tick **Keep a copy**.
   - If your university blocks forwarding to outside addresses, use **Rules → Add new rule → "Redirect to"** instead. If that's blocked too, the email part won't work. Timetable and WhatsApp still will.
3. **Fill in the rest of `[mail]`.** Set `notify_to` to wherever you want updates, such as your university address.

The first scan only records where your inbox is; it doesn't process old mail.

### 4. Portal login

Put your login in `[portal]`, then:

```bash
uni-sync portal-sync --dry-run      # logs in and prints the classes it found — calendar untouched
```

- **If it lists your classes correctly**, you're done.
- **If it fails or finds 0 classes**, run `uni-sync portal-discover`. It saves screenshots, HTML and a list of links to `~/.local/share/uni-sync/discover/`.
  - **Wrong link?** Set `timetable_url` from `links.txt`.
  - **Login fields not detected?** Set `[portal.selectors]`.
  - **Still stuck?** Your portal's layout is probably different. Adapt the parser in `unisync/portal.py` using the saved `*-candidate.html`.

### 5. Test the local AI

```bash
uni-sync test "kal DSA ka quiz hai 10 baje, chapter 4 and 5"
uni-sync test "Tuesday wali OOP class cancel hai"
```

Each takes about 20–40 s on a 4-core laptop CPU. That's normal; it runs in the background. If it's too slow, set `model = "qwen2.5:1.5b"` and run `ollama pull qwen2.5:1.5b`.

### 6. Check everything

```bash
uni-sync doctor --send-test
```

### 7. Link WhatsApp (one or more numbers)

Each number gets a short name of your choice and its own background bridge. Link each one:

```bash
uni-sync wa-link personal 971XXXXXXXXX     # regular WhatsApp
uni-sync wa-link business 92XXXXXXXXXX     # WhatsApp Business
```

Use the number with country code, no `+` or spaces.

On the phone for that number, go to **WhatsApp (or WhatsApp Business) → Settings → Linked devices → Link a device → Link with phone number instead**, then type the code shown. Wait for **Connected**, then press **Ctrl+C**. That bridge then runs in the background. Repeat for the next number.

- **QR code instead:** leave out the phone number.
- **See what's linked:** `uni-sync wa-list` shows each account and whether its bridge is running.
- **Messages from both numbers** go to the same AI and calendar. Emails say which number a message came from, e.g. "WhatsApp (business) · BSCS-S3". A group that both numbers are in is only processed once.

### 7b. Canvas (assignment due dates)

Pick **one** way in and fill it in under `[canvas]` in `config.toml`:

- **Access token (recommended).** It also knows what you've already submitted, so submitted work turns ✅ and stops reminding you.
  1. Log into Canvas in a browser and go to **Account → Settings**.
  2. Under "Approved integrations", click **+ New access token**, name it `uni-sync`, and copy the token.
  3. Set `token = '…'`, and set `url` to your Canvas address, e.g. `url = 'https://nit.instructure.com'` (whatever is in the browser bar).
- **Calendar feed (no token).** In Canvas, open **Calendar**, then click **Calendar Feed** at the bottom right. Copy the link into `ical_url = '…'`. This route can't tell what you've submitted.

Then test it with `uni-sync canvas-sync --dry-run`, which lists what it found.

Once set up, it checks Canvas every 2 hours:
- Every assignment, quiz and discussion goes on the calendar at its due time as 📌, with reminders 1 day and 1 hour before.
- You get an email when something **new** is posted, a **due date changes**, or something is **removed**.
- **Missed but still open** (token only): if a deadline passes without a submission and Canvas still accepts late work (not locked), it moves to an all-day `⚠️ LATE` item on *today* showing when it closes. You get one email when it's missed, and it's listed in the morning digest until you submit it or it locks. Only the last `missed_days` (default 30) are checked; `missed_days = 0` turns this off. Paper/in-class items are skipped.
- The 7:00 email gets a **"Due soon on Canvas"** list of anything due in the next 36 hours that you haven't submitted.

The token gives read access to your Canvas account, so treat it like a password. To revoke it, delete it from Canvas → Settings.

### 7c. WeBWorK (maths homework)

Add one block per maths course to the end of `config.toml`:

```toml
[[webwork]]
name     = 'MATH-101'
url      = 'https://YOUR-WEBWORK-SITE/webwork2/COURSE_NAME/'
username = 'your webwork username'
password = 'your webwork password'
```

For `url`, open the course in a browser, log in, and copy the address of the page listing your homework sets. It usually ends in `/webwork2/SOMETHING/`. For a second course, add another `[[webwork]]` block.

Test with `uni-sync webwork-sync --dry-run`. Once set up, it checks every 3 hours:
- Each open or upcoming set goes on the calendar as 📐 at its close time, with reminders 1 day and 1 hour before.
- Sets you've finished at 100% turn ✅.
- New sets and changed due dates are emailed to you.
- Anything due within 36 hours that isn't 100% appears in the 7:00 email.

**If you open WeBWorK from Canvas** (no separate WeBWorK password), this login won't work. Check whether the sets also show up in your Canvas assignments, in which case the Canvas sync already covers them. Otherwise ask your instructor to set a WeBWorK password for you.

### 8. Structured (Android)

In Structured's settings, open the calendar import, choose **Google Calendar**, and turn on **only** the **University** calendar. Only do this on one device, or you'll get duplicates.

Optional: install the Google Calendar app too. Quizzes, exams and deadlines then get pop-up reminders 1 day and 1 hour before.

### 9. Before relying on it

Run `uni-sync portal-sync` (without `--dry-run`) once and check the calendar looks right. Everything else is already scheduled.

---

## WhatsApp safety — read this

The bridge uses Baileys, an **unofficial** WhatsApp Web client. No unofficial client is zero-risk: WhatsApp can restrict a number that uses one. Most bans hit accounts that **send** spam or bulk messages. This bridge only ever receives. The risk can't be removed, but it's minimised as far as possible.

**What the bridge does for you:**

| Risk factor | What the bridge does |
|---|---|
| Sending / automation | **Never sends anything**: no messages, reactions, read receipts or typing. Your messages stay unread (blue ticks only when *you* read them). |
| Looking like a bot online 24/7 | Connects as "offline" (`markOnlineOnConnect: false`). This also keeps your phone's notifications working. |
| Heavy traffic | Only WhatsApp's normal small startup sync (like WhatsApp Web), never the full history download. No link previews, no media downloads. Group names are fetched at most once a day per group, one every 5 s at most. |
| Reconnect storms | Slow back-off (10 s → 30 min). After 8 drops in an hour it pauses for 6 h and emails you. |
| Repeated re-linking (a big red flag) | One saved session. It never re-pairs by itself. On logout, a 403 or a session conflict it **stops completely** and emails you. |
| Outdated client fingerprint | `uni-sync wa-update` pulls the latest library. Run it about once a month. |
| Datacenter IPs | It runs on your home connection, which is good. Don't route it through a VPN or cloud server. |

**What you should do:**

- **Turn on WhatsApp chat backup** (Settings → Chats → Chat backup → Google Drive). If the worst happens, you keep your chats.
- **Link once and leave it.** Don't unlink and relink repeatedly.
- **Keep your phone online regularly.** Linked devices drop off if the phone is offline for 14 days.
- **Don't use WhatsApp mods** (GB WhatsApp and similar) or other bots on the same number.
- **If WhatsApp ever shows you a warning**, run `uni-sync pause` (all numbers) or `uni-sync pause business` (just that one). If the bridge reports `forbidden`, stay unlinked for a few days.
- **Lock down the server.** `~/.local/share/uni-sync/wa-auth` *is* access to your WhatsApp.
  - Use SSH keys only: set `PasswordAuthentication no` in `/etc/ssh/sshd_config`.
  - Keep the server updated.
  - Don't share the account.

**Kill switch:** `uni-sync pause` disconnects every bridge within a minute and keeps the sessions; `uni-sync pause <name>` does just one. Add `--off` to resume. `uni-sync wa-unlink <name>` removes that number's session entirely.

**Two numbers = two separate risks.** Each number is a separate account to WhatsApp, and the protections above apply to each. Linking both from the same home connection is normal (like using WhatsApp Web for both in one browser).

To limit which chats are read, use `only_chats` / `ignore_chats` in the config. Messages from ignored chats are dropped before the AI sees them.

---

## Everyday commands

| Command | What it does |
|---|---|
| `uni-sync status` | when each job last ran and next runs |
| `uni-sync logs` | recent logs from all parts |
| `uni-sync portal-sync` | sync the timetable now |
| `uni-sync canvas-sync` | sync Canvas due dates now (`--dry-run` to just list them) |
| `uni-sync webwork-sync` | sync WeBWorK sets now (`--dry-run` to just list them) |
| `uni-sync digest --print` | show today's schedule in the terminal |
| `uni-sync test "…"` | see what the AI would do with a message |
| `uni-sync pause [name]` / `pause [name] --off` | WhatsApp kill switch (all, or one number) |
| `uni-sync wa-list` | linked WhatsApp numbers and whether each is running |
| `uni-sync wa-update` | update the WhatsApp library (about monthly) |

**Schedule (Pakistan time):**
- Timetable sync at 06:00, 13:00 and 19:00.
- WhatsApp processed every 5 min.
- Email checked every 10 min.
- Digest at 07:00, tomorrow preview at 21:30.

**How cancellations work:** if a chat says a class is cancelled or moved, that class is removed from the calendar and remembered. The next timetable sync won't put it back. Moved classes get a new event at the new time.

**Failures:** if the portal, email or AI stops working, you get one email, at most every 12 hours, and it keeps retrying. If the portal returns a page with no recognisable classes, the calendar is left untouched rather than wiped.

## Files

```
unisync/portal.py   portal login + timetable parser (3 layouts: days×times grid, times×days grid, list)
unisync/extract.py  keyword pre-filter + applying AI output to the calendar
unisync/llm.py      the local-model prompt (handles English / Urdu / Roman Urdu)
unisync/gcal.py     Google Calendar
unisync/mail.py     IMAP read (read-only) + SMTP send
unisync/cli.py      commands
wa-bridge/index.mjs WhatsApp bridge (receive-only)
systemd/            background services and timers
```

Config: `~/.config/uni-sync/config.toml` · Data: `~/.local/share/uni-sync/`
