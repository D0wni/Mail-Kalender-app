# -*- coding: utf-8 -*-
"""
TechNova Online-Shop (Backend)
==============================
Läuft mit reinem Python (keine Zusatzpakete nötig) -- gleiches Muster
wie die Mail & Kalender App in diesem Repository.

Was dieser Server macht:
  - Liefert die Shop-Website (index.html) und den Admin-Bereich (admin.html) aus.
  - Speichert Produkte und Bestellungen in einer SQLite-Datenbank (shop.db).
  - Admin-Login mit sicher gehashtem Passwort (PBKDF2, wie in der Mail-App).
    Beim allerersten Aufruf des Admin-Bereichs legt man das Passwort fest.
  - Chatbot unter /api/chat: antwortet regelbasiert (kostenlos) auf Fragen zu
    Produkten, Preisen, Versand usw. Ist die Umgebungsvariable
    ANTHROPIC_API_KEY gesetzt, antwortet stattdessen automatisch eine echte
    KI (Claude) -- ohne dass am Code etwas geändert werden muss.

Start:
    python3 server.py          (im Ordner shop/)
    python3 shop/server.py     (im Hauptordner des Repos)
"""

import http.server
import json
import sqlite3
import os
import ssl
import base64
import hashlib
import secrets
import re
import urllib.request
import urllib.error
from urllib.parse import urlparse
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = BASE_DIR
ALLOWED_STATIC_FILES = {"index.html", "admin.html"}
DB_FILE = os.path.join(BASE_DIR, "shop.db")
PORT = int(os.environ.get("PORT", 8000))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB pro Produktbild (als Data-URL)


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
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS products (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        category    TEXT NOT NULL CHECK (category IN ('phone', 'laptop')),
        price_cents INTEGER NOT NULL,
        tagline     TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        specs       TEXT NOT NULL DEFAULT '{}',
        image       TEXT NOT NULL DEFAULT '',
        is_featured INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_name TEXT NOT NULL,
        email         TEXT NOT NULL,
        address       TEXT NOT NULL,
        note          TEXT NOT NULL DEFAULT '',
        total_cents   INTEGER NOT NULL,
        status        TEXT NOT NULL DEFAULT 'neu',
        created_at    TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS order_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        product_id   INTEGER,
        product_name TEXT NOT NULL,
        price_cents  INTEGER NOT NULL,
        quantity     INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
        token      TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    seed_products(conn)
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Passwort-Hashing (wie in der Mail-App: PBKDF2, nur Standardbibliothek)
# ---------------------------------------------------------------------------

def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, expected_hash_hex):
    _, computed_hash = hash_password(password, salt_hex)
    return secrets.compare_digest(computed_hash, expected_hash_hex)


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


# ---------------------------------------------------------------------------
# Beispiel-Produkte mit selbst gezeichneten SVG-Bildern (keine Copyright-
# Probleme, kein Internet nötig). Der Admin kann sie ersetzen oder löschen.
# ---------------------------------------------------------------------------

def svg_to_data_url(svg):
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + encoded


def phone_svg(color_a, color_b, label):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600">
<defs>
  <linearGradient id="scr" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{color_a}"/><stop offset="1" stop-color="{color_b}"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.45" r="0.6">
    <stop offset="0" stop-color="{color_a}" stop-opacity="0.25"/>
    <stop offset="1" stop-color="{color_a}" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="600" height="600" fill="url(#glow)"/>
<rect x="205" y="70" width="190" height="400" rx="38" fill="#1d1d1f"/>
<rect x="214" y="79" width="172" height="382" rx="30" fill="url(#scr)"/>
<rect x="268" y="94" width="64" height="18" rx="9" fill="#0b0b0c"/>
<circle cx="318" cy="103" r="4" fill="#2a2a2e"/>
<path d="M214 109 Q300 60 386 109 L386 79 Q300 65 214 79 Z" fill="#ffffff" opacity="0.10"/>
<rect x="214" y="79" width="60" height="382" fill="#ffffff" opacity="0.07"/>
<text x="300" y="300" font-family="-apple-system,Helvetica,Arial,sans-serif" font-size="26"
      font-weight="600" fill="#ffffff" text-anchor="middle" opacity="0.92">{label}</text>
