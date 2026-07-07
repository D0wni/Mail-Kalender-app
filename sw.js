// Minimaler Service Worker – wird nur benötigt, damit iOS/Android die Seite
// als installierbare App erkennt. Kein Offline-Caching von E-Mail-Daten,
// da diese sich ständig ändern.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", () => self.clients.claim());
self.addEventListener("fetch", () => {}); // no-op, Netzwerk wird normal genutzt
