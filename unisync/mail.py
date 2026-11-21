"""Reads new mail over IMAP (read-only, never marks as read) and sends notifications over SMTP."""
from __future__ import annotations

import email
import re
import imaplib
import smtplib
import ssl
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup


def _hdr(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _body(msg) -> str:
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/plain" and plain is None:
            plain = payload
        elif ct == "text/html" and html is None:
            html = payload
    if plain and plain.strip():
        text = plain
    elif html:
        text = BeautifulSoup(html, "lxml").get_text("\n")
    else:
        text = ""
    # drop quoted replies and collapse blank lines
    lines = [l.rstrip() for l in text.splitlines() if not l.startswith(">")]
    out = "\n".join(l for i, l in enumerate(lines) if l or (i and lines[i - 1]))
    return out.strip()


def fetch_new(cfg, db, limit: int = 50) -> list[dict]:
    """New messages since the last run. First run only remembers the position (doesn't backfill)."""
    M = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ssl.create_default_context())
    M.login(cfg.imap_user, cfg.imap_password)
    M.select(cfg.imap_folder, readonly=True)
    key = f"imap_last_uid:{cfg.imap_user}:{cfg.imap_folder}"
    last = db.get(key)
    typ, data = M.uid("search", None, "ALL")
    uids = [int(u) for u in data[0].split()] if data and data[0] else []
    if last is None:
        db.set(key, max(uids, default=0))
        M.logout()
        return []
    new = [u for u in uids if u > last][-limit:]
    out = []
    for u in new:
        typ, d = M.uid("fetch", str(u), "(BODY.PEEK[])")
        if not d or not d[0]:
            continue
        msg = email.message_from_bytes(d[0][1])
        try:
            when = parsedate_to_datetime(msg["Date"]).astimezone(cfg.tz)
        except Exception:
            when = datetime.now(cfg.tz)
        out.append({"uid": u, "id": msg.get("Message-ID", str(u)), "from": _hdr(msg["From"]),
                    "subject": _hdr(msg["Subject"]), "time": when, "body": _body(msg),
                    "bulk": bool(msg.get("List-Unsubscribe") or (msg.get("Precedence", "").lower() in ("bulk", "list")))})
    if uids:
        db.set(key, max(uids))
    M.logout()
    return out


TRANSACTIONAL = re.compile(
    r"(\bRs\.?\s?\d|\bPKR\b|\bAED\b|\bUSD\b|\$\s?\d|\btransaction\b|\btransferred\b|\byou (got|received|sent|paid)\b|"
    r"\bpayment (received|successful|confirmed)\b|\breceipt\b|\binvoice\b|\bOTP\b|\bverification code\b|"
    r"\bone[- ]time (password|code)\b|\byour order\b|\border (#|no\.?|number)|\bdelivered\b|\bshipped\b|"
    r"\bcashback\b|\bavailable balance\b|\baccount statement\b|\bnew sign[- ]in\b|\blogin attempt\b|"
    r"\bsecurity alert\b|\bpassword reset\b|cha-ching)", re.I)


def skip_reason(cfg, m: dict) -> str | None:
    """Emails that are never schedule events: payments, OTPs, shopping, newsletters.
    Mail from your university / Canvas is always checked."""
    sender = m["from"].lower()
    if any(d in sender for d in cfg.trusted_domains):
        return None
    head = f"{m['subject']}\n{m['body'][:800]}"
    if TRANSACTIONAL.search(head):
        return "payment/OTP/shopping notification"
    if m.get("bulk"):
        return "newsletter/marketing"
    return None


def send(cfg, subject: str, text: str, html: str | None = None) -> None:
    if not (cfg.smtp_host and cfg.notify_to):
        print(f"[mail not configured] {subject}\n{text}")
        return
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = f"Uni Sync <{cfg.smtp_user}>"
    m["To"] = cfg.notify_to
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    _smtp_send(cfg, m)


def _smtp_once(host: str, port: int, cfg, m) -> None:
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(m)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(m)


def _ipv4(host: str) -> str | None:
    import socket
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except OSError:
        return None


def _smtp_send(cfg, m) -> None:
    """Try the configured port, then 465 (SSL) and 587, then IPv4-only.
    Some ISPs block 587, and some servers have broken IPv6."""
    ports = [cfg.smtp_port] + [p for p in (465, 587) if p != cfg.smtp_port]
    errors = []
    for port in ports:
        try:
            return _smtp_once(cfg.smtp_host, port, cfg, m)
        except smtplib.SMTPAuthenticationError:
            raise
        except (OSError, smtplib.SMTPException) as e:
            errors.append(f"{port}: {e}")
    v4 = _ipv4(cfg.smtp_host)
    if v4:
        # connect by IPv4 address but verify the certificate against the real hostname
        class _SSL4(smtplib.SMTP_SSL):
            def _get_socket(self, host, port, timeout):
                import socket
                raw = socket.create_connection((v4, port), timeout)
                return self.context.wrap_socket(raw, server_hostname=cfg.smtp_host)
        try:
            with _SSL4(cfg.smtp_host, 465, timeout=30, context=ssl.create_default_context()) as s:
                s.login(cfg.smtp_user, cfg.smtp_password)
                return s.send_message(m)
        except smtplib.SMTPAuthenticationError:
            raise
        except (OSError, smtplib.SMTPException) as e:
            errors.append(f"465/IPv4: {e}")
    raise OSError("Couldn't reach the mail server on any port — " + "; ".join(errors))


def check(cfg) -> str:
    M = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ssl.create_default_context())
    M.login(cfg.imap_user, cfg.imap_password)
    M.select(cfg.imap_folder, readonly=True)
    M.logout()
    return f"{cfg.imap_user} / {cfg.imap_folder}"
