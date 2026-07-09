# -*- coding: utf-8 -*-
"""
Mail & Kalender Web-App (Backend) - Mehrbenutzer-Version
==========================================================
Läuft mit reinem Python (keine Zusatzpakete nötig).

Sicherheitsmodell:
  - Jede Person (z.B. Alpkaan und seine Frau) hat ein eigenes Login
    (Benutzername + Passwort, sicher gehasht mit PBKDF2).
  - Mails und Termine sind ausschließlich nach Login sichtbar.
  - Der Kalender-Abo-Link (webcal://...ics) ist die einzige öffentliche
    Ausnahme -- technisch notwendig, damit iPhones Kalender ohne Login
    abonnieren können. Der Link ist ein 32-Byte-Zufallscode (praktisch
    nicht erratbar) und erlaubt nur Lesen, nie Schreiben.

Kalender-Verknüpfung:
  - Jede Person hat ihren eigenen Kalender.
  - Öffnet man den Freigabe-Link eines Partners, während man in der
    App eingeloggt ist, kann man auf "In meiner App verknüpfen" tippen.
  - Danach zeigt die App beide Kalender zusammen an (eigene Termine in
    Gold, Partner-Termine in Blau), inklusive Live-Aktualisierung.

Start:
    python3 server.py
"""

import http.server
import socketserver
import json
import sqlite3
import os
import uuid
import imaplib
import smtplib
import email
import ssl
import threading
import hashlib
import secrets
from email.mime.text import MIMEText
from email.utils import formataddr
from email.header import decode_header, make_header
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = BASE_DIR
ALLOWED_STATIC_FILES = {"index.html", "manifest.json", "sw.js"}
DB_FILE = os.path.join(BASE_DIR, "app.db")
PORT = int(os.environ.get("PORT", 8000))

