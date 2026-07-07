# -*- coding: utf-8 -*-
"""
Mail & Kalender Web-App (Backend)
==================================
Läuft mit reinem Python (keine Zusatzpakete nötig).
Bietet:
  - E-Mails empfangen/lesen (IMAP) und beantworten (SMTP)
  - Einen eigenen Kalender mit Terminen
  - Einen Kalender-Abo-Link (webcal://...ics), den man z.B. der Ehefrau
    schicken kann. Beim Antippen erscheint der Kalender automatisch
    in der echten iPhone-Kalender-App und aktualisiert sich von selbst.

Start:
    python3 server.py
Der Server läuft dann standardmäßig auf Port 8000.
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
from email.utils import parseaddr, formataddr
from email.header import decode_header, make_header
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Statische Dateien liegen jetzt direkt im Hauptverzeichnis (keine Unterordner
# nötig -- praktisch, wenn man Dateien manuell über die GitHub-Weboberfläche
# hochlädt). Nur diese konkreten Dateien dürfen ausgeliefert werden, damit
# server.py oder die Datenbank niemals versehentlich ausgeliefert werden.
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
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        name TEXT, email TEXT, password TEXT,
        imap_server TEXT, imap_port INTEGER,
        smtp_server TEXT, smtp_port INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS calendars (
        id TEXT PRIMARY KEY,
        name TEXT,
        share_token TEXT UNIQUE
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
    c.execute("""CREATE TABLE IF NOT EXISTS auth_user (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT,
        salt TEXT,
        password_hash TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        created_at TEXT
    )""")
    conn.commit()

    c.execute("SELECT id FROM calendars LIMIT 1")
    if not c.fetchone():
        cal_id = str(uuid.uuid4())
        token = uuid.uuid4().hex
        c.execute("INSERT INTO calendars (id, name, share_token) VALUES (?, ?, ?)",
                   (cal_id, "Unser Kalender", token))
        conn.commit()
    conn.close()


def get_default_calendar():
    conn = get_db()
    row = conn.execute("SELECT * FROM calendars LIMIT 1").fetchone()
    conn.close()
    return row


def hash_password(password, salt_hex=None):
    """Erzeugt einen sicheren Passwort-Hash mit PBKDF2 (nur Standardbibliothek)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, expected_hash_hex):
    _, computed_hash = hash_password(password, salt_hex)
    return secrets.compare_digest(computed_hash, expected_hash_hex)


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
    """ 'YYYY-MM-DDTHH:MM' -> 'YYYYMMDDTHHMMSSZ' (wir behandeln alle Zeiten als UTC) """
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

    server_version = "MailKalenderApp/1.0"

    # -------- Hilfsmethoden --------

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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

    def log_message(self, fmt, *args):
        pass  # ruhiger Server, keine Konsolen-Flut

    def _get_session_token(self):
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[len("session="):]
        return None

    def _is_authenticated(self):
        token = self._get_session_token()
        if not token:
            return False
        conn = get_db()
        row = conn.execute("SELECT token FROM sessions WHERE token = ?", (token,)).fetchone()
        conn.close()
        return row is not None

    def _set_session_cookie(self, token):
        self.send_header(
            "Set-Cookie",
            f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
        )

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; Max-Age=0")

    def _send_json_with_cookie(self, obj, status=200, token=None, clear=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if token:
            self._set_session_cookie(token)
        if clear:
            self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    # -------- Routing --------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            # Öffentlich zugänglich: Auth-Status, ICS-Feed, Join-Seite, Frontend-Dateien
            if path == "/api/auth/status":
                return self.get_auth_status()
            if path.startswith("/calendar/") and path.endswith(".ics"):
                token = path[len("/calendar/"):-len(".ics")]
                return self.get_ics_feed(token)
            if path.startswith("/join/"):
                token = path[len("/join/"):]
                return self.get_join_page(token)
            if not path.startswith("/api/"):
                return self._serve_static_file(path)

            # Ab hier: alle /api/*-Routen erfordern Login
            if not self._is_authenticated():
                return self._send_json({"error": "not_authenticated"}, 401)

            if path == "/api/account":
                return self.get_account()
            if path == "/api/mail/list":
                return self.get_mail_list(query)
            if path == "/api/mail/read":
                return self.get_mail_read(query)
            if path == "/api/calendar/info":
                return self.get_calendar_info()
            if path == "/api/calendar/events":
                return self.get_calendar_events(query)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            # Öffentlich zugänglich: Registrierung und Login selbst
            if path == "/api/auth/register":
                return self.post_auth_register()
            if path == "/api/auth/login":
                return self.post_auth_login()
            if path == "/api/auth/logout":
                return self.post_auth_logout()

            # Ab hier: Login erforderlich
            if not self._is_authenticated():
                return self._send_json({"error": "not_authenticated"}, 401)

            if path == "/api/account":
                return self.post_account()
            if path == "/api/mail/send":
                return self.post_mail_send()
            if path == "/api/calendar/events":
                return self.post_calendar_event()
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if not self._is_authenticated():
                return self._send_json({"error": "not_authenticated"}, 401)
            if path.startswith("/api/calendar/events/"):
                event_id = path.split("/")[-1]
                return self.put_calendar_event(event_id)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if not self._is_authenticated():
                return self._send_json({"error": "not_authenticated"}, 401)
            if path.startswith("/api/calendar/events/"):
                event_id = path.split("/")[-1]
                return self.delete_calendar_event(event_id)
            self._send_json({"error": "Unbekannter Endpunkt"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # -------- Login / Registrierung --------

    def get_auth_status(self):
        conn = get_db()
        user = conn.execute("SELECT username FROM auth_user WHERE id = 1").fetchone()
        conn.close()
        registered = user is not None
        logged_in = self._is_authenticated()
        self._send_json({
            "registered": registered,
            "logged_in": logged_in,
            "username": user["username"] if user else None,
        })

    def post_auth_register(self):
        conn = get_db()
        existing = conn.execute("SELECT id FROM auth_user WHERE id = 1").fetchone()
        if existing:
            conn.close()
            return self._send_json({"error": "Es existiert bereits ein Konto. Bitte einloggen."}, 400)

        data = self._read_json_body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or len(password) < 4:
            conn.close()
            return self._send_json({"error": "Benutzername und ein Passwort (mind. 4 Zeichen) erforderlich"}, 400)

        salt_hex, hash_hex = hash_password(password)
        with DB_LOCK:
            conn.execute(
                "INSERT INTO auth_user (id, username, salt, password_hash) VALUES (1, ?, ?, ?)",
                (username, salt_hex, hash_hex)
            )
            token = secrets.token_hex(32)
            conn.execute("INSERT INTO sessions (token, created_at) VALUES (?, ?)",
                         (token, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        conn.close()
        self._send_json_with_cookie({"ok": True, "username": username}, token=token)

    def post_auth_login(self):
        data = self._read_json_body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""

        conn = get_db()
        user = conn.execute("SELECT * FROM auth_user WHERE id = 1").fetchone()
        if not user or user["username"] != username or not verify_password(password, user["salt"], user["password_hash"]):
            conn.close()
            return self._send_json({"error": "Benutzername oder Passwort falsch"}, 401)

        token = secrets.token_hex(32)
        with DB_LOCK:
            conn.execute("INSERT INTO sessions (token, created_at) VALUES (?, ?)",
                         (token, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        conn.close()
        self._send_json_with_cookie({"ok": True, "username": username}, token=token)

    def post_auth_logout(self):
        token = self._get_session_token()
        if token:
            with DB_LOCK:
                conn = get_db()
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
                conn.close()
        self._send_json_with_cookie({"ok": True}, clear=True)

    # -------- Konto / E-Mail-Zugangsdaten --------

    def get_account(self):
        conn = get_db()
        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        conn.close()
        if not row:
            return self._send_json({"configured": False})
        data = dict(row)
        data["configured"] = True
        data.pop("password", None)  # Passwort nicht ans Frontend zurückgeben
        self._send_json(data)

    def post_account(self):
        data = self._read_json_body()
        required = ["email", "password", "imap_server", "imap_port", "smtp_server", "smtp_port"]
        for r in required:
            if r not in data or data[r] in (None, ""):
                return self._send_json({"error": f"Feld '{r}' fehlt"}, 400)
        with DB_LOCK:
            conn = get_db()
            conn.execute("DELETE FROM account WHERE id = 1")
            conn.execute(
                """INSERT INTO account (id, name, email, password, imap_server, imap_port, smtp_server, smtp_port)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                (data.get("name", ""), data["email"], data["password"],
                 data["imap_server"], int(data["imap_port"]),
                 data["smtp_server"], int(data["smtp_port"]))
            )
            conn.commit()
            conn.close()
        self._send_json({"ok": True})

    def _get_account_row(self):
        conn = get_db()
        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        conn.close()
        return row

    # -------- E-Mail: Abrufen/Lesen/Senden --------

    def get_mail_list(self, query):
        account = self._get_account_row()
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

    def get_mail_read(self, query):
        account = self._get_account_row()
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

    def post_mail_send(self):
        account = self._get_account_row()
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

    def get_calendar_info(self):
        cal = get_default_calendar()
        host = self.headers.get("Host", f"localhost:{PORT}")
        https_link = f"https://{host}/calendar/{cal['share_token']}.ics"
        webcal_link = f"webcal://{host}/calendar/{cal['share_token']}.ics"
        join_link = f"https://{host}/join/{cal['share_token']}"
        self._send_json({
            "id": cal["id"],
            "name": cal["name"],
            "share_token": cal["share_token"],
            "ics_link": https_link,
            "webcal_link": webcal_link,
            "join_link": join_link,
        })

    def get_calendar_events(self, query):
        cal = get_default_calendar()
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM events WHERE calendar_id = ? ORDER BY start_utc ASC",
            (cal["id"],)
        ).fetchall()
        conn.close()
        events = [dict(r) for r in rows]
        for e in events:
            e["all_day"] = bool(e["all_day"])
        self._send_json({"events": events})

    def post_calendar_event(self):
        cal = get_default_calendar()
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
                (event_id, cal["id"], title, data.get("description", ""), data.get("location", ""),
                 start_utc, end_utc, 1 if data.get("all_day") else 0,
                 datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        self._send_json({"ok": True, "id": event_id})

    def put_calendar_event(self, event_id):
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

    def delete_calendar_event(self, event_id):
        with DB_LOCK:
            conn = get_db()
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
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
        conn.close()
        if not cal:
            return self._send_text("Kalender nicht gefunden", 404)
        host = self.headers.get("Host", f"localhost:{PORT}")
        webcal_link = f"webcal://{host}/calendar/{cal['share_token']}.ics"
        https_link = f"https://{host}/calendar/{cal['share_token']}.ics"
        html = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalender abonnieren</title>
<style>
body {{ font-family: -apple-system, sans-serif; text-align:center; padding: 40px 20px; background:#f5f5f7; }}
h1 {{ font-size: 22px; }}
a.button {{ display:inline-block; margin-top: 24px; padding: 14px 28px; background:#0a84ff; color:white;
  text-decoration:none; border-radius: 12px; font-size: 17px; font-weight: 600; }}
p.hint {{ color:#666; font-size: 14px; margin-top: 28px; word-break: break-all; }}
</style></head>
<body>
<h1>📅 {cal['name']}</h1>
<p>Du wurdest eingeladen, diesen Kalender auf deinem iPhone zu abonnieren.
Neue und geänderte Termine erscheinen automatisch.</p>
<a class="button" href="{webcal_link}">Kalender abonnieren</a>
<p class="hint">Falls sich nichts öffnet: Einstellungen → Kalender → Accounts →
Account hinzufügen → Andere → Kalenderabo hinzufügen, und dort diesen Link einfügen:<br>{https_link}</p>
</body></html>"""
        self._send_text(html, 200, "text/html; charset=utf-8")


def main():
    init_db()
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Server läuft auf http://0.0.0.0:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
