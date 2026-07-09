# 🛍️ TechNova – Dein Online-Shop: Anleitung für Anfänger

Hallo! Diese Anleitung erklärt dir **Schritt für Schritt und ohne Fachchinesisch**, was gebaut wurde und wie du deine Website **kostenlos** ins Internet bringst.

---

## 1. Was wurde gebaut?

In deinem Projekt gibt es jetzt einen neuen Ordner **`shop/`** mit drei Dateien. Deine Mail & Kalender App wurde **nicht verändert** – sie funktioniert weiter wie bisher.

| Datei | Was sie macht |
|---|---|
| `shop/index.html` | **Die Website selbst.** Das sehen deine Besucher: Startseite mit dem neuesten Handy, Scroll-Animationen wie bei Apple, Produktbereich, Warenkorb und Chatbot. Funktioniert auf dem Computer **und** auf dem Handy. |
| `shop/admin.html` | **Dein Admin-Bereich** (erreichbar unter `/admin`). Hier siehst du Bestellungen und stellst neue Produkte ein. Nur mit Passwort zugänglich. |
| `shop/server.py` | **Das Gehirn dahinter.** Ein kleines Programm, das die Website ausliefert, Bestellungen und Produkte in einer Datenbank speichert und den Chatbot antworten lässt. Es braucht **nur Python**, keine Zusatzprogramme. |

Der Shop startet mit **6 Beispiel-Produkten** (3 Handys, 3 Laptops mit dem Fantasienamen "Nova"). Echte Apple-Namen und -Bilder darf man rechtlich nicht verwenden – aber du kannst im Admin-Bereich jederzeit eigene Produkte mit eigenen Fotos einstellen.

---

## 2. Die Website auf deinem Computer ausprobieren