DB_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Datenbank
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE COLLATE NOCASE,
        salt TEXT,
        password_hash TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS accounts (
        user_id TEXT PRIMARY KEY,
        name TEXT, email TEXT, password TEXT,
        imap_server TEXT, imap_port INTEGER,
        smtp_server TEXT, smtp_port INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS calendars (
        id TEXT PRIMARY KEY,
        owner_user_id TEXT,
        name TEXT,
        share_token TEXT UNIQUE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS calendar_access (
        viewer_user_id TEXT,
        calendar_id TEXT,
        granted_at TEXT,
        PRIMARY KEY (viewer_user_id, calendar_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        calendar_id TEXT,
        title TEXT,
        description TEXT,
        location TEXT,
        start_utc TEXT,
        end_utc TEXT,
        all_day INTEGER,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS game_scores (
        user_id TEXT PRIMARY KEY,
        high_score INTEGER
    )""")
    conn.commit()
    conn.close()


def hash_password(password, salt_hex=None):
    """Sicherer Passwort-Hash mit PBKDF2 (nur Standardbibliothek, kein bcrypt nötig)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, expected_hash_hex):
    _, computed_hash = hash_password(password, salt_hex)
    return secrets.compare_digest(computed_hash, expected_hash_hex)


def create_calendar_for_user(conn, user_id, calendar_name):
    cal_id = str(uuid.uuid4())
    token = secrets.token_hex(32)  # 256 Bit -- praktisch nicht erratbar
    conn.execute(
        "INSERT INTO calendars (id, owner_user_id, name, share_token) VALUES (?, ?, ?, ?)",
        (cal_id, user_id, calendar_name, token)
    )
    return cal_id


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def decode_mime_words(s):
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def escape_ics(text):
    if not text:
        return ""
    return (text.replace("\\", "\\\\")
                .replace(";", "\\;")
                .replace(",", "\\,")
                .replace("\n", "\\n"))


def to_ics_datetime(iso_str):
    dt = datetime.fromisoformat(iso_str)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def generate_ics(calendar_name, events):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//MeineApp//MailKalender//DE",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{escape_ics(calendar_name)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]
    for ev in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev['id']}@mailkalender")
        lines.append(f"DTSTAMP:{to_ics_datetime(ev['created_at'])}")
        lines.append(f"SUMMARY:{escape_ics(ev['title'])}")
        if ev["description"]:
            lines.append(f"DESCRIPTION:{escape_ics(ev['description'])}")
        if ev["location"]:
            lines.append(f"LOCATION:{escape_ics(ev['location'])}")
        if ev["all_day"]:
            start_date = ev["start_utc"][:10].replace("-", "")
            end_date = ev["end_utc"][:10].replace("-", "")
            lines.append(f"DTSTART;VALUE=DATE:{start_date}")
            lines.append(f"DTEND;VALUE=DATE:{end_date}")
        else:
            lines.append(f"DTSTART:{to_ics_datetime(ev['start_utc'])}")
            lines.append(f"DTEND:{to_ics_datetime(ev['end_utc'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):

    server_version = "MailKalenderApp/2.0"

    # -------- Hilfsmethoden --------

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_with_cookie(self, obj, status=200, token=None, clear=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if token:
            self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
        if clear:
            self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _serve_static_file(self, rel_path):
        if rel_path == "" or rel_path == "/":
            rel_path = "index.html"
        rel_path = rel_path.lstrip("/")
        if rel_path not in ALLOWED_STATIC_FILES:
            self._send_text("Nicht gefunden", 404)
            return
        full_path = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if not os.path.isfile(full_path):
            self._send_text("Nicht gefunden", 404)
            return
        ext = os.path.splitext(full_path)[1].lower()
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        ctype = content_types.get(ext, "application/octet-stream")
        with open(full_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_kiglobal_site(self):
        """Öffentliche KI-Global-Website (kiglobal-website/index.html)."""
        full_path = os.path.join(BASE_DIR, "kiglobal-website", "index.html")
        if not os.path.isfile(full_path):
            self._send_text("Nicht gefunden", 404)
            return
        with open(full_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def _get_session_token(self):
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[len("session="):]
        return None

    def _get_current_user(self):
        """Gibt die eingeloggte Nutzerzeile zurück, oder None."""
        token = self._get_session_token()
        if not token:
            return None
        conn = get_db()
        row = conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""", (token,)
        ).fetchone()
        conn.close()
        return row

    # -------- Routing --------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            # Öffentlich (kein Login nötig)
            if path == "/api/auth/status":
                return self.get_auth_status()
            if path.startswith("/calendar/") and path.endswith(".ics"):
                token = path[len("/calendar/"):-len(".ics")]
                return self.get_ics_feed(token)
            if path.startswith("/join/"):
                token = path[len("/join/"):]
                return self.get_join_page(token)
            if path in ("/kiglobal", "/kiglobal/"):
                return self._serve_kiglobal_site()
            if not path.startswith("/api/"):
                return self._serve_static_file(path)

            # Ab hier: Login erforderlich
            user = self._get_current_user()
            if not user:
                return self._send_json({"error": "not_authenticated"}, 401)

            if path == "/api/account":
                return self.get_account(user)
            if path == "/api/mail/list":
                return self.get_mail_list(user, query)
            if path == "/api/mail/read":
                return self.get_mail_read(user, query)
            if path == "/api/calendar/info":
                return self.get_calendar_info(user)
            if path == "/api/calendar/events":
                return self.get_calendar_events(user)
            if path == "/api/game/highscore":
                return self.get_highscore(user)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/auth/register":
                return self.post_auth_register()
            if path == "/api/auth/login":
                return self.post_auth_login()
            if path == "/api/auth/logout":
                return self.post_auth_logout()

            user = self._get_current_user()
            if not user:
                return self._send_json({"error": "not_authenticated"}, 401)

            if path == "/api/account":
                return self.post_account(user)
            if path == "/api/mail/send":
                return self.post_mail_send(user)
            if path == "/api/calendar/events":
                return self.post_calendar_event(user)
            if path == "/api/calendar/link":
                return self.post_calendar_link(user)
            if path == "/api/game/highscore":
                return self.post_highscore(user)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            user = self._get_current_user()
            if not user:
                return self._send_json({"error": "not_authenticated"}, 401)
            if path.startswith("/api/calendar/events/"):
                event_id = path.split("/")[-1]
                return self.put_calendar_event(user, event_id)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            user = self._get_current_user()
            if not user:
                return self._send_json({"error": "not_authenticated"}, 401)
            if path.startswith("/api/calendar/events/"):
                event_id = path.split("/")[-1]
                return self.delete_calendar_event(user, event_id)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # -------- Login / Registrierung --------

    def get_auth_status(self):
        user = self._get_current_user()
        self._send_json({
            "logged_in": user is not None,
            "username": user["username"] if user else None,
        })

    def post_auth_register(self):
        data = self._read_json_body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or len(password) < 4:
            return self._send_json({"error": "Benutzername und ein Passwort (mind. 4 Zeichen) erforderlich"}, 400)

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            return self._send_json({"error": "Dieser Benutzername ist bereits vergeben"}, 400)

        user_id = str(uuid.uuid4())
        salt_hex, hash_hex = hash_password(password)
        token = secrets.token_hex(32)
        with DB_LOCK:
            conn.execute(
                "INSERT INTO users (id, username, salt, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, salt_hex, hash_hex, datetime.now(timezone.utc).isoformat())
            )
            create_calendar_for_user(conn, user_id, f"{username}s Kalender")
            conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                         (token, user_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        conn.close()
        self._send_json_with_cookie({"ok": True, "username": username}, token=token)

    def post_auth_login(self):
        data = self._read_json_body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user or not verify_password(password, user["salt"], user["password_hash"]):
            conn.close()
            return self._send_json({"error": "Benutzername oder Passwort falsch"}, 401)

        token = secrets.token_hex(32)
        with DB_LOCK:
            conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                         (token, user["id"], datetime.now(timezone.utc).isoformat()))
            conn.commit()
        conn.close()
        self._send_json_with_cookie({"ok": True, "username": user["username"]}, token=token)

    def post_auth_logout(self):
        token = self._get_session_token()
        if token:
            with DB_LOCK:
                conn = get_db()
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
                conn.close()
        self._send_json_with_cookie({"ok": True}, clear=True)

    # -------- Konto / E-Mail-Zugangsdaten (pro Nutzer) --------

    def get_account(self, user):
        conn = get_db()
        row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user["id"],)).fetchone()
        conn.close()
        if not row:
            return self._send_json({"configured": False})
        data = dict(row)
        data["configured"] = True
        data.pop("password", None)
        self._send_json(data)

    def post_account(self, user):
        data = self._read_json_body()
        required = ["email", "password", "imap_server", "imap_port", "smtp_server", "smtp_port"]
        for r in required:
            if r not in data or data[r] in (None, ""):
                return self._send_json({"error": f"Feld '{r}' fehlt"}, 400)
        with DB_LOCK:
            conn = get_db()
            conn.execute("DELETE FROM accounts WHERE user_id = ?", (user["id"],))
            conn.execute(
                """INSERT INTO accounts (user_id, name, email, password, imap_server, imap_port, smtp_server, smtp_port)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user["id"], data.get("name", ""), data["email"], data["password"],
                 data["imap_server"], int(data["imap_port"]),
                 data["smtp_server"], int(data["smtp_port"]))
            )
            conn.commit()
            conn.close()
        self._send_json({"ok": True})

    def _get_account_row(self, user):
        conn = get_db()
        row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user["id"],)).fetchone()
        conn.close()
        return row

    # -------- E-Mail: Abrufen/Lesen/Senden --------

    def get_mail_list(self, user, query):
        account = self._get_account_row(user)
        if not account:
            return self._send_json({"error": "Kein E-Mail-Konto eingerichtet"}, 400)

        limit = int(query.get("limit", ["30"])[0])

        imap = imaplib.IMAP4_SSL(account["imap_server"], account["imap_port"])
        try:
            imap.login(account["email"], account["password"])
            imap.select("INBOX")
            status, data = imap.search(None, "ALL")
            uids = data[0].split()
            uids = uids[-limit:]
            uids.reverse()

            results = []
            for uid in uids:
                status, msg_data = imap.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if status != "OK":
                    continue
                raw_header = msg_data[0][1]
                msg = email.message_from_bytes(raw_header)
                results.append({
                    "uid": uid.decode(),
                    "from": decode_mime_words(msg.get("From", "")),
                    "subject": decode_mime_words(msg.get("Subject", "(kein Betreff)")),
                    "date": msg.get("Date", ""),
                })
            self._send_json({"emails": results})
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def get_mail_read(self, user, query):
        account = self._get_account_row(user)
        if not account:
            return self._send_json({"error": "Kein E-Mail-Konto eingerichtet"}, 400)
        uid = query.get("uid", [None])[0]
        if not uid:
            return self._send_json({"error": "uid fehlt"}, 400)

        imap = imaplib.IMAP4_SSL(account["imap_server"], account["imap_port"])
        try:
            imap.login(account["email"], account["password"])
            imap.select("INBOX")
            status, msg_data = imap.fetch(uid.encode(), "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                return self._send_json({"error": "E-Mail nicht gefunden"}, 404)
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            body = self._extract_body(msg)
            self._send_json({
                "uid": uid,
                "from": decode_mime_words(msg.get("From", "")),
                "to": decode_mime_words(msg.get("To", "")),
                "subject": decode_mime_words(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
                "body": body,
            })
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _extract_body(self, msg):
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if ctype == "text/plain" and "attachment" not in disp:
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        return payload.decode(charset, errors="replace")
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        return "(HTML-Mail, reiner Text nicht verfügbar)\n\n" + payload.decode(charset, errors="replace")
            return "(Kein lesbarer Inhalt gefunden)"
        else:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload is not None:
                return payload.decode(charset, errors="replace")
            return msg.get_payload()

    def post_mail_send(self, user):
        account = self._get_account_row(user)
        if not account:
            return self._send_json({"error": "Kein E-Mail-Konto eingerichtet"}, 400)
        data = self._read_json_body()
        to_addr = data.get("to")
        subject = data.get("subject", "")
        body = data.get("body", "")
        in_reply_to = data.get("in_reply_to")

        if not to_addr or not body.strip():
            return self._send_json({"error": "Empfänger und Text erforderlich"}, 400)

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        sender_name = account["name"] or ""
        msg["From"] = formataddr((sender_name, account["email"])) if sender_name else account["email"]
        msg["To"] = to_addr
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        context = ssl.create_default_context()
        with smtplib.SMTP(account["smtp_server"], account["smtp_port"]) as server:
            server.starttls(context=context)
            server.login(account["email"], account["password"])
            server.sendmail(account["email"], [to_addr], msg.as_string())

        self._send_json({"ok": True})

    # -------- Kalender --------

    def _get_own_calendar(self, conn, user_id):
        return conn.execute("SELECT * FROM calendars WHERE owner_user_id = ?", (user_id,)).fetchone()

    def get_calendar_info(self, user):
        conn = get_db()
        own_cal = self._get_own_calendar(conn, user["id"])
        host = self.headers.get("Host", f"localhost:{PORT}")

        linked = conn.execute(
            """SELECT c.owner_user_id, c.name, u.username AS owner_username
               FROM calendar_access ca
               JOIN calendars c ON c.id = ca.calendar_id
               JOIN users u ON u.id = c.owner_user_id
               WHERE ca.viewer_user_id = ?""",
            (user["id"],)
        ).fetchall()
        conn.close()

        self._send_json({
            "id": own_cal["id"],
            "name": own_cal["name"],
            "share_token": own_cal["share_token"],
            "ics_link": f"https://{host}/calendar/{own_cal['share_token']}.ics",
            "webcal_link": f"webcal://{host}/calendar/{own_cal['share_token']}.ics",
            "join_link": f"https://{host}/join/{own_cal['share_token']}",
            "linked_calendars": [dict(row) for row in linked],
        })

    def get_calendar_events(self, user):
        conn = get_db()
        own_cal = self._get_own_calendar(conn, user["id"])

        all_events = []
        own_rows = conn.execute(
            "SELECT * FROM events WHERE calendar_id = ? ORDER BY start_utc ASC", (own_cal["id"],)
        ).fetchall()
        for r in own_rows:
            e = dict(r)
            e["all_day"] = bool(e["all_day"])
            e["owner_username"] = user["username"]
            e["is_own"] = True
            all_events.append(e)

        linked = conn.execute(
            """SELECT c.id AS calendar_id, u.username AS owner_username
               FROM calendar_access ca
               JOIN calendars c ON c.id = ca.calendar_id
               JOIN users u ON u.id = c.owner_user_id
               WHERE ca.viewer_user_id = ?""",
            (user["id"],)
        ).fetchall()
        for link in linked:
            rows = conn.execute(
                "SELECT * FROM events WHERE calendar_id = ? ORDER BY start_utc ASC", (link["calendar_id"],)
            ).fetchall()
            for r in rows:
                e = dict(r)
                e["all_day"] = bool(e["all_day"])
                e["owner_username"] = link["owner_username"]
                e["is_own"] = False
                all_events.append(e)

        conn.close()
        all_events.sort(key=lambda e: e["start_utc"])
        self._send_json({"events": all_events})

    def post_calendar_event(self, user):
        conn = get_db()
        own_cal = self._get_own_calendar(conn, user["id"])
        conn.close()

        data = self._read_json_body()
        title = data.get("title", "").strip()
        start_utc = data.get("start")
        end_utc = data.get("end")
        if not title or not start_utc or not end_utc:
            return self._send_json({"error": "title, start und end sind erforderlich"}, 400)

        event_id = str(uuid.uuid4())
        with DB_LOCK:
            conn = get_db()
            conn.execute(
                """INSERT INTO events (id, calendar_id, title, description, location,
                   start_utc, end_utc, all_day, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, own_cal["id"], title, data.get("description", ""), data.get("location", ""),
                 start_utc, end_utc, 1 if data.get("all_day") else 0,
                 datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        self._send_json({"ok": True, "id": event_id})

    def _event_belongs_to_user(self, conn, event_id, user_id):
        row = conn.execute(
            """SELECT e.id FROM events e
               JOIN calendars c ON c.id = e.calendar_id
               WHERE e.id = ? AND c.owner_user_id = ?""",
            (event_id, user_id)
        ).fetchone()
        return row is not None

    def put_calendar_event(self, user, event_id):
        conn = get_db()
        if not self._event_belongs_to_user(conn, event_id, user["id"]):
            conn.close()
            return self._send_json({"error": "Du kannst nur eigene Termine bearbeiten"}, 403)
        conn.close()

        data = self._read_json_body()
        fields = []
        values = []
        for key in ["title", "description", "location", "start_utc", "end_utc", "all_day"]:
            api_key = "start" if key == "start_utc" else "end" if key == "end_utc" else key
            if api_key in data:
                fields.append(f"{key} = ?")
                val = data[api_key]
                if key == "all_day":
                    val = 1 if val else 0
                values.append(val)
        if not fields:
            return self._send_json({"error": "Keine Felder zum Aktualisieren"}, 400)
        values.append(event_id)
        with DB_LOCK:
            conn = get_db()
            conn.execute(f"UPDATE events SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
            conn.close()
        self._send_json({"ok": True})

    def delete_calendar_event(self, user, event_id):
        conn = get_db()
        if not self._event_belongs_to_user(conn, event_id, user["id"]):
            conn.close()
            return self._send_json({"error": "Du kannst nur eigene Termine löschen"}, 403)
        with DB_LOCK:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
        conn.close()
        self._send_json({"ok": True})

    def post_calendar_link(self, user):
        data = self._read_json_body()
        token = (data.get("token") or "").strip()
        conn = get_db()
        cal = conn.execute("SELECT * FROM calendars WHERE share_token = ?", (token,)).fetchone()
        if not cal:
            conn.close()
            return self._send_json({"error": "Ungültiger Kalender-Link"}, 404)
        if cal["owner_user_id"] == user["id"]:
            conn.close()
            return self._send_json({"error": "Das ist dein eigener Kalender"}, 400)

        owner = conn.execute("SELECT username FROM users WHERE id = ?", (cal["owner_user_id"],)).fetchone()
        with DB_LOCK:
            conn.execute(
                "INSERT OR IGNORE INTO calendar_access (viewer_user_id, calendar_id, granted_at) VALUES (?, ?, ?)",
                (user["id"], cal["id"], datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        conn.close()
        self._send_json({"ok": True, "owner_username": owner["username"]})

    def get_highscore(self, user):
        conn = get_db()
        own = conn.execute("SELECT high_score FROM game_scores WHERE user_id = ?", (user["id"],)).fetchone()

        partners = conn.execute(
            """SELECT DISTINCT u.username, gs.high_score
               FROM calendar_access ca
               JOIN calendars c ON c.id = ca.calendar_id
               JOIN users u ON u.id = c.owner_user_id
               LEFT JOIN game_scores gs ON gs.user_id = u.id
               WHERE ca.viewer_user_id = ?""",
            (user["id"],)
        ).fetchall()
        conn.close()
        self._send_json({
            "high_score": own["high_score"] if own else 0,
            "partners": [{"username": p["username"], "high_score": p["high_score"] or 0} for p in partners],
        })

    def post_highscore(self, user):
        data = self._read_json_body()
        score = int(data.get("score", 0))
        with DB_LOCK:
            conn = get_db()
            existing = conn.execute("SELECT high_score FROM game_scores WHERE user_id = ?", (user["id"],)).fetchone()
            if existing:
                if score > existing["high_score"]:
                    conn.execute("UPDATE game_scores SET high_score = ? WHERE user_id = ?", (score, user["id"]))
            else:
                conn.execute("INSERT INTO game_scores (user_id, high_score) VALUES (?, ?)", (user["id"], score))
            conn.commit()
            conn.close()
        self._send_json({"ok": True})

    def get_ics_feed(self, token):
        conn = get_db()
        cal = conn.execute("SELECT * FROM calendars WHERE share_token = ?", (token,)).fetchone()
        if not cal:
            conn.close()
            return self._send_text("Kalender nicht gefunden", 404)
        rows = conn.execute(
            "SELECT * FROM events WHERE calendar_id = ? ORDER BY start_utc ASC",
            (cal["id"],)
        ).fetchall()
        conn.close()
        events = [dict(r) for r in rows]
        ics = generate_ics(cal["name"], events)
        self._send_text(ics, 200, "text/calendar; charset=utf-8")

    def get_join_page(self, token):
        conn = get_db()
        cal = conn.execute("SELECT * FROM calendars WHERE share_token = ?", (token,)).fetchone()
        owner = None
        if cal:
            owner = conn.execute("SELECT username FROM users WHERE id = ?", (cal["owner_user_id"],)).fetchone()
        conn.close()
        if not cal:
            return self._send_text("Kalender nicht gefunden", 404)

        host = self.headers.get("Host", f"localhost:{PORT}")
        webcal_link = f"webcal://{host}/calendar/{cal['share_token']}.ics"
        https_link = f"https://{host}/calendar/{cal['share_token']}.ics"
        owner_name = owner["username"] if owner else "jemand"

        html = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalender abonnieren</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@600&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  body {{ font-family: "Inter", -apple-system, sans-serif; text-align:center; padding: 48px 22px;
         background:#11131c; color:#f3efe6; margin:0; }}
  .badge {{ width: 64px; height: 64px; border-radius: 50%; margin: 0 auto 18px;
           background: radial-gradient(circle at 32% 28%, #e7c583, #c9a25c 60%, #8a6a2f 100%);
           display:flex; align-items:center; justify-content:center; }}
  .badge span {{ font-family:"Fraunces",serif; font-weight:600; font-size:24px; color:#1a1306; }}
  h1 {{ font-size: 21px; margin: 0 0 6px; }}
  p {{ color:#9295ab; font-size: 14.5px; line-height:1.5; max-width: 360px; margin: 8px auto; }}
  a.button {{ display:block; margin: 20px auto 10px; padding: 14px 20px; background:#c9a25c; color:#1a1306;
    text-decoration:none; border-radius: 12px; font-size: 16px; font-weight: 700; max-width: 320px; }}
  a.button.secondary {{ background:#20233a; color:#f3efe6; border:1px solid #2c3046; }}
  p.hint {{ font-size: 12.5px; margin-top: 24px; word-break: break-all; color: #6d7086; }}
  #link-status {{ font-size: 14px; margin-top: 6px; min-height: 20px; }}
</style></head>
<body>
<div class="badge"><span>AK</span></div>
<h1>Kalender von {owner_name}</h1>
<p>Du wurdest eingeladen, diesen Kalender zu abonnieren. Neue und geänderte
Termine erscheinen automatisch.</p>

<a class="button" href="{webcal_link}">📅 Im iPhone-Kalender abonnieren</a>
<a class="button secondary" href="#" id="link-btn">🔗 In meiner App verknüpfen</a>
<div id="link-status"></div>

<p class="hint">Falls sich beim Abonnieren nichts öffnet: Einstellungen → Kalender →
Accounts → Account hinzufügen → Andere → Kalenderabo hinzufügen, und dort
diesen Link einfügen:<br>{https_link}</p>

<script>
const token = "{cal['share_token']}";
const statusEl = document.getElementById('link-status');
const linkBtn = document.getElementById('link-btn');

fetch('/api/auth/status').then(r => r.json()).then(status => {{
  if (!status.logged_in) {{
    linkBtn.textContent = '🔗 Zum Verknüpfen bitte in der App einloggen';
    linkBtn.href = '/';
  }}
}});

linkBtn.addEventListener('click', function(e) {{
  e.preventDefault();
  fetch('/api/auth/status').then(r => r.json()).then(status => {{
    if (!status.logged_in) {{ window.location.href = '/'; return; }}
    fetch('/api/calendar/link', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ token: token }})
    }}).then(r => r.json()).then(data => {{
      if (data.ok) {{
        statusEl.textContent = '✓ Verknüpft! Öffne die App, um beide Kalender zusammen zu sehen.';
        statusEl.style.color = '#6fcf97';
      }} else {{
        statusEl.textContent = data.error || 'Fehler beim Verknüpfen';
        statusEl.style.color = '#ff6b6b';
      }}
    }});
  }});
}});
</script>
</body></html>"""
        self._send_text(html, 200, "text/html; charset=utf-8")


def main():
    init_db()
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Server läuft auf http://0.0.0.0:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