<rect x="264" y="440" width="72" height="6" rx="3" fill="#ffffff" opacity="0.85"/>
</svg>"""


def laptop_svg(color_a, color_b, label):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600">
<defs>
  <linearGradient id="scr" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{color_a}"/><stop offset="1" stop-color="{color_b}"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.5" r="0.6">
    <stop offset="0" stop-color="{color_a}" stop-opacity="0.22"/>
    <stop offset="1" stop-color="{color_a}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="base" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#e8e8ed"/><stop offset="1" stop-color="#b9b9c0"/>
  </linearGradient>
</defs>
<rect width="600" height="600" fill="url(#glow)"/>
<rect x="140" y="150" width="320" height="212" rx="14" fill="#1d1d1f"/>
<rect x="152" y="162" width="296" height="188" rx="6" fill="url(#scr)"/>
<circle cx="300" cy="157" r="2.5" fill="#3a3a3e"/>
<path d="M152 162 L448 162 L448 210 Q300 180 152 230 Z" fill="#ffffff" opacity="0.10"/>
<text x="300" y="268" font-family="-apple-system,Helvetica,Arial,sans-serif" font-size="24"
      font-weight="600" fill="#ffffff" text-anchor="middle" opacity="0.92">{label}</text>
<path d="M110 362 L490 362 L470 390 Q460 398 445 398 L155 398 Q140 398 130 390 Z" fill="url(#base)"/>
<rect x="262" y="362" width="76" height="10" rx="5" fill="#9a9aa2"/>
</svg>"""