Du brauchst nur **Python 3** (auf den meisten Computern schon installiert, sonst kostenlos von [python.org](https://www.python.org/downloads/)).

1. Öffne ein Terminal (Windows: "Eingabeaufforderung", Mac: "Terminal").
2. Gehe in deinen Projektordner und starte den Server:
   ```
   python3 shop/server.py
   ```
   (Unter Windows heißt der Befehl oft nur `python` statt `python3`.)
3. Öffne im Browser:
   - **Shop:** http://localhost:8000
   - **Admin-Bereich:** http://localhost:8000/admin

Zum Beenden im Terminal `Strg + C` drücken.

---

## 3. Den Admin-Bereich einrichten (nur einmal nötig)

1. Öffne `/admin` (siehe oben).
2. Beim **allerersten Aufruf** wirst du gebeten, ein Admin-Passwort festzulegen (mindestens 8 Zeichen). **Merke es dir gut** – es gibt keine "Passwort vergessen"-Funktion!
3. Danach meldest du dich immer mit diesem Passwort an.

**Wichtig:** Richte das Passwort direkt nach dem ersten Start ein – besonders sobald die Seite online ist. Solange kein Passwort gesetzt ist, könnte sonst jemand anderes zuerst eines setzen.

Im Admin-Bereich kannst du dann:
- **Bestellungen** ansehen (Name, Adresse, bestellte Artikel) und als "erledigt" abhaken
- **Produkte** einstellen, bearbeiten und löschen – mit eigenem Foto, Preis und technischen Daten
- Ein Produkt als **"Neuestes Produkt"** markieren → es erscheint dann groß auf der Startseite mit den Scroll-Animationen

---

## 4. Die Website KOSTENLOS ins Internet bringen

Deine Website braucht einen Server im Internet, damit andere sie sehen können. Das geht kostenlos bei **Render** (dort läuft vermutlich schon deine Mail-App – das Prinzip ist identisch):

### Schritt für Schritt mit Render

1. Gehe auf [render.com](https://render.com) und melde dich an (kostenlos, am einfachsten mit deinem GitHub-Konto).
2. Klicke auf **"New" → "Web Service"**.
3. Wähle dein GitHub-Repository **`Mail-Kalender-app`** aus.
4. Fülle das Formular so aus:
   - **Name:** z.B. `technova-shop` (daraus wird deine Internetadresse: `technova-shop.onrender.com`)
   - **Language:** Python 3
   - **Build Command:** kann leer bleiben (oder `pip install -r requirements.txt`)
   - **Start Command:** `python3 shop/server.py`
   - **Instance Type:** **Free** (kostenlos)
5. Klicke auf **"Deploy Web Service"** und warte 1–2 Minuten.
6. Fertig! 🎉 Deine Website ist jetzt unter `https://DEIN-NAME.onrender.com` für alle erreichbar. Öffne sofort `https://DEIN-NAME.onrender.com/admin` und lege dein Admin-Passwort fest.

### Zwei ehrliche Hinweise zum kostenlosen Paket

- **Einschlafen:** Wenn 15 Minuten lang niemand die Seite besucht, "schläft" sie ein. Der nächste Besucher muss dann ca. 30–60 Sekunden warten, bis sie aufwacht. Für ein Hobby-Projekt völlig okay.
- **Daten sind nicht für immer:** Beim kostenlosen Paket wird der Server gelegentlich neu aufgesetzt. Dann sind **selbst eingestellte Produkte, Bestellungen und das Admin-Passwort weg** und die Beispiel-Produkte kehren zurück. Zum Lernen, Zeigen und Ausprobieren reicht das. Wenn du später einen echten Shop betreiben willst, brauchst du bei Render eine "Persistent Disk" (kostet ein paar Euro im Monat) – oder eine richtige Shop-Plattform.

### Alternativen (auch kostenlos)

- [PythonAnywhere](https://www.pythonanywhere.com) – kostenloser Python-Server, schläft nicht ein, aber Einrichtung etwas umständlicher.
- [Railway](https://railway.app) – ähnlich wie Render, Startguthaben statt Dauer-Gratis.

---

## 5. Der Chatbot – und wie du echte KI einschaltest

Unten rechts auf der Website ist ein Chat-Knopf. Der Chatbot antwortet **sofort und kostenlos** auf Fragen wie:

- „Welche Handys habt ihr?"
- „Was kostet das Nova X Pro?"
- „Wie lange dauert der Versand?"

Er kennt automatisch alle Produkte und Preise aus deiner Datenbank – auch die, die du selbst neu einstellst.

### Später auf echte KI umstellen (optional)

Wenn du möchtest, dass der Chatbot **frei formulierte Antworten mit echter KI** (Claude) gibt:

1. Erstelle ein Konto auf [console.anthropic.com](https://console.anthropic.com) und lade ein kleines Guthaben auf (schon 5 $ reichen lange – ein Gespräch kostet unter einem Cent).
2. Erstelle dort unter "API Keys" einen Schlüssel und kopiere ihn.
3. In Render: Öffne deinen Web Service → **"Environment"** → füge hinzu:
   - **Key:** `ANTHROPIC_API_KEY`
   - **Value:** *dein kopierter Schlüssel*
4. Speichern – Render startet den Server automatisch neu. **Fertig, der Chatbot ist jetzt eine KI.** Am Code musst du nichts ändern; ohne Schlüssel schaltet er automatisch zurück auf die kostenlosen Antworten.

---

## 6. Häufige Fragen

**Kann ich echte Zahlungen annehmen (PayPal, Kreditkarte)?**
Aktuell nicht – bewusst. Zahlungsanbieter kosten Gebühren und verlangen meist ein Gewerbe. Bestellungen landen stattdessen in deinem Admin-Bereich und du wickelst sie per E-Mail ab ("Kauf auf Rechnung").

**Kann ich eine eigene Internetadresse wie `www.mein-shop.de` haben?**
Ja, aber Domains kosten ca. 10–15 € im Jahr (z.B. bei Namecheap oder IONOS). Die Domain verbindest du dann in Render unter "Custom Domains". Die kostenlose Adresse `….onrender.com` funktioniert aber genauso gut.

**Wie ändere ich Texte auf der Website?**
Öffne `shop/index.html` in einem Texteditor (z.B. dem kostenlosen [VS Code](https://code.visualstudio.com)). Alle sichtbaren Texte stehen als normaler Text darin – ändern, speichern, auf GitHub hochladen, Render aktualisiert sich automatisch.

**Ist meine Mail & Kalender App noch da?**
Ja! Sie liegt unverändert im Hauptordner. Der Shop lebt komplett getrennt im Ordner `shop/` und läuft als eigener Web Service.

Viel Spaß mit deinem Shop! 🚀
