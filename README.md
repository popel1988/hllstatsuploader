# CRCON Standalone Sync (Docker)

ONE-WAY Daten-Export von CRCON PostgreSQL zu externer Datenbank.

## 🚀 Schnellstart

### 1. Konfiguration anpassen

Öffne `docker-compose.yml` und passe die Umgebungsvariablen an:

```yaml
environment:
  DB_HOST: localhost             # ← localhost (wegen network_mode: host)
  DB_PORT: 15432                 # ← Dein PostgreSQL Port
  DB_PASSWORD: dein_passwort     # ← DB-Passwort ÄNDERN!
  EXTERNAL_DB_URL: https://...   # ← Externe API-URL ÄNDERN!
  ENABLED_SERVERS: "1,2"         # ← Deine Server-Nummern
  SERVER_NAMES: '{"1": "Server-DE-01", "2": "Server-DE-02"}'  # ← Server-Namen
```

### 2. Starten

```bash
# Build und Start
docker-compose up -d

# Logs ansehen
docker-compose logs -f

# Status prüfen
docker-compose exec crcon-sync-standalone python external_sync_standalone.py status
```

### 3. Beenden

```bash
docker-compose down
```

---

## 📁 Dateien

- `external_sync_standalone.py` - Haupt-Script
- `Dockerfile` - Docker-Image
- `docker-compose.yml` - Docker Compose Konfiguration (HIER KONFIGURIEREN!)
- `requirements_standalone.txt` - Python-Dependencies
- `.env.example` - Beispiel-Konfiguration (nur für lokale Entwicklung)

---

## 🔧 Konfiguration

Alle Einstellungen erfolgen in `docker-compose.yml` unter `environment`:

| Variable | Beschreibung | Beispiel |
|----------|--------------|----------|
| `DB_HOST` | PostgreSQL Hostname | `localhost` (wegen network_mode: host) |
| `DB_PORT` | PostgreSQL Port | `15432` (CRCON Standard) |
| `DB_NAME` | Datenbankname | `rcon` |
| `DB_USER` | DB-Benutzer | `rcon` |
| `DB_PASSWORD` | **DB-Passwort** | `dein_passwort` |
| `EXTERNAL_DB_URL` | **Externe API-URL** | `https://...` |
| `EXTERNAL_DB_API_KEY` | API-Key (optional) | - |
| `EXTERNAL_SERVER_ID` | Server-ID für externe DB | `crcon_server_001` |
| `ENABLED_SERVERS` | Server-Nummern (komma-getrennt) | `"1,2,3"` |
| `SERVER_NAMES` | Server-Namen als JSON | `'{"1": "Server-DE-01"}'` |
| `SYNC_INTERVAL_MINUTES` | Intervall zwischen Exporten | `5` |
| `BATCH_SIZE` | Zeilen pro Batch | `100000` |

---

## 📊 Befehle

```bash
# Container starten
docker-compose up -d

# Logs (live)
docker-compose logs -f crcon-sync-standalone

# Status anzeigen
docker-compose exec crcon-sync-standalone python external_sync_standalone.py status

# DB-Verbindung testen
docker-compose exec crcon-sync-standalone python external_sync_standalone.py test-db

# Einmaliger Export (statt Loop)
docker-compose run --rm crcon-sync-standalone python external_sync_standalone.py once

# State zurücksetzen (VORSICHT!)
docker-compose exec crcon-sync-standalone python external_sync_standalone.py reset

# Container neustarten
docker-compose restart crcon-sync-standalone

# Container stoppen
docker-compose stop crcon-sync-standalone

# Container löschen (inkl. Volumes)
docker-compose down -v
```

---

## 🗂️ Volumes

- `sync-state:/data` - Speichert `external_sync_state.json` persistent

**State-Datei Speicherort im Container:** `/data/external_sync_state.json`

---

## 🌐 Netzwerk

**Nutzt `network_mode: host`:**

- Container läuft im Host-Netzwerk (keine Isolation)
- Kann `localhost` nutzen für DB-Zugriff
- Keine separate `networks:` Sektion nötig
- ✅ Einfacher Zugriff auf Host-Services
- ⚠️ Weniger Netzwerk-Isolation (aber OK für diesen Use-Case)

---

## 🔍 Troubleshooting

### Problem: "connection refused"

```bash
# DB-Verbindung testen
docker-compose exec crcon-sync-standalone python external_sync_standalone.py test-db
```

**Lösung:**
- Prüfe `DB_HOST` (Service-Name oder IP)
- Prüfe ob DB-Container im gleichen Netzwerk
- Prüfe `DB_PORT`

### Problem: "No module named..."

```bash
# Container neu bauen
docker-compose build --no-cache
docker-compose up -d
```

### Problem: State-Datei geht verloren

- Volume prüfen: `docker volume ls`
- Nicht `docker-compose down -v` nutzen (löscht Volumes!)

---

## 📈 Monitoring

```bash
# Logs der letzten Stunde
docker-compose logs --since 1h crcon-sync-standalone

# Nur Fehler
docker-compose logs | grep ERROR

# Container-Status
docker-compose ps

# Resource-Nutzung
docker stats crcon-sync-standalone
```

---

## 🔄 Update

```bash
# Code aktualisieren
git pull  # oder neue Dateien kopieren

# Container neu bauen und starten
docker-compose build
docker-compose up -d
```

---

## 📝 Hinweise

1. **Passwörter:** Nie in Git committen! Nur in `docker-compose.yml` auf dem Server.
2. **SERVER_NAMES:** Muss gültiges JSON sein, mit einfachen Anführungszeichen außen!
3. **Volumes:** State-Datei bleibt erhalten bei `docker-compose down` (ohne `-v`)
4. **Netzwerk:** Container muss PostgreSQL erreichen können

---

Weitere Details: Siehe `STANDALONE_SYNC_GUIDE.md`