def seed_products(conn):
    count = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    if count > 0:
        return
    products = [
        {
            "name": "Nova X Pro", "category": "phone", "price_cents": 109900,
            "tagline": "Das neue Nova X Pro. Ein Sprung nach vorn.",
            "description": "Unser bisher leistungsstärkstes Smartphone: Titanrahmen, "
                           "brillantes OLED-Display und eine Kamera, die selbst nachts "
                           "gestochen scharfe Fotos macht.",
            "specs": {"Display": "6,7\" OLED, 120 Hz ProMotion", "Chip": "NovaChip N3 Pro",
                      "Kamera": "48 MP Triple-Kamera mit 5x Zoom", "Akku": "Bis zu 29 Std. Videowiedergabe",
                      "Speicher": "256 GB", "Material": "Titan, IP68 wasserdicht"},
            "image": svg_to_data_url(phone_svg("#5e5ce6", "#1b1464", "Nova X Pro")),
            "is_featured": 1,
        },
        {
            "name": "Nova X", "category": "phone", "price_cents": 84900,
            "tagline": "Stark. Schlank. Nova X.",
            "description": "Alles, was du liebst: großes OLED-Display, blitzschneller Chip "
                           "und ein Akku, der locker durch den Tag kommt.",
            "specs": {"Display": "6,1\" OLED, 60 Hz", "Chip": "NovaChip N3",
                      "Kamera": "48 MP Dual-Kamera", "Akku": "Bis zu 22 Std. Videowiedergabe",
                      "Speicher": "128 GB", "Material": "Aluminium, IP68 wasserdicht"},
            "image": svg_to_data_url(phone_svg("#0a84ff", "#003a75", "Nova X")),
            "is_featured": 0,
        },
        {
            "name": "Nova SE", "category": "phone", "price_cents": 54900,
            "tagline": "Viel Power. Kleiner Preis.",
            "description": "Der günstige Einstieg in die Nova-Welt -- mit dem gleichen Chip "
                           "wie das Nova X und einer erstaunlich guten Kamera.",
            "specs": {"Display": "6,1\" LCD", "Chip": "NovaChip N3",
                      "Kamera": "12 MP Kamera", "Akku": "Bis zu 18 Std. Videowiedergabe",
                      "Speicher": "128 GB", "Material": "Aluminium"},
            "image": svg_to_data_url(phone_svg("#ff9f0a", "#8a3d00", "Nova SE")),
            "is_featured": 0,
        },
        {
            "name": "NovaBook Pro 16", "category": "laptop", "price_cents": 219900,
            "tagline": "Ein Monster von einem Laptop.",
            "description": "Für Profis gebaut: riesiges 16\"-Display, extreme Leistung für "
                           "Videoschnitt und 3D -- und trotzdem den ganzen Tag Akku.",
            "specs": {"Display": "16,2\" Liquid-Display, 120 Hz", "Chip": "NovaChip M2 Max",
                      "Arbeitsspeicher": "32 GB", "Speicher": "1 TB SSD",
                      "Akku": "Bis zu 22 Std.", "Anschlüsse": "3x USB-C, HDMI, Kartenleser"},
            "image": svg_to_data_url(laptop_svg("#30d158", "#0b5c28", "NovaBook Pro 16")),
            "is_featured": 0,
        },
        {
            "name": "NovaBook Air 13", "category": "laptop", "price_cents": 109900,
            "tagline": "Federleicht. Überraschend stark.",
            "description": "Nur 1,2 kg leicht, lautlos ohne Lüfter -- perfekt für Uni, "
                           "Büro und unterwegs.",
            "specs": {"Display": "13,6\" Liquid-Display", "Chip": "NovaChip M2",
                      "Arbeitsspeicher": "16 GB", "Speicher": "256 GB SSD",
                      "Akku": "Bis zu 18 Std.", "Gewicht": "1,2 kg"},
            "image": svg_to_data_url(laptop_svg("#64d2ff", "#0a4a6e", "NovaBook Air 13")),
            "is_featured": 0,
        },
        {
            "name": "NovaBook 14", "category": "laptop", "price_cents": 139900,
            "tagline": "Der Allrounder für jeden Tag.",
            "description": "Die goldene Mitte: handliches 14\"-Display, viel Leistung und "
                           "genug Anschlüsse für alles, was du brauchst.",
            "specs": {"Display": "14,2\" Liquid-Display, 120 Hz", "Chip": "NovaChip M2 Pro",
                      "Arbeitsspeicher": "16 GB", "Speicher": "512 GB SSD",
                      "Akku": "Bis zu 20 Std.", "Anschlüsse": "2x USB-C, HDMI"},
            "image": svg_to_data_url(laptop_svg("#bf5af2", "#4a1070", "NovaBook 14")),
            "is_featured": 0,
        },
    ]
    for p in products:
        conn.execute(
            """INSERT INTO products (name, category, price_cents, tagline, description,
                                     specs, image, is_featured, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p["name"], p["category"], p["price_cents"], p["tagline"], p["description"],
             json.dumps(p["specs"], ensure_ascii=False), p["image"], p["is_featured"], now_iso()))
    conn.commit()


# ---------------------------------------------------------------------------
# Chatbot
# ---------------------------------------------------------------------------

SHOP_INFO = {
    "versand": "Wir versenden innerhalb von Deutschland kostenlos. Die Lieferzeit "
               "beträgt in der Regel 2-4 Werktage.",
    "bezahlung": "Aktuell bestellst du bequem auf Rechnung: Du gibst deine Bestellung "
                 "auf, wir melden uns per E-Mail mit den Zahlungsdetails.",
    "garantie": "Auf alle Geräte gibt es 2 Jahre gesetzliche Gewährleistung.",
    "rueckgabe": "Du kannst jede Bestellung innerhalb von 14 Tagen ohne Angabe von "
                 "Gründen zurückgeben.",
    "kontakt": "Du erreichst uns jederzeit hier im Chat oder per E-Mail an "
               "support@technova-shop.de.",
}


def format_price(cents):
    euros = cents / 100
    return ("%.2f" % euros).replace(".", ",") + " €"


def product_summary(p):
    specs = json.loads(p["specs"] or "{}")
    lines = [f"{p['name']} – {format_price(p['price_cents'])}"]
    for key, val in specs.items():
        lines.append(f"• {key}: {val}")
    return "\n".join(lines)


def chatbot_rules_answer(message, conn):
    """Kostenloser, regelbasierter Chatbot: erkennt Stichwörter und antwortet
    mit echten Daten aus der Produkt-Datenbank."""
    text = message.lower()
    products = conn.execute("SELECT * FROM products ORDER BY is_featured DESC, id").fetchall()

    # Erwähnt die Nachricht ein konkretes Produkt?
    mentioned = [p for p in products if p["name"].lower() in text]
    if not mentioned:
        # Auch Teilnamen erkennen (z.B. "x pro" oder "air")
        for p in products:
            words = [w for w in p["name"].lower().split() if len(w) > 1]
            if len(words) > 1 and all(w in text for w in words[1:]):
                mentioned.append(p)

    if any(w in text for w in ("hallo", "hi", "hey", "guten tag", "moin", "servus")) and len(text) < 30:
        return ("Hallo! 👋 Ich bin der TechNova-Assistent. Frag mich z.B. nach unseren "
                "Handys oder Laptops, nach Preisen, Versand oder Rückgabe.")

    if any(w in text for w in ("danke", "dank", "super", "perfekt")) and len(text) < 40:
        return "Sehr gerne! Wenn du noch Fragen hast, bin ich hier. 😊"

    if mentioned:
        p = mentioned[0]
        if any(w in text for w in ("preis", "kostet", "kosten", "teuer", "euro", "€")):
            return f"Das {p['name']} kostet {format_price(p['price_cents'])}."
        return f"Gerne! Hier die Daten zum {p['name']}:\n\n{product_summary(p)}\n\n{p['description']}"

    if any(w in text for w in ("versand", "liefer", "wie lange", "wann kommt")):
        return SHOP_INFO["versand"]
    if any(w in text for w in ("bezahl", "zahlung", "zahlen", "paypal", "rechnung", "kreditkarte")):
        return SHOP_INFO["bezahlung"]
    if any(w in text for w in ("garantie", "gewährleistung", "kaputt", "defekt")):
        return SHOP_INFO["garantie"]
    if any(w in text for w in ("rückgabe", "zurückgeben", "zurückschicken", "retoure", "umtausch", "widerruf")):
        return SHOP_INFO["rueckgabe"]
    if any(w in text for w in ("kontakt", "erreichen", "e-mail", "email", "telefon")):
        return SHOP_INFO["kontakt"]

    if any(w in text for w in ("günstig", "billig", "am wenigsten")):
        cheapest = min(products, key=lambda p: p["price_cents"], default=None)
        if cheapest:
            return (f"Unser günstigstes Gerät ist das {cheapest['name']} für "
                    f"{format_price(cheapest['price_cents'])}. {cheapest['tagline']}")

    if any(w in text for w in ("handy", "handys", "smartphone", "telefon")):
        phones = [p for p in products if p["category"] == "phone"]
        lines = [f"• {p['name']} – {format_price(p['price_cents'])}" for p in phones]
        return "Diese Handys haben wir gerade im Angebot:\n" + "\n".join(lines) + \
               "\n\nFrag mich gerne nach Details zu einem Modell!"
    if any(w in text for w in ("laptop", "laptops", "notebook", "rechner", "computer")):
        laptops = [p for p in products if p["category"] == "laptop"]
        lines = [f"• {p['name']} – {format_price(p['price_cents'])}" for p in laptops]
        return "Das sind unsere aktuellen Laptops:\n" + "\n".join(lines) + \
               "\n\nFrag mich gerne nach Details zu einem Modell!"

    if any(w in text for w in ("preis", "kostet", "kosten", "angebot", "produkte", "sortiment")):
        lines = [f"• {p['name']} – {format_price(p['price_cents'])}" for p in products]
        return "Hier eine Übersicht über unser Sortiment:\n" + "\n".join(lines)

    if any(w in text for w in ("bestell", "kaufen", "warenkorb")):
        return ("Bestellen ist ganz einfach: Leg ein Produkt in den Warenkorb, klicke oben "
                "rechts auf den Warenkorb und fülle das Bestellformular aus. Wir melden uns "
                "dann per E-Mail bei dir.")

    return ("Das habe ich leider nicht verstanden. 🤔 Du kannst mich z.B. fragen:\n"
            "• \"Welche Handys habt ihr?\"\n"
            "• \"Was kostet das Nova X Pro?\"\n"
            "• \"Wie lange dauert der Versand?\"\n\n"
            "Oder schreib uns an support@technova-shop.de.")


def chatbot_ai_answer(message, history, conn):
    """KI-Antwort über die Claude-API (nur wenn ANTHROPIC_API_KEY gesetzt ist)."""
    products = conn.execute("SELECT * FROM products ORDER BY category, id").fetchall()
    catalog = "\n\n".join(product_summary(p) + "\n" + p["description"] for p in products)
    system_prompt = (
        "Du bist der freundliche Kundenservice-Chatbot des deutschen Online-Shops "
        "'TechNova', der Handys und Laptops verkauft. Antworte kurz, hilfsbereit und "
        "auf Deutsch. Hier ist das aktuelle Sortiment mit Preisen:\n\n" + catalog +
        "\n\nWeitere Infos:\n"
        f"- Versand: {SHOP_INFO['versand']}\n"
        f"- Bezahlung: {SHOP_INFO['bezahlung']}\n"
        f"- Garantie: {SHOP_INFO['garantie']}\n"
        f"- Rückgabe: {SHOP_INFO['rueckgabe']}\n"
        f"- Kontakt: {SHOP_INFO['kontakt']}\n"
        "Erfinde keine Produkte oder Preise. Bei Fragen, die nichts mit dem Shop zu "
        "tun haben, lenke höflich zurück zum Thema."
    )
    messages = []
    for h in (history or [])[-10:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = str(h.get("text", ""))[:2000]
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message[:2000]})

    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "system": system_prompt,
        "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    answer = "\n".join(p for p in parts if p).strip()
    return answer or "Entschuldige, dazu fällt mir gerade keine Antwort ein."


# ---------------------------------------------------------------------------
# HTTP-Server
# ---------------------------------------------------------------------------

class ShopHandler(http.server.BaseHTTPRequestHandler):
    server_version = "TechNovaShop/1.0"

    # -------- Hilfsfunktionen --------

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
            self.send_header("Set-Cookie",
                             f"admin_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
        elif clear:
            self.send_header("Set-Cookie", "admin_session=; Path=/; HttpOnly; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes=8 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > max_bytes:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _serve_static_file(self, rel_path):
        if rel_path not in ALLOWED_STATIC_FILES:
            return self._send_json({"error": "Nicht gefunden"}, 404)
        full = os.path.join(STATIC_DIR, rel_path)
        if not os.path.isfile(full):
            return self._send_json({"error": "Nicht gefunden"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # Keine Request-Logs in der Konsole

    def _get_session_token(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                return part[len("admin_session="):]
        return None

    def _is_admin(self, conn):
        token = self._get_session_token()
        if not token:
            return False
        row = conn.execute("SELECT token FROM admin_sessions WHERE token = ?", (token,)).fetchone()
        return row is not None

    def _require_admin(self, conn):
        if not self._is_admin(conn):
            self._send_json({"error": "Bitte zuerst als Admin anmelden."}, 401)
            return False
        return True

    def _product_public(self, row):
        return {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "price_cents": row["price_cents"],
            "price": format_price(row["price_cents"]),
            "tagline": row["tagline"],
            "description": row["description"],
            "specs": json.loads(row["specs"] or "{}"),
            "is_featured": bool(row["is_featured"]),
            "image_url": f"/api/products/{row['id']}/image",
        }

    # -------- Routing --------

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_static_file("index.html")
            if path in ("/admin", "/admin.html"):
                return self._serve_static_file("admin.html")

            if path == "/api/products":
                return self.get_products()
            m = re.fullmatch(r"/api/products/(\d+)/image", path)
            if m:
                return self.get_product_image(int(m.group(1)))

            if path == "/api/admin/status":
                return self.get_admin_status()
            if path == "/api/admin/orders":
                return self.get_admin_orders()

            return self._send_json({"error": "Nicht gefunden"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send_json({"error": f"Serverfehler: {e}"}, 500)
            except Exception:
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/orders":
                return self.post_order()
            if path == "/api/chat":
                return self.post_chat()
            if path == "/api/admin/setup":
                return self.post_admin_setup()
            if path == "/api/admin/login":
                return self.post_admin_login()
            if path == "/api/admin/logout":
                return self.post_admin_logout()
            if path == "/api/admin/products":
                return self.post_admin_product()
            return self._send_json({"error": "Nicht gefunden"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send_json({"error": f"Serverfehler: {e}"}, 500)
            except Exception:
                pass

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            m = re.fullmatch(r"/api/admin/orders/(\d+)", path)
            if m:
                return self.put_admin_order(int(m.group(1)))
            m = re.fullmatch(r"/api/admin/products/(\d+)", path)
            if m:
                return self.put_admin_product(int(m.group(1)))
            return self._send_json({"error": "Nicht gefunden"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send_json({"error": f"Serverfehler: {e}"}, 500)
            except Exception:
                pass

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            m = re.fullmatch(r"/api/admin/products/(\d+)", path)
            if m:
                return self.delete_admin_product(int(m.group(1)))
            return self._send_json({"error": "Nicht gefunden"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send_json({"error": f"Serverfehler: {e}"}, 500)
            except Exception:
                pass

    # -------- Öffentliche API --------

    def get_products(self):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM products ORDER BY is_featured DESC, category, price_cents DESC"
            ).fetchall()
            self._send_json({"products": [self._product_public(r) for r in rows]})
        finally:
            conn.close()

    def get_product_image(self, product_id):
        conn = get_db()
        try:
            row = conn.execute("SELECT image FROM products WHERE id = ?", (product_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["image"]:
            return self._send_json({"error": "Kein Bild"}, 404)
        m = re.fullmatch(r"data:([\w.+-]+/[\w.+-]+);base64,(.*)", row["image"], re.DOTALL)
        if not m:
            return self._send_json({"error": "Ungültiges Bild"}, 500)
        try:
            data = base64.b64decode(m.group(2))
        except ValueError:
            return self._send_json({"error": "Ungültiges Bild"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", m.group(1))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def post_order(self):
        body = self._read_json_body()
        if not body:
            return self._send_json({"error": "Ungültige Anfrage"}, 400)
        name = str(body.get("customer_name", "")).strip()
        email = str(body.get("email", "")).strip()
        address = str(body.get("address", "")).strip()
        note = str(body.get("note", "")).strip()[:1000]
        items = body.get("items", [])
        if not name or not email or not address:
            return self._send_json({"error": "Bitte Name, E-Mail und Adresse ausfüllen."}, 400)
        if "@" not in email or "." not in email.split("@")[-1]:
            return self._send_json({"error": "Bitte eine gültige E-Mail-Adresse angeben."}, 400)
        if not isinstance(items, list) or not items:
            return self._send_json({"error": "Der Warenkorb ist leer."}, 400)

        conn = get_db()
        try:
            # Preise IMMER aus der Datenbank nehmen (nie dem Browser vertrauen)
            order_items = []
            total = 0
            for item in items[:50]:
                try:
                    pid = int(item.get("product_id"))
                    qty = max(1, min(99, int(item.get("quantity", 1))))
                except (TypeError, ValueError):
                    continue
                row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
                if not row:
                    continue
                order_items.append((pid, row["name"], row["price_cents"], qty))
                total += row["price_cents"] * qty
            if not order_items:
                return self._send_json({"error": "Kein gültiges Produkt im Warenkorb."}, 400)

            cur = conn.execute(
                """INSERT INTO orders (customer_name, email, address, note, total_cents, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'neu', ?)""",
                (name[:200], email[:200], address[:500], note, total, now_iso()))
            order_id = cur.lastrowid
            for pid, pname, price, qty in order_items:
                conn.execute(
                    """INSERT INTO order_items (order_id, product_id, product_name, price_cents, quantity)
                       VALUES (?, ?, ?, ?, ?)""",
                    (order_id, pid, pname, price, qty))
            conn.commit()
            self._send_json({"ok": True, "order_id": order_id, "total": format_price(total)})
        finally:
            conn.close()

    def post_chat(self):
        body = self._read_json_body(max_bytes=64 * 1024)
        if not body:
            return self._send_json({"error": "Ungültige Anfrage"}, 400)
        message = str(body.get("message", "")).strip()
        if not message:
            return self._send_json({"error": "Leere Nachricht"}, 400)
        history = body.get("history", [])
        conn = get_db()
        try:
            ai_used = False
            if ANTHROPIC_API_KEY:
                try:
                    answer = chatbot_ai_answer(message, history, conn)
                    ai_used = True
                except Exception:
                    # KI nicht erreichbar -> automatisch auf Regeln zurückfallen
                    answer = chatbot_rules_answer(message, conn)
            else:
                answer = chatbot_rules_answer(message, conn)
            self._send_json({"answer": answer, "ai": ai_used})
        finally:
            conn.close()

    # -------- Admin: Einrichtung & Login --------

    def get_admin_status(self):
        conn = get_db()
        try:
            setup_done = get_setting(conn, "admin_password_hash") is not None
            logged_in = setup_done and self._is_admin(conn)
            self._send_json({"setup_done": setup_done, "logged_in": logged_in})
        finally:
            conn.close()

    def post_admin_setup(self):
        body = self._read_json_body()
        password = str((body or {}).get("password", ""))
        if len(password) < 8:
            return self._send_json({"error": "Das Passwort muss mindestens 8 Zeichen lang sein."}, 400)
        conn = get_db()
        try:
            if get_setting(conn, "admin_password_hash") is not None:
                return self._send_json({"error": "Der Admin-Zugang wurde bereits eingerichtet."}, 400)
            salt_hex, hash_hex = hash_password(password)
            set_setting(conn, "admin_password_salt", salt_hex)
            set_setting(conn, "admin_password_hash", hash_hex)
            token = secrets.token_hex(32)
            conn.execute("INSERT INTO admin_sessions (token, created_at) VALUES (?, ?)",
                         (token, now_iso()))
            conn.commit()
            self._send_json_with_cookie({"ok": True}, token=token)
        finally:
            conn.close()

    def post_admin_login(self):
        body = self._read_json_body()
        password = str((body or {}).get("password", ""))
        conn = get_db()
        try:
            salt_hex = get_setting(conn, "admin_password_salt")
            hash_hex = get_setting(conn, "admin_password_hash")
            if not salt_hex or not hash_hex:
                return self._send_json({"error": "Der Admin-Zugang ist noch nicht eingerichtet."}, 400)
            if not verify_password(password, salt_hex, hash_hex):
                return self._send_json({"error": "Falsches Passwort."}, 401)
            token = secrets.token_hex(32)
            conn.execute("INSERT INTO admin_sessions (token, created_at) VALUES (?, ?)",
                         (token, now_iso()))
            conn.commit()
            self._send_json_with_cookie({"ok": True}, token=token)
        finally:
            conn.close()

    def post_admin_logout(self):
        token = self._get_session_token()
        conn = get_db()
        try:
            if token:
                conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
                conn.commit()
            self._send_json_with_cookie({"ok": True}, clear=True)
        finally:
            conn.close()

    # -------- Admin: Bestellungen --------

    def get_admin_orders(self):
        conn = get_db()
        try:
            if not self._require_admin(conn):
                return
            orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
            result = []
            for o in orders:
                items = conn.execute(
                    "SELECT product_name, price_cents, quantity FROM order_items WHERE order_id = ?",
                    (o["id"],)).fetchall()
                result.append({
                    "id": o["id"],
                    "customer_name": o["customer_name"],
                    "email": o["email"],
                    "address": o["address"],
                    "note": o["note"],
                    "total": format_price(o["total_cents"]),
                    "status": o["status"],
                    "created_at": o["created_at"],
                    "items": [{"name": i["product_name"],
                               "price": format_price(i["price_cents"]),
                               "quantity": i["quantity"]} for i in items],
                })
            self._send_json({"orders": result})
        finally:
            conn.close()

    def put_admin_order(self, order_id):
        conn = get_db()
        try:
            if not self._require_admin(conn):
                return
            body = self._read_json_body() or {}
            status = body.get("status")
            if status not in ("neu", "erledigt"):
                return self._send_json({"error": "Ungültiger Status."}, 400)
            cur = conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
            conn.commit()
            if cur.rowcount == 0:
                return self._send_json({"error": "Bestellung nicht gefunden."}, 404)
            self._send_json({"ok": True})
        finally:
            conn.close()

    # -------- Admin: Produkte --------

    def _validate_product_body(self, body):
        name = str(body.get("name", "")).strip()
        category = body.get("category")
        tagline = str(body.get("tagline", "")).strip()[:200]
        description = str(body.get("description", "")).strip()[:2000]
        specs = body.get("specs", {})
        image = str(body.get("image", "") or "")
        try:
            price_cents = int(round(float(str(body.get("price", "0")).replace(",", ".")) * 100))
        except (TypeError, ValueError):
            return None, "Bitte einen gültigen Preis angeben (z.B. 999 oder 999,99)."
        if not name:
            return None, "Bitte einen Produktnamen angeben."
        if category not in ("phone", "laptop"):
            return None, "Bitte eine Kategorie wählen (Handy oder Laptop)."
        if price_cents <= 0:
            return None, "Der Preis muss größer als 0 sein."
        if not isinstance(specs, dict):
            return None, "Ungültige technische Daten."
        specs = {str(k)[:60]: str(v)[:200] for k, v in list(specs.items())[:20] if str(k).strip()}
        if image:
            if not re.match(r"data:image/[\w.+-]+;base64,", image):
                return None, "Ungültiges Bildformat."
            if len(image) > MAX_IMAGE_BYTES * 4 // 3:
                return None, "Das Bild ist zu groß (max. 4 MB)."
        return {
            "name": name[:120], "category": category, "price_cents": price_cents,
            "tagline": tagline, "description": description,
            "specs": json.dumps(specs, ensure_ascii=False), "image": image,
            "is_featured": 1 if body.get("is_featured") else 0,
        }, None

    def post_admin_product(self):
        conn = get_db()
        try:
            if not self._require_admin(conn):
                return
            body = self._read_json_body()
            if not body:
                return self._send_json({"error": "Ungültige Anfrage"}, 400)
            data, err = self._validate_product_body(body)
            if err:
                return self._send_json({"error": err}, 400)
            if not data["image"]:
                # Ohne Bild-Upload: ein passendes Standard-Bild erzeugen
                svg = phone_svg("#0a84ff", "#003a75", data["name"]) if data["category"] == "phone" \
                    else laptop_svg("#0a84ff", "#003a75", data["name"])
                data["image"] = svg_to_data_url(svg)
            if data["is_featured"]:
                conn.execute("UPDATE products SET is_featured = 0")
            cur = conn.execute(
                """INSERT INTO products (name, category, price_cents, tagline, description,
                                         specs, image, is_featured, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data["name"], data["category"], data["price_cents"], data["tagline"],
                 data["description"], data["specs"], data["image"], data["is_featured"], now_iso()))
            conn.commit()
            self._send_json({"ok": True, "id": cur.lastrowid})
        finally:
            conn.close()

    def put_admin_product(self, product_id):
        conn = get_db()
        try:
            if not self._require_admin(conn):
                return
            body = self._read_json_body()
            if not body:
                return self._send_json({"error": "Ungültige Anfrage"}, 400)
            existing = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if not existing:
                return self._send_json({"error": "Produkt nicht gefunden."}, 404)
            data, err = self._validate_product_body(body)
            if err:
                return self._send_json({"error": err}, 400)
            if not data["image"]:
                data["image"] = existing["image"]  # Bild behalten, wenn keins hochgeladen wurde
            if data["is_featured"]:
                conn.execute("UPDATE products SET is_featured = 0")
            conn.execute(
                """UPDATE products SET name = ?, category = ?, price_cents = ?, tagline = ?,
                                       description = ?, specs = ?, image = ?, is_featured = ?
                   WHERE id = ?""",
                (data["name"], data["category"], data["price_cents"], data["tagline"],
                 data["description"], data["specs"], data["image"], data["is_featured"], product_id))
            conn.commit()
            self._send_json({"ok": True})
        finally:
            conn.close()

    def delete_admin_product(self, product_id):
        conn = get_db()
        try:
            if not self._require_admin(conn):
                return
            cur = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
            conn.commit()
            if cur.rowcount == 0:
                return self._send_json({"error": "Produkt nicht gefunden."}, 404)
            self._send_json({"ok": True})
        finally:
            conn.close()


def main():
    init_db()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), ShopHandler)
    print(f"TechNova-Shop läuft auf http://localhost:{PORT}")
    print(f"Admin-Bereich:            http://localhost:{PORT}/admin")
    if ANTHROPIC_API_KEY:
        print("Chatbot-Modus: KI (Claude) -- ANTHROPIC_API_KEY ist gesetzt")
    else:
        print("Chatbot-Modus: regelbasiert (kostenlos). Für KI: ANTHROPIC_API_KEY setzen.")
    server.serve_forever()


if __name__ == "__main__":
    main()
