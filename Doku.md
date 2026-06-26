# Handbuch: MIC Traffic Lab – Der gesamte Source-Code erklärt

Dieses Handbuch erklärt **jede Datei und jede wichtige Codezeile** des Projekts, von unten nach oben: zuerst die einfachen, isolierten Bausteine (Targets, Metrics), dann die 4 Traffic-Generatoren, dann das Herzstück (der Controller), dann das Dashboard, und zum Schluss die YAML-Profile und die Swagger/OpenAPI-Doku als "Klammer" über allem.

Jedes Modul ist in sich abgeschlossen lesbar. Konkrete Werte (Defaults, Beispielpfade, Beispiel-JSON) werden überall ausgeschrieben, nicht nur beschrieben – damit du nichts nachschlagen musst, um zu verstehen, was tatsächlich passiert.

Am Ende jedes Moduls stehen **Check-Fragen**. Am Ende des Handbuchs gibt es einen Anhang mit Lösungshinweisen für alle Fragen, plus zwei vollständige "End-to-End-Reisen" und ein großes Abschluss-Quiz.

---

## Inhaltsverzeichnis

0. Architektur-Überblick (`docker-compose.yml`)
1. Targets & Metrics Collector
2. Die 4 Traffic-Generatoren (TCP/UDP, HTTP/2, QUIC, MQTT)
3. Der Controller (`controller/main.py`)
4. Das Dashboard (`dashboard/index.html`)
5. Die YAML-Profile (`config/*.yaml`)
6. API-Dokumentation (Swagger/OpenAPI)
7. Anhang: Lösungshinweise, End-to-End-Reisen, Abschluss-Quiz

---

## Modul 0 – Architektur-Überblick

**Datei:** `docker-compose.yml` (158 Zeilen)

### Das Netzwerk

Ein einziges Bridge-Netzwerk `mic-net` (Zeile 4-6). Alle 10 Container hängen daran und erreichen sich über Docker's internen DNS per Service-Namen – z.B. `http://metrics:9090` statt einer IP-Adresse. Deshalb taucht überall `METRICS_URL: http://metrics:9090` etc. als Environment-Variable auf.

### Die 10 Services im Detail

**1. `mosquitto`** (Zeile 12-20) – fertiges Image `eclipse-mosquitto:2`, kein eigener Build. MQTT-Broker, Port 1883 nach außen offen. Bekommt eine eigene Config-Datei eingebunden (`./config/mosquitto.conf`, read-only).

**2. `metrics`** (Zeile 23-34) – eigener Build (`./metrics`), Port 9090. Hat als einziger Service einen **Healthcheck**:
```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:9090/metrics')"]
  interval: 5s
  timeout: 3s
  retries: 5
```
Mehrere andere Services warten per `depends_on: metrics: condition: service_healthy` darauf, dass `metrics` **wirklich antwortet** (nicht nur "Container läuft").

**3. `controller`** (Zeile 37-54) – das Steuerzentrum, Port 8000. Bekommt `./config` als read-only Volume gemountet (die YAML-Profile) und kennt über Environment-Variablen die URLs aller 4 Generatoren:
```yaml
environment:
  METRICS_URL: http://metrics:9090
  GEN_HTTP2_URL: http://gen-http2:7001
  GEN_QUIC_URL: http://gen-quic:7002
  GEN_MQTT_URL: http://gen-mqtt:7003
  GEN_TCPUDP_URL: http://gen-tcpudp:7004
```

**4. Drei Targets** (Zeile 57-89):
- `target-http2` → Port 8080 (TCP)
- `target-quic` → Port `4433/udp` (wichtig: QUIC läuft über **UDP**, nicht TCP!)
- `target-tcpudp` → **kein** externer Port; nur intern über `mic-net` erreichbar (Port 9999)

**5. Vier Generatoren** (Zeile 92-148) – gleiches Muster: eigener Build, interner Port (7001-7004), `TARGET_*`/`BROKER_*`-Env zeigt aufs jeweilige Ziel, `METRICS_URL` zum Collector, `depends_on` Target + Metrics. **Keine** externen Ports – sie werden nur vom Controller über `mic-net` angesprochen.

| Generator | Build | Port | Ziel-Env | Ziel |
|---|---|---|---|---|
| `gen-http2` | `./generators/http2` | 7001 | `TARGET_URL=http://target-http2:8080` | target-http2 |
| `gen-quic` | `./generators/quic` | 7002 | `TARGET_HOST=target-quic`, `TARGET_PORT=4433` | target-quic |
| `gen-mqtt` | `./generators/mqtt` | 7003 | `BROKER_HOST=mosquitto`, `BROKER_PORT=1883` | mosquitto |
| `gen-tcpudp` | `./generators/tcpudp` | 7004 | `TARGET_HOST=target-tcpudp`, `TARGET_PORT=9999` | target-tcpudp |

**6. `dashboard`** (Zeile 151-157) – eigener Build, Port 3000 nach außen offen. Kein `depends_on` – das Frontend pollt einfach den Controller über `localhost:8000` (vom Browser aus, nicht über `mic-net`).

### Das Gesamtbild

```
Browser (Dashboard, :3000)
   │  pollt alle 2s GET /status
   ▼
Controller (:8000)
   │  steuert per REST (start/stop/PATCH)
   ▼
4 Generatoren (gen-http2, gen-quic, gen-mqtt, gen-tcpudp)
   │  senden Traffic
   ▼
3 Targets / Broker (target-http2, target-quic, target-tcpudp, mosquitto)
   │  alle Generatoren + 2 Targets melden Stats alle 5-10s
   ▼
Metrics Collector (:9090)
   ▲
   │  Controller liest GET /metrics für Aggregation + Adaptive Control
```

### Check-Fragen Modul 0

1. Welche 4 Protokoll-Generatoren gibt es und gegen welches Target/Broker sendet jeder?
2. Wozu existiert der Metrics-Collector separat vom Controller, und wer wartet eigentlich auf wessen Healthcheck?
3. Welche konkreten Einträge bräuchtest du in `docker-compose.yml`, um einen 5. Protokoll-Generator (z.B. WebSocket) einzubinden?

---

## Modul 1 – Targets & Metrics Collector

**Dateien:** `targets/http2_server/server.py` (79 Zeilen), `targets/quic_server/server.py` (116 Zeilen), `targets/tcpudp_sink/sink.py` (55 Zeilen), `metrics/collector.py` (74 Zeilen)

### `targets/http2_server/server.py`

Ein Hypercorn/FastAPI-Server, der HTTP/2 nativ unterstützt.

- `GET /` und `POST /data` sind die "echten" Endpoints. `/data` liest den Request-Body und zählt `bytes_in`.
- `GET /health` für Healthchecks.
- **Catch-all-Routes** (Zeile 45-58):
  ```python
  @app.get("/{path:path}")
  async def catch_all_get(path: str):
      ...
      return JSONResponse({"status": "ok", "server": "HTTP/2 target", "path": f"/{path}"})

  @app.post("/{path:path}")
  async def catch_all_post(path: str, request: Request):
      body = await request.body()
      ...
      return JSONResponse({"received": len(body), "path": f"/{path}"})
  ```
  Grund: Der HTTP/2-Generator hat konfigurierbare `get_paths`/`post_paths` (z.B. `/api/items`), die der Server gar nicht im Voraus kennen muss. Statt 404 antwortet er generisch. Die spezifischen Routen `/`, `/data`, `/health` haben **Vorrang**, weil FastAPI Routen in Registrierungsreihenfolge matcht (sie stehen im Code vor den Catch-all-Routen).
- `_report()`-Thread (Zeile 61-78) meldet alle **10 Sekunden** `requests`/`bytes_in` als `packets_sent`/`bytes_sent` an den Metrics Collector, mit `"errors": 0` hartkodiert (das Target zählt keine eigenen Fehler).

### `targets/quic_server/server.py`

Deutlich tiefer in `aioquic` als der HTTP/2-Server – kein High-Level-Framework, sondern manuelle Event-Verarbeitung.

- `H3Handler(QuicConnectionProtocol)` verarbeitet QUIC-Events selbst über `quic_event_received()`.
- Bei `HeadersReceived`: die Methode (`:method`-Pseudo-Header) wird extrahiert. Ist der Stream schon zu Ende (`stream_ended`, z.B. ein GET ohne Body), wird direkt geantwortet. Sonst wird der Stream-State (`_methods`, `_bodies`) zwischengespeichert, bis bei `DataReceived` `stream_ended=True` kommt (POST mit Body).
- `_respond()`: bei POST wird `bytes_in` gezählt und `{"received": N}` zurückgegeben, bei GET ein generisches `{"status":"ok","server":"QUIC target"}`.
- TLS-Zertifikat kommt aus `cert.pem`/`key.pem` (im Dockerfile generiert, selbstsigniert) – deshalb setzt der QUIC-Generator `verify_mode = ssl.CERT_NONE`.
- Gleicher `_report()`-Mechanismus wie beim HTTP/2-Target (alle 10s).

### `targets/tcpudp_sink/sink.py`

Der einfachste Service im ganzen Projekt:

```python
def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(128)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=drain, args=(conn,), daemon=True).start()

def drain(conn):
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
    except Exception:
        pass
    finally:
        conn.close()
```

- Zwei Daemon-Threads: ein TCP-Server (akzeptiert Verbindungen, liest in `drain()` bis nichts mehr kommt) und ein UDP-Server (`recvfrom()` in Endlosschleife).
- **Keine Auswertung des Inhalts** – Daten werden gelesen und verworfen ("Sink"/Müllschlucker).
- **Kein** `_report()`-Mechanismus! Dieser Service meldet sich **nicht** beim Metrics Collector, im Gegensatz zu den HTTP/2- und QUIC-Targets.
- Zweck laut Docstring: macht TCP-RST sichtbar in Wireshark, wenn der Container gestoppt wird (Failure-Visibility-Analyse).
- Reines `socket`-Modul, kein FastAPI/Flask.

### `metrics/collector.py`

Flask statt FastAPI (einziger Flask-Service im Projekt).

```python
store = {
    "generators": {},   # name → letzter Stat-Snapshot
    "start_time": time.time(),
}

@app.route("/update", methods=["POST"])
def update():
    payload = request.get_json(silent=True)
    if not payload or "generator" not in payload:
        return jsonify({"error": "missing 'generator' field"}), 400
    name = payload["generator"]
    with lock:
        store["generators"][name] = {**payload, "_ts": time.time()}
    return jsonify({"ok": True})
```

- `store["generators"]`: Dict `name → letzter Snapshot`. Jeder neue `/update`-Call **überschreibt komplett** den vorherigen Eintrag für diesen Namen (kein Aufsummieren über Zeit – nur der jeweils aktuellste Snapshot).
- Trotz des Namens `"generators"` landen hier **6 mögliche Quellen**: die 4 Generatoren (`gen-http2`, `gen-quic`, `gen-mqtt`, `gen-tcpudp`) **plus** die 2 Targets, die selbst `_report()`-Threads haben (`target-http2`, `target-quic`). Der TCP/UDP-Sink meldet sich nie, taucht also nie in `store["generators"]` auf.
- `GET /metrics`: summiert `packets_sent`, `bytes_sent`, `errors`, `rate_bps` über **alle** Einträge und gibt zusätzlich die rohen Einzel-Snapshots unter `"generators"` zurück.
- `POST /reset`: setzt alle Zähler auf 0 und `start_time` neu – für einen frischen Demo-Lauf ohne Container-Neustart.

### Konkretes Beispiel: was schickt ein Generator wirklich?

Aus dem MQTT-Generator (`_metrics_loop`, alle 5 Sekunden):
```python
payload = {
    "generator":    "gen-mqtt",
    "running":      state["running"],
    "packets_sent": state["packets_sent"],
    "bytes_sent":   state["bytes_sent"],
    "errors":       state["errors"],
    "rate_bps":     state["rate_bps"],
    "rate":         state["rate"],
    "latency_ms":   state["latency_ms"],
}
```
Kein "payload" (die Nutzdaten-Bytes selbst werden nie mitgeschickt, nur deren **Anzahl**), keine Zeit (der Zeitstempel `_ts` wird erst vom Collector beim Empfang gesetzt). Die Targets schicken ein abgespecktes Subset (`running`, `packets_sent`, `bytes_sent`, `errors: 0` – kein `rate_bps`/`rate`/`latency_ms`).

### Check-Fragen Modul 1

1. Was passiert mit einem eingehenden TCP/UDP-Paket im Sink – wird der Inhalt ausgewertet oder nur verschluckt?
2. Wie viele Einträge hat `store["generators"]` typischerweise, wenn alles läuft, und welche Quelle fehlt darin garantiert?
3. Welche Felder schickt ein Generator/Target an `/update`, und welche zwei Felder schickt **kein** Sender (sondern werden lokal beim Empfänger erzeugt)?

---

## Modul 2 – Die 4 Traffic-Generatoren

Alle 4 Generatoren folgen demselben Grundgerüst (das ist bewusst so gebaut, nicht zufällig gleich):

- Ein globales `state`-Dict (Thread-sicher über `threading.Lock`/`_lock`), das sowohl Konfiguration als auch laufende Zähler enthält.
- Ein Pydantic-Modell `GeneratorConfig` für Swagger/Validierung – alle Felder `Optional`, damit `PATCH /config` immer nur Teilmengen aktualisieren kann.
- Ein Pydantic-Modell `StatusResponse` für `GET /status`.
- Ein Hintergrund-Loop (`_send_loop`/`_generate`), der in einem Daemon-Thread läuft und unabhängig von den HTTP-Requests Traffic erzeugt.
- Ein `_metrics_loop`/`_metrics`-Hintergrund-Task, der alle 5 Sekunden Stats an den Metrics Collector pusht.
- Vier REST-Endpoints: `POST /start`, `POST /stop`, `PATCH /config`, `GET /status` – exakt dasselbe Muster bei allen 4 Generatoren.
- Fault Injection (`fault_rate`, `extra_latency_ms`): Vor dem eigentlichen Senden wird ein Münzwurf gemacht; bei Treffer wird nur `errors` erhöht, **ohne** dass wirklich gesendet wird.

Reihenfolge hier: TCP/UDP zuerst (am "rohesten"), dann HTTP/2 (am vertrautesten), dann QUIC (am komplexesten wegen Connection-Handling), dann MQTT (Pub/Sub, andersartig).

### 2.1 TCP/UDP-Generator (`generators/tcpudp/generator.py`, 348 Zeilen)

Hier gibt es **zwei orthogonale Konfigurationsachsen**, die nicht verwechselt werden dürfen:

- **`mode`** (`"normal"` | `"stealth"`) → steuert **Paketgröße + Basis-Interval**
- **`pattern`** (`"constant"` | `"periodic_burst"` | `"random"`) → steuert die **übergeordnete Sende-Kadenz**

Beide wirken gleichzeitig und unabhängig voneinander.

**Relevante State-Felder (Zeile 50-70):**
```python
state = {
    "running":      False,
    "mode":         "normal",   # "normal" | "stealth"
    "tcp_rate":     10,         # TCP packets/sec (normal mode)
    "udp_rate":     5,          # UDP packets/sec (normal mode)
    "packet_size":  512,        # bytes (normal mode, fixed)
    "mean_interval":0.100,      # seconds (stealth mode Poisson mean)
    "min_size":     64,         # bytes (stealth mode)
    "max_size":     1400,       # bytes (stealth mode)
    "tcp_ratio":    60,         # % TCP, rest UDP
    "pattern":      "constant", # "constant" | "periodic_burst" | "random"
    "burst_size":     10,       # packets per burst (periodic_burst)
    "burst_interval": 1.0,      # seconds idle between bursts (periodic_burst)
    ...
}
```

**`_normal_params()`** (Zeile 152-163): feste `packet_size`, Interval = `1/rate` (`rate` ist je nach gewürfeltem Protokoll `tcp_rate` oder `udp_rate`). Klares, gleichmäßiges Muster → "leicht erkennbarer Fingerprint" laut Docstring.

**`_stealth_params()`** (Zeile 166-178):
```python
def _stealth_params() -> tuple[int, float, str]:
    with _lock:
        mean, min_s, max_s, tcp_pct = state["mean_interval"], state["min_size"], state["max_size"], state["tcp_ratio"]
    size     = random.randint(min_s, max_s)
    interval = np.random.exponential(mean)   # Poisson-Prozess
    proto    = "tcp" if random.randint(1, 100) <= tcp_pct else "udp"
    return size, interval, proto
```
Größe zufällig zwischen `min_size`/`max_size`, Interval bereits hier via `np.random.exponential(mean)` – also **schon im Stealth-Mode selbst** ein Poisson-Prozess, unabhängig vom `pattern`-Feld. Wichtig: Stealth-Mode hat *immer* zufälliges Timing, auch wenn `pattern="constant"` gesetzt ist. `pattern` entscheidet nur, was mit diesem bereits berechneten `interval`-Wert **zusätzlich** passiert.

**`_send_loop()`** (Zeile 218-271) – der Kern:
```python
n_packets = burst_size if pattern == "periodic_burst" else 1
for i in range(n_packets):
    size, interval, proto = (_normal_params() if mode == "normal" else _stealth_params())
    ...
    if pattern == "periodic_burst":
        if i < n_packets - 1:
            time.sleep(0.005)              # enger Abstand innerhalb eines Bursts
    elif pattern == "random":
        time.sleep(np.random.exponential(interval))   # Poisson-Gap, ZUSÄTZLICH zu mode
    else:  # "constant"
        time.sleep(interval)
if pattern == "periodic_burst":
    time.sleep(burst_interval)             # lange Pause NACH dem ganzen Burst
```
Bei Stealth-Mode **und** `pattern="random"` wird also zweimal eine Exponentialverteilung angewendet (einmal in `_stealth_params` als Basis, einmal hier oben drauf) – eine bewusste Verschachtelung, die man beim Nachrechnen in Wireshark kennen sollte.

**`_send_tcp`/`_send_udp`** (Zeile 181-213): TCP öffnet **pro Paket eine neue Verbindung** (`connect()` → `sendall()` → `close()`) – **kein** Connection-Reuse wie bei QUIC/HTTP-2! Bewusst anders, weil hier rohe TCP-Pakete im Fokus stehen (jeder Connect erzeugt sichtbaren SYN/SYN-ACK/FIN-Verkehr in Wireshark). UDP ist sowieso verbindungslos.

**REST-API** (Zeile 312-343): gleiches Muster, mit einer Kleinigkeit: `OkResponse` enthält zusätzlich `mode`, das `PATCH /config` im Response mitzurückgibt.

### Check-Fragen 2.1

1. Was genau ändert sich am Sende-Timing, wenn man `mode="stealth"` UND `pattern="random"` gleichzeitig setzt – wie viele Zufallsprozesse wirken da zusammen?
2. Warum öffnet der TCP-Pfad pro Paket eine neue Verbindung statt eine offene Verbindung wiederzuverwenden – was wäre der Vorteil einer Wiederverwendung, und warum macht man es hier trotzdem nicht?

### 2.2 HTTP/2-Generator (`generators/http2/generator.py`, 342 Zeilen)

**State (Zeile 45-64):**
```python
state = {
    "running":            False,
    "rate":               50,
    "payload_size":       2048,
    "method_get_pct":     70,    # 70% GET, 30% POST
    "concurrent_streams": 3,
    "get_paths":          ["/"],       # Ziel-Pfade für GET-Requests
    "post_paths":         ["/data"],   # Ziel-Pfade für POST-Requests
    "pattern":            "constant",  # "constant" | "periodic_burst" | "random"
    "burst_rate":         400,         # requests/sec während eines Bursts
    "burst_duration":     5,           # Sekunden: Länge eines Burst-Fensters
    "burst_interval":     30,          # Sekunden: Abstand zwischen Burst-Fenstern
    ...
}
```

**Konkrete Antwort auf "was steht in `get_paths`?"** (das war die ursprüngliche Frage, die diesen Handbuch-Auftrag ausgelöst hat): **Standardmäßig genau ein Pfad**, nämlich `"/"`. `post_paths` enthält standardmäßig genau `"/data"`. Das sind die einzigen zwei Werte, die im Python-Code als Default hartkodiert sind (Zeile 51-52 oben). Diese Listen sind aber **zur Laufzeit erweiterbar** – auf drei Wegen:

1. **Im Dashboard** (Modul 4): ein Textfeld "GET paths" / "POST paths", in das man kommagetrennt zusätzliche Pfade eintragen kann, z.B. `/, /api/items`. Der JS-Handler `onPathsChange()` zerlegt den String an Kommas und schickt die Liste per `PATCH /generator/gen-http2` mit `{"get_paths": ["/", "/api/items"]}`.
2. **Direkt per API** (Swagger/curl): `PATCH http://localhost:7001/config` mit Body `{"get_paths": ["/", "/api/items"], "post_paths": ["/data", "/api/upload"]}`. Das ist auch das Beispiel, das im Pydantic-Modell als `json_schema_extra`-Beispiel hinterlegt ist (Zeile 76-78).
3. **Über ein YAML-Profil**: aktuell nutzt keines der drei mitgelieferten Profile (`balanced.yaml`, `http2_heavy.yaml`, `mqtt_heavy.yaml`) dieses Feld – sie verlassen sich auf die Defaults `["/"]`/`["/data"]`. Man könnte es aber genauso unter `protocols.http2.get_paths: ["/", "/api/items"]` in eine Phase schreiben, weil `_phase_to_gen_configs()` im Controller (Modul 3) das Feld unverändert durchreicht.

Bei jedem Request wird **einer** der konfigurierten Pfade **zufällig** gewählt (`random.choice(get_paths)` bzw. `random.choice(post_paths)`, Zeile 195/199) – nicht alle der Reihe nach. Der Sinn laut Pydantic-Beschreibung (Zeile 98-103): "Allows the generator to spread traffic across multiple endpoints instead of always hitting the same path." Auf der Zielseite fängt der HTTP/2-Target-Server (Modul 1) **jeden** beliebigen Pfad über seine Catch-all-Routen ab – man muss also nie einen neuen Endpoint im Target anlegen, wenn man im Generator einen neuen Pfad konfiguriert.

**`_send_one()`** (Zeile 171-213) – "ein Sendezyklus" bedeutet hier **eine** HTTP/2-Anfrage:
```python
use_get = random.randint(1, 100) <= state["method_get_pct"]   # Münzwurf GET/POST
...
if use_get:
    path = random.choice(get_paths)
    r = await client.get(f"{TARGET_URL}{path}", timeout=5.0)
    n = len(r.content)                 # gemessen wird die ANTWORT-Größe
else:
    path = random.choice(post_paths)
    payload = os.urandom(payload_size)
    r = await client.post(f"{TARGET_URL}{path}", content=payload, timeout=5.0)
    n = payload_size                   # gemessen wird die GESENDETE Größe
```
Kleine Asymmetrie: Bei GET wird `bytes_sent` aus der **Antwortgröße** befüllt, bei POST aus der **gesendeten Payload-Größe** – wichtig, wenn man die `bytes_sent`-Statistik zwischen GET und POST vergleicht.

**`_is_burst_active()`** (Zeile 216-223) – der "Epoch-Modulo-Trick":
```python
def _is_burst_active(pattern, burst_duration, burst_interval) -> bool:
    if pattern != "periodic_burst" or burst_interval <= 0:
        return False
    return (time.time() % burst_interval) < burst_duration
```
Elegant, weil **zustandslos**: kein Timer/Task, der mitzählt, wann der letzte Burst war. Jeder Aufruf berechnet unabhängig anhand der aktuellen Wall-Clock-Zeit, ob man gerade "im Burst-Fenster" ist. Vorteil: Die Kadenz bleibt auch über Neustarts hinweg stabil (kein In-Memory-Zähler) und ist in Wireshark als regelmäßiges Muster erkennbar.

**`_generate()`** (Zeile 226-256) – das Herzstück:
```python
effective_rate = rate
if _is_burst_active(pattern, burst_duration, burst_interval):
    effective_rate = max(rate, burst_rate)

tasks = [_send_one(client) for _ in range(streams)]
await asyncio.gather(*tasks, return_exceptions=True)   # ECHTES Multiplexing

interval = streams / effective_rate if effective_rate > 0 else 0.1
if pattern == "random":
    await asyncio.sleep(random.expovariate(1.0 / interval))
else:
    await asyncio.sleep(interval)
```
- `effective_rate = max(rate, burst_rate)` **nur** wenn `_is_burst_active(...)` true ist.
- Pro Zyklus werden `streams` Requests **gleichzeitig** gefeuert (`asyncio.gather`) – das ist das "echte" HTTP/2-Multiplexing über die eine persistente Connection.
- `interval = streams / effective_rate` bezieht sich auf einen **ganzen Zyklus von `streams` Requests**, nicht auf 1 Request. Bei `streams=3` und `rate=50` wartet der Generator `3/50 = 0.06s` zwischen den Dreier-Bursts → im Schnitt 50 Requests/Sek.
- `pattern == "random"`: Poisson-Gaps zwischen den Multi-Stream-Zyklen (nicht zwischen Einzel-Requests).
- HTTP/2-Client: **eine** persistente `httpx.AsyncClient(http2=True)`-Instanz über die ganze `_generate()`-Schleife hinweg (`async with`, Zeile 228) – kein TLS-Handshake-Overhead pro Request.

**`/status`-Sonderfall** (Zeile 330-337): `burst_active` wird **live berechnet** bei jedem Status-Abruf, nicht aus dem State gelesen – das Dashboard sieht also exakt, ob *jetzt gerade* ein Burst läuft.

### Check-Fragen 2.2

1. Was steht standardmäßig in `get_paths` und `post_paths`, und über welche drei Wege kann man das ändern?
2. Wird bei jedem Request derselbe Pfad benutzt oder einer zufällig ausgewählt – und was passiert auf der Zielseite, wenn man im Generator einen Pfad konfiguriert, den der Server noch nie "gesehen" hat?
3. Warum bezieht sich `interval = streams / effective_rate` auf einen ganzen Zyklus und nicht auf 1 Request – was würde sich ändern, wenn man stattdessen `interval = 1 / effective_rate` schreiben würde?

### 2.3 QUIC/HTTP-3-Generator (`generators/quic/generator.py`, 393 Zeilen)

**Design-Hintergrund (wichtig, steht so im Docstring):** Ein QUIC-Handshake (TLS 1.3) kostet mindestens einen zusätzlichen Round-Trip, bevor überhaupt ein HTTP/3-Request gesendet werden kann. Würde man pro Request neu verbinden, würde dieser Handshake-Overhead die gemessene Latenz dominieren und QUIC künstlich viel langsamer aussehen lassen als die anderen Protokolle (die alle eine Verbindung wiederverwenden: HTTP/2 über `httpx.AsyncClient`, MQTT über die persistente Broker-Verbindung, TCP/UDP zumindest konzeptionell). Deshalb öffnet dieser Generator **eine** QUIC-Verbindung und sendet viele Requests darüber, bis sie abbricht oder der Generator gestoppt wird.

**State (Zeile 42-57):**
```python
state = {
    "running":      False,
    "rate":         20,
    "payload_size": 512,
    "stream_count": 1,
    "use_0rtt":     False,
    "pattern":      "constant",  # "constant" | "random"
    ...
    "zero_rtt_used":      False, # read-only: ob 0-RTT wirklich genutzt wurde
}
```
`zero_rtt_used` ist über `_READONLY_FIELDS = {"zero_rtt_used"}` explizit vor `PATCH /config`-Überschreibung geschützt (Zeile 165) – es ist eine **Beobachtung**, kein Eingabewert.

**0-RTT-Mechanismus** (Zeile 70-92, 243-266):
```python
def _on_session_ticket(ticket):
    global _session_ticket
    _session_ticket = ticket   # vom Server geschickt, für künftige Resumption gecacht
```
Beim (Re-)Connect: wenn `use_0rtt=True` und ein gecachtes Ticket existiert, wird es als `config.session_ticket` angeboten. Nach erfolgreichem Connect wird geprüft: `zero_rtt_used = bool(cached_ticket is not None and conn._quic.tls.session_resumed)` – also **echte** Bestätigung, nicht nur "war angefragt".

**Der `_quiet_aioquic_unraisablehook`-Workaround** (Zeile 77-92): Beim Schließen einer HTTP/3-Verbindung versucht aioquic manchmal noch, auf bereits geschlossenen, vom Server initiierten unidirektionalen Streams (QPACK-Encoder/Decoder) ein FIN zu senden, was korrekt mit `ValueError` abgelehnt wird – harmlos, aber spammt stderr bei jedem Request. Der Hook filtert gezielt **nur** diese eine bekannte Meldung heraus und leitet alles andere normal weiter. Ein gutes Beispiel dafür, dass nicht jede gefilterte Exception ein Bug-Versteck ist – hier ist es dokumentiert und bewusst eng eingegrenzt.

**`_send_on_connection()`** (Zeile 184-233) – ein Sendezyklus auf einer bereits offenen Verbindung:
```python
payload = os.urandom(payload_size)   # EIN Payload, für alle Streams dieses Zyklus wiederverwendet
for _ in range(streams):
    stream_id = conn._quic.get_next_available_stream_id()
    h3.send_headers(stream_id=stream_id, headers=[...])
    h3.send_data(stream_id=stream_id, data=payload, end_stream=True)
conn.transmit()
await asyncio.sleep(0.005)   # kurze Wartezeit auf Antwort-Frames
```
Derselbe zufällige Payload wird über alle `streams` dieses Zyklus wiederverwendet (vermeidet wiederholte `os.urandom()`-Aufrufe bei hohen Stream-Zahlen); jeder Stream ist trotzdem ein unabhängiger, multiplexter HTTP/3-POST.

**`_run_connection()`** (Zeile 236-290) – die While-Schleife auf der offenen Verbindung:
```python
while True:
    with _lock:
        running, rate, pattern = state["running"], state["rate"], state["pattern"]
    if not running:
        return   # schließt die Verbindung sauber (via "async with")
    if rate <= 0:
        await asyncio.sleep(0.1)
        continue
    await _send_on_connection(conn, h3)
    interval = 1.0 / rate
    if pattern == "random":
        await asyncio.sleep(random.expovariate(1.0 / interval))
    else:
        await asyncio.sleep(interval)
```

**`_generate()`** (Zeile 293-308) – die äußere Schleife, die `_run_connection()` immer wieder aufruft und bei Fehlern (abgebrochene Verbindung) nach 1 Sekunde Backoff neu verbindet:
```python
async def _generate():
    while True:
        ...
        try:
            await _run_connection()
        except Exception:
            state["errors"] += 1
            await asyncio.sleep(1.0)
```

### Check-Fragen 2.3

1. Warum würde eine neue QUIC-Verbindung pro Request die gemessene Latenz verzerren – was genau würde dabei mitgemessen, das bei den anderen Protokollen nicht mitgemessen wird?
2. Was genau bedeutet `zero_rtt_used=True` – reicht es, dass `use_0rtt=True` gesetzt ist?
3. Warum ist `zero_rtt_used` in `_READONLY_FIELDS`, und was würde passieren, wenn ein Client versucht, es per `PATCH /config` zu setzen?

### 2.4 MQTT-Generator (`generators/mqtt/generator.py`, 386 Zeilen)

Einziger Generator mit echtem **Pub/Sub-Modell**: er publiziert **und** subscribed gleichzeitig auf dieselben Topics.

**Warum subscribed der Generator seine eigenen Topics?** Damit das Fan-out des Brokers (Publish → Broker → Subscriber) für die Wireshark-Analyse sichtbar wird. Ohne eigene Subscription würde man im Capture nur die Hälfte des echten MQTT-Verkehrs sehen (nur PUBLISH vom Client zum Broker, nicht den Broker-seitigen Re-Forward an Subscriber).

**State (Zeile 69-86):**
```python
state = {
    "running":      False,
    "rate":         10,       # messages per second
    "payload_size": 128,
    "qos":          1,
    "topic_count":  len(BASE_TOPICS),  # = 5
    "qos_distribution": None,          # optional [w0, w1, w2], überschreibt qos wenn gesetzt
    "pattern":      "constant",        # "constant" | "random"
    ...
}
```

**`_topic_list(n)`** (Zeile 57-64):
```python
BASE_TOPICS = ["sensors/temperature", "sensors/humidity", "sensors/pressure",
               "actuators/control", "status/heartbeat"]
MAX_TOPIC_COUNT = 20

def _topic_list(n: int) -> list[str]:
    n = max(1, min(MAX_TOPIC_COUNT, n))
    if n <= len(BASE_TOPICS):
        return BASE_TOPICS[:n]
    return BASE_TOPICS + [f"load/topic-{i}" for i in range(len(BASE_TOPICS), n)]
```
Die ersten 5 Topics (bis `topic_count<=5`) sind die "beschreibenden" Basis-Topics; darüber hinaus werden generische `load/topic-5`, `load/topic-6`, ... angehängt, bis maximal 20.

**`_resubscribe()`** (Zeile 181-190): Wenn sich `topic_count` ändert, werden genau die neu hinzugekommenen Topics subscribed und die herausgefallenen unsubscribed – kein komplettes Neu-Subscriben aller Topics bei jeder Änderung.

**QoS-Verteilung** (`_normalize_qos_distribution`, Zeile 166-174):
```python
def _normalize_qos_distribution(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return [float(value.get(str(q), value.get(q, 0)) or 0) for q in (0, 1, 2)]
    return [float(w) for w in value]
```
Akzeptiert sowohl `[w0, w1, w2]` (Liste) als auch `{"0": w0, "1": w1, "2": w2}` (Dict – genau die Form, die YAML erzeugt, wenn man `qos_distribution: {0: 40, 1: 40, 2: 20}` schreibt, weil YAMLs Integer-Keys über JSON zu String-Keys werden) und normalisiert beides zu einer Liste.

**`_send_loop()`** (Zeile 231-295):
```python
interval = 1.0 / rate
if pattern == "random":
    sleep_time = random.expovariate(1.0 / interval)
else:
    sleep_time = interval
...
topic   = random.choice(_topic_list(topic_count))
publish_qos = qos
if qos_dist and len(qos_dist) == 3 and all(w >= 0 for w in qos_dist) and sum(qos_dist) > 0:
    publish_qos = random.choices(QOS_OPTIONS, weights=qos_dist)[0]
result = mqtt_client.publish(topic, payload, qos=publish_qos)
```
Ist eine gültige `qos_distribution` gesetzt (3 nicht-negative Zahlen, Summe > 0), wird die QoS pro Publish **gewichtet zufällig** gezogen; sonst die fixe `qos`. **QoS-Unterschied im Wireshark:** QoS 0 = ein PUBLISH-Paket pro Nachricht (fire-and-forget); QoS 1 = PUBLISH + PUBACK (2 Pakete); QoS 2 = PUBLISH + PUBREC + PUBREL + PUBCOMP (4 Pakete, "exactly once"-Handshake). Das erklärt, warum `mqtt_heavy.yaml`s QoS-2-Phase bei halber Message-Rate trotzdem 4x mehr Pakete erzeugt (siehe Modul 5).

### Check-Fragen 2.4

1. Warum subscribed der MQTT-Generator seine eigenen Topics, statt nur zu publizieren?
2. Welche Form kann `qos_distribution` als Eingabe annehmen, und warum genau diese zwei Formen?
3. Wie viele Pakete erzeugt eine einzelne MQTT-Nachricht bei QoS 0 vs. QoS 2, und was bedeutet das für die Interpretation einer Rate von "50 Nachrichten/Sek bei QoS 2"?

### Check-Fragen Modul 2 (übergreifend, alle 4 Generatoren)

1. Welche Felder/Methoden tauchen in **allen** 4 Generatoren in fast identischer Form auf, und warum (Code-Konsistenz vs. Copy-Paste)?
2. Erkläre für jeden Generator, was genau "ein Sendezyklus" bedeutet (1 Paket? 1 Request? mehrere Streams? mehrere Pakete?).
3. Wie wird `fault_rate` jeweils umgesetzt – wird wirklich nichts gesendet, oder wird nur der Fehler simuliert?
4. Was bewirkt `pattern: "random"` konkret anders im Code als `"constant"`, und warum ist `random.expovariate(1/interval)` (bzw. `np.random.exponential(interval)`) die richtige Wahl, um einen Poisson-Prozess mit gleichem Mittelwert zu erzeugen?
5. Nenne für jeden Generator ein Feld, das **nur** dieses eine Protokoll hat, und erkläre warum genau dieses Protokoll dieses Feld braucht.

---

## Modul 3 – Der Controller (`controller/main.py`, 724 Zeilen)

Das komplexeste Modul – das Steuerzentrum, das alle 4 Generatoren orchestriert, YAML-Profile lädt, Phasen abspielt und Adaptive Control betreibt.

### 3.1 Datenmodelle & State

**Konstanten** (Zeile 72-93):
```python
GENERATORS = {
    "gen-http2":  os.getenv("GEN_HTTP2_URL",  "http://gen-http2:7001"),
    "gen-quic":   os.getenv("GEN_QUIC_URL",   "http://gen-quic:7002"),
    "gen-mqtt":   os.getenv("GEN_MQTT_URL",   "http://gen-mqtt:7003"),
    "gen-tcpudp": os.getenv("GEN_TCPUDP_URL", "http://gen-tcpudp:7004"),
}
METRICS_URL  = os.getenv("METRICS_URL", "http://metrics:9090")
CONFIG_DIR   = "/app/config"
LOG_TZ = ZoneInfo(os.getenv("LOG_TZ", "Europe/Berlin"))

RATE_FIELDS = {
    "gen-http2":  ["rate"],
    "gen-quic":   ["rate"],
    "gen-mqtt":   ["rate"],
    "gen-tcpudp": ["tcp_rate", "udp_rate"],
}
```
`LOG_TZ` existiert, weil Docker-Container standardmäßig UTC laufen, aber der Browser (Dashboard) lokale Zeit (Europe/Berlin) verwendet – ohne diese Korrektur wären Controller-Log-Einträge 1-2 Stunden gegenüber den Dashboard-eigenen Log-Einträgen verschoben.

`RATE_FIELDS` ist zentral wichtig: Es ist die einzige Stelle, die weiß, wie das "Rate"-Feld bei jedem Generator heißt. TCP/UDP hat **zwei** Ratenfelder (`tcp_rate`, `udp_rate`), alle anderen genau eines (`rate`). Sowohl `_ramp_rates()` (Warmup/Cooldown) als auch der Adaptive-Control-Loop nutzen `RATE_FIELDS`, um protokoll-agnostisch zu bleiben, statt 4x hartkodierte Sonderfälle zu brauchen.

**Globaler State** (Zeile 97-107):
```python
state: dict[str, Any] = {
    "running":         False,
    "active_profile":  None,
    "active_phase":    None,
    "phase_task":      None,   # Hintergrund-Task für den Phasen-Ablauf
    "adaptive_enabled": False,
    "adaptive_task":    None,
    "adaptive_status":  {},    # Generator → letzte Adaptive-Entscheidung
    "ramp_status":      None,  # {"phase": "warmup"|"cooldown", "progress": 0.0-1.0}
}
log_entries: list[dict] = []
```

**Wichtige Pydantic-Modelle:** `StatusResponse` (für `GET /status`, bündelt `running`, `active_profile`, `active_phase`, `adaptive_enabled`, `adaptive_status`, `ramp_status`, `generators` [Live-Status jedes Generators], `metrics` [aggregierte Metrics-Collector-Antwort], `log` [letzte 50 Einträge]), `RampStatus` (`phase`: "warmup"|"cooldown", `progress`: 0.0-1.0), `AdaptiveDecision` (`multiplier`, `error_rate`, `action`, `applied`, `checked_at`).

### 3.2 Start/Stop & Profile laden

**`_load_yaml(profile)`** (Zeile 210-215): liest `config/<profile>.yaml`, wirft `FileNotFoundError`, wenn die Datei nicht existiert (→ wird im Endpoint zu HTTP 404).

**`_phase_to_gen_configs(phase)`** (Zeile 259-274) – übersetzt einen YAML-Phasenblock in pro-Generator-Configs:
```python
def _phase_to_gen_configs(phase: dict) -> dict[str, dict]:
    p = phase.get("protocols", {})
    return {
        "gen-http2":  {k: v for k, v in p.get("http2", {}).items()},
        "gen-quic":   {k: v for k, v in p.get("quic",  {}).items()},
        "gen-mqtt":   {k: v for k, v in p.get("mqtt",  {}).items()},
        "gen-tcpudp": {
            "tcp_rate":    p.get("tcp", {}).get("rate", 0),
            "udp_rate":    p.get("udp", {}).get("rate", 0),
            "packet_size": p.get("tcp", {}).get("packet_size", 512),
            **p.get("tcpudp", {}),   # mode, mean_interval, min_size, max_size, tcp_ratio, pattern, ...
        },
    }
```
Auffällig: `http2`/`quic`/`mqtt` werden 1:1 durchgereicht (jedes YAML-Feld landet unverändert im `PATCH`-Body an den jeweiligen Generator), aber `tcpudp` ist ein **Sonderfall**: die YAML-Profile schreiben `tcp:`/`udp:` als getrennte Blöcke (jeweils mit eigenem `rate`), aber der Generator selbst kennt nur ein einziges `state`-Dict mit `tcp_rate`/`udp_rate`. Diese Funktion baut die Brücke. Zusätzlich gibt es einen optionalen `tcpudp:`-Block (für Stealth-Mode-Felder wie `mean_interval`, `min_size`, `max_size`), der per `**p.get("tcpudp", {})` einfach durchgemischt wird.

**Warmup-Logik in `/start`** (Zeile 465-503):
```python
if phases:
    configs = _phase_to_gen_configs(phases[0])
    warmup = profile_data.get("global", {}).get("warmup", 0)
    start_configs = configs
    if warmup > 0:
        start_configs = {
            name: {**cfg, **{k: 0 for k in RATE_FIELDS.get(name, []) if k in cfg}}
            for name, cfg in configs.items()
        }
    for name, cfg in start_configs.items():
        if cfg:
            await _call("post", f"{GENERATORS[name]}/start", json=cfg)
```
Ist ein `warmup` konfiguriert, werden die Generatoren mit **Rate = 0** gestartet (alle anderen Felder wie `payload_size` bleiben wie in Phase 1 konfiguriert) – die eigentliche Hochrampung übernimmt `_run_phases()` (siehe 3.3).

### 3.3 Phasen-Ablaufsteuerung: `_run_phases()`

**`_ramp_rates()`** (Zeile 218-256) – das gemeinsame Warmup/Cooldown/Ramp-Mechanismus:
```python
async def _ramp_rates(configs, duration, direction, label):
    targets = {}
    for name, cfg in configs.items():
        rate_fields = RATE_FIELDS.get(name, [])
        rates = {k: float(cfg[k]) for k in rate_fields if k in cfg and isinstance(cfg[k], (int, float))}
        if rates:
            targets[name] = rates
    if not targets or duration <= 0:
        return

    steps = max(1, min(20, round(duration)))
    step_duration = duration / steps

    for i in range(1, steps + 1):
        if not state["running"]:
            break
        await asyncio.sleep(step_duration)
        frac = i / steps
        applied_frac = frac if direction == "up" else (1 - frac)
        for name, rates in targets.items():
            scaled = {k: max(0, round(v * applied_frac, 3)) for k, v in rates.items()}
            await _call("patch", f"{GENERATORS[name]}/config", json=scaled)
        state["ramp_status"] = {"phase": label, "progress": round(frac, 2)}
    state["ramp_status"] = None
```
Maximal **20 Schritte**, egal wie lang `duration` ist (z.B. bei `warmup: 30` → `step_duration = 30/20 = 1.5s`). Bei `direction="up"` steigt `applied_frac` linear von `1/20` auf `1.0`; bei `"down"` sinkt es von `19/20` auf `0.0`. Wichtig: **nur** die Felder aus `RATE_FIELDS` werden geändert – `payload_size`, `mode` etc. bleiben unangetastet, weil die schon vorher per normaler Phase-Config gesetzt wurden.

Das ist gleichzeitig der Mechanismus für **zwei** Anforderungen aus der Aufgabenstellung: das globale Warmup/Cooldown **und** das "Ramping (linearer Anstieg über Zeit)"-Sendemuster – die allmähliche Ratenänderung ist direkt als Rampe im Wireshark-I/O-Graph sichtbar.

**`_run_phases()`** (Zeile 407-451) – der Gesamtablauf:
```python
async def _run_phases(profile_data: dict):
    warmup, cooldown = global_cfg.get("warmup", 0), global_cfg.get("cooldown", 0)
    phases = profile_data.get("phases", [])

    if phases and warmup > 0 and state["running"]:
        state["active_phase"] = "warmup"
        await _ramp_rates(_phase_to_gen_configs(phases[0]), warmup, "up", "warmup")

    for phase in phases:
        if not state["running"]:
            break
        state["active_phase"] = phase.get("name", "?")
        configs = _phase_to_gen_configs(phase)
        for name, cfg in configs.items():
            if cfg:
                await _call("patch", f"{GENERATORS[name]}/config", json=cfg)

        adaptive_cfg = phase.get("adaptive_control")
        if adaptive_cfg and adaptive_cfg.get("enabled"):
            _start_adaptive(adaptive_cfg)
        else:
            _stop_adaptive()

        await asyncio.sleep(phase.get("duration", 60))

    _stop_adaptive()

    if phases and cooldown > 0 and state["running"]:
        state["active_phase"] = "cooldown"
        await _ramp_rates(_phase_to_gen_configs(phases[-1]), cooldown, "down", "cooldown")

    state["running"] = False
    state["active_phase"] = None
```
Ablauf: **Warmup** (rampt Phase-1-Raten von 0 hoch) → **Phase 1, 2, ... N** (jede patcht alle Generatoren neu, startet/stoppt Adaptive Control je nach `adaptive_control.enabled`, wartet `duration` Sekunden) → **Cooldown** (rampt die Raten der **letzten** Phase auf 0 runter) → `running=False`. Wichtig: Adaptive Control wird bei **jedem** Phasenwechsel neu bewertet – eine Phase ohne `adaptive_control`-Block schaltet eine zuvor aktive Steuerung automatisch wieder ab (`_stop_adaptive()`).

### 3.4 Adaptive Control

**`_get_current_rates()`** (Zeile 279-288): liest per `GET /status` die aktuelle Rate jedes Generators als **Baseline**, bevor der Loop überhaupt zu skalieren beginnt.

**`_adaptive_loop()`** (Zeile 291-386) – die eigentliche autonome Regelschleife:
```python
async def _adaptive_loop(adaptive_cfg: dict):
    check_interval = adaptive_cfg.get("check_interval", 10)
    up_factor, down_factor = adaptive_cfg.get("scale_up_factor", 1.1), adaptive_cfg.get("scale_down_factor", 0.8)
    max_multiplier, min_multiplier = adaptive_cfg.get("max_multiplier", 5.0), adaptive_cfg.get("min_multiplier", 0.2)
    error_rate_max = up_cfg.get("error_rate_max", 0.0)
    error_rate_min = down_cfg.get("error_rate_min", 1.0)

    baseline = await _get_current_rates()
    multipliers = {name: 1.0 for name in baseline}

    while True:
        await asyncio.sleep(check_interval)
        metrics = await _call("get", f"{METRICS_URL}/metrics")
        gens = metrics.get("generators", {})

        for name, base_rates in baseline.items():
            g = gens.get(name, {})
            d_packets = max(0, g.get("packets_sent",0) - prev["packets"])
            d_errors  = max(0, g.get("errors",0) - prev["errors"])
            error_rate = (d_errors / d_packets) if d_packets > 0 else 0.0

            action = "hold"
            if d_packets == 0:
                action = "hold"
            elif error_rate >= error_rate_min or (latency over threshold):
                action = "down"
            elif error_rate <= error_rate_max and (latency under threshold):
                action = "up"

            if action == "up":   multipliers[name] = min(max_multiplier, multipliers[name] * up_factor)
            elif action == "down": multipliers[name] = max(min_multiplier, multipliers[name] * down_factor)

            applied = {k: max(1, round(v * multipliers[name])) for k, v in base_rates.items()}
            state["adaptive_status"][name] = {...}
            if action in ("up", "down"):
                await _call("patch", f"{GENERATORS[name]}/config", json=applied)
```
Wichtige Details:
- Die Fehlerrate wird **pro Check-Intervall neu berechnet** (`d_packets`/`d_errors` = Differenz seit letztem Check, nicht die kumulierte Gesamtrate seit Start) – sonst würde ein früher Fehlerausschlag noch nach Stunden die Statistik verzerren.
- Der `multiplier` ist **multiplikativ** und wird bei jedem "up"/"down" weiter potenziert (`×1.2`, `×1.2×1.2`, ...), begrenzt durch `min_multiplier`/`max_multiplier` – das ist klassisches exponentielles Hoch-/Herunterskalieren, kein additiver Schritt.
- `applied = base_rate * multiplier` – die Basis bleibt die **zu Beginn gemessene** Rate (`baseline`), nicht die zuletzt angewendete – Rundungsfehler akkumulieren sich also nicht über viele Zyklen.
- Latenz-Schwellen (`latency_max_ms`/`latency_min_ms`) werden nur honoriert, wenn der Generator `latency_ms` überhaupt meldet (aktuell `gen-http2` und `gen-tcpudp`).

**`/adaptive/toggle`** (Zeile 679-708) – manuelles Ein-/Ausschalten unabhängig vom YAML-Profil, mit fest hinterlegten "Demo-Defaults" (Check alle 5s, hoch bei ≤0% Fehlern, runter bei ≥5% Fehlern oder ≥150ms Latenz, ×1.2 hoch/×0.5 runter) – schnell und dramatisch genug, um innerhalb von 5-10 Sekunden sichtbar auf die Fault-Injection-Slider im Dashboard zu reagieren.

### 3.5 Status-Aggregation & Logging

**`_call()`** (Zeile 201-207) – der zentrale HTTP-Helper:
```python
async def _call(method: str, url: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await getattr(client, method)(url, **kwargs)
            return r.json()
    except Exception as e:
        return {"error": str(e)}
```
Schlägt ein Call fehl (Generator nicht erreichbar, Timeout), wird **kein** Exception nach oben geworfen, sondern ein `{"error": ...}`-Dict zurückgegeben – der Aufrufer (z.B. `/status`) bleibt dadurch robust, auch wenn ein einzelner Generator gerade nicht antwortet.

**`GET /status`** (Zeile 611-637): fragt **sequenziell** den Status jedes der 4 Generatoren ab (`for name, url in GENERATORS.items()`), holt zusätzlich `GET /metrics` vom Collector, und packt alles inklusive der letzten 50 Log-Einträge in eine Antwort. Das ist genau die Antwort, die das Dashboard alle 2 Sekunden pollt.

**`_log()`** (Zeile 110-119):
```python
def _log(message: str, level: str = "info"):
    entry = {"time": datetime.now(LOG_TZ).strftime("%H:%M:%S"), "level": level, "message": message}
    log_entries.append(entry)
    if len(log_entries) > 200:
        log_entries.pop(0)
    print(f"[{entry['time']}] {message}")
```
Begrenzt auf 200 Einträge im Speicher (älteste fliegen raus), aber `/status` gibt sowieso nur die letzten 50 zurück. `level` ist eine von `"info"`/`"success"`/`"adaptive"` – das Dashboard färbt die Log-Zeilen je nach Level ein.

### Check-Fragen Modul 3

1. Zeichne (verbal) den Lebenszyklus eines Profils: von `POST /start` bis `state["running"] = False`.
2. Was passiert, wenn `warmup = 0` aber `cooldown = 15` ist? Was passiert **nicht**?
3. Warum ist `_ramp_rates()` bewusst protokoll-agnostisch (über `RATE_FIELDS`) statt 4x hartkodiert für jeden Generator?
4. Wie unterscheidet sich Adaptive Control von der Warmup/Cooldown-Ramp – können beide gleichzeitig aktiv sein, und was würde dabei passieren (wer "gewinnt", wenn beide gleichzeitig versuchen, dieselbe Rate zu setzen)?
5. `active_phase` kann u.a. einen YAML-Phasennamen, `"warmup"` oder `"cooldown"` enthalten – an welchen Stellen im Code (Controller **und** Dashboard) wird das ausgewertet?

---

## Modul 4 – Dashboard (`dashboard/index.html`, 1758 Zeilen)

Eine einzige HTML-Datei: CSS im `<head>`, HTML-Struktur im `<body>`, das gesamte JavaScript am Ende in einem `<script>`-Block. Kein Build-Step, kein Framework – reines Vanilla-JS, das den Controller über `fetch()` anspricht.

### 4.1 Struktur & Layout

Grobgliederung von oben nach unten: Header (Status-Punkt, Start/Stop-Button) → KPI-Leiste (4 Kacheln: Total packets, Transfer/sec, Error rate, Active protocols) → Profil-Presets (3 Buttons: HTTP/2 Dominant, MQTT Dominant, Balanced) → Adaptive-Control-Panel → Generatoren-Übersicht (Rate-Slider + On/Off pro Protokoll) → 4x protokollspezifische Settings-Sektionen (HTTP/2, QUIC, MQTT, TCP/UDP) → Fault-Injection-Panel → Angreifer-Sicht ("Attacker's View") → System-Log.

CSS-Konvention: `.gen-table`/`.gen-header`/`.gen-row` bilden überall im Dashboard dasselbe Tabellen-Layout (CSS-Grid mit fester Spaltenbreite), egal ob für Generatoren, Fault Injection oder Adaptive-Status – eine einzige visuelle Sprache für alle "Zeile pro Protokoll"-Bereiche.

### 4.2 Polling & Render-Zyklus

```javascript
poll();
pollInterval = setInterval(poll, 2000);

async function poll() {
  try {
    const res  = await fetch(`${API}/status`, {signal: AbortSignal.timeout(3000)});
    const data = await res.json();
    hideError();
    renderStatus(data);
  } catch (e) {
    showError('Controller unreachable; is docker-compose up running?');
    setSystemStatus(false, 'Offline');
  }
}
```
Alle 2 Sekunden ein `GET /status` an den Controller, mit 3-Sekunden-Timeout. Bei Fehler: roter Banner + Status auf "Offline" – keine Exception, die die App zum Stillstand bringt.

**Das "Render-Sync"-Pattern** – zieht sich durch die **gesamte** `renderStatus()`-Funktion:
```javascript
const slider = document.getElementById('range-' + name);
if (document.activeElement !== slider) {
  slider.value = g.rate;
}
```
**Warum ist das nötig?** Ohne diese Prüfung würde der Server-Wert bei jedem 2-Sekunden-Poll den Slider zurücksetzen, **während der Nutzer ihn gerade zieht** – der Regler würde unter der Maus "zucken"/zurückspringen, weil der eigene Eingabewert ständig vom alten Server-Stand überschrieben wird. Die Lösung: Nur synchronisieren, wenn das Element gerade **nicht** fokussiert ist (`document.activeElement !== el`). Dieses Pattern taucht bei **jedem** interaktiven Element im Dashboard auf: Rate-Slider, Toggle-Switches, Mode-Select, Fault-Slider, Latency-Slider, Payload-Slider, Pattern-Select, QoS-Felder, Burst-Felder – immer dieselbe Zeile, immer derselbe Grund.

**Formatierungs-Helfer** (Zeile 1050-1072):
```javascript
function fmtNum(n) { return Number(n).toLocaleString('en-US'); }      // 1234 → "1,234"
function fmtBytes(b) {                                                 // 2048 → "2.0 KB/s"
  if (b < 1024) return b + ' B/s';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB/s';
  return (b/1048576).toFixed(1) + ' MB/s';
}
function fmtUptime(s) { /* Sekunden → "HH:MM:SS" */ }
function now() { return new Date().toLocaleTimeString('en-GB', {...}); }  // lokale Browser-Zeit
```

### 4.3 Interaktive Controls & PATCH-Flow

**`patchGen()`** – die einzige Stelle, die wirklich Config-Änderungen an den Controller schickt:
```javascript
async function patchGen(name, body) {
  try {
    await fetch(`${API}/generator/${name}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch(e) {}
}
```
Das ruft `PATCH /generator/{name}` am **Controller** auf (nicht direkt am Generator!) – der Controller leitet das per `_call("patch", f"{url}/config", ...)` an den jeweiligen Generator weiter (Modul 3, Endpoint `patch_generator`). Diese Indirektion erlaubt zentrales Logging jeder Änderung.

**Debouncing über `setTimeout(400)`:**
```javascript
let faultTimers = {};
function onFaultChange(name, value) {
  const pct = parseInt(value);
  document.getElementById('fault-disp-' + name).textContent = pct + '%';   // SOFORT: visuelles Feedback
  clearTimeout(faultTimers[name + '-fault']);
  faultTimers[name + '-fault'] = setTimeout(() => {
    patchGen(name, {fault_rate: pct / 100});                                // VERZÖGERT: Netzwerk-Call
    addLog(now(), `... error rate set to ${pct}%`, pct > 0 ? 'error' : 'info');
  }, 400);
}
```
**Warum debounced?** Beim Ziehen eines Sliders feuert `oninput` dutzende Male pro Sekunde. Ohne Debounce würde jede einzelne Mausbewegung einen eigenen `PATCH`-Request auslösen – Netzwerk-Flut und unnötige Last auf dem Controller/Generator. Die Lösung: Die Anzeige (`textContent`) wird **sofort** aktualisiert (für direktes visuelles Feedback), aber der eigentliche `PATCH`-Call wird in einen Timer gepackt, der bei jeder neuen Bewegung zurückgesetzt wird (`clearTimeout` + neuer `setTimeout`). Erst wenn 400ms **ohne** weitere Bewegung vergehen, geht der Call tatsächlich raus. Dasselbe Muster wiederholt sich identisch für `onPayloadChange`, `onMethodMixChange`, `onStreamsChange`, `onTopicCountChange`, `onQosWeightChange`, `onBurstSizeChange`, `onBurstIntervalChange`, `onHttp2BurstFieldChange` – jeweils mit eigenem Timer-Key in `faultTimers`, damit unterschiedliche Felder sich nicht gegenseitig canceln.

Nicht alle Handler debouncen – Toggle-Switches und Selects (`onPatternChange`, `onModeChange`, `onZeroRttToggle`, `onPathsChange`) feuern **sofort**, weil sie keine kontinuierliche Drag-Bewegung sind, sondern einen diskreten Klick/Auswahl-Wechsel darstellen, bei dem es kein "Flut"-Problem gibt.

**Beispiel-Pfad eines Wertes – Rate-Slider HTTP/2:**
1. Nutzer zieht `<input type="range" id="range-gen-http2" oninput="onRateChange('gen-http2', this.value)">`
2. `onRateChange()` aktualisiert sofort die Anzeige (`rate-gen-http2`-Span) und setzt einen 400ms-Timer
3. Nach 400ms ohne weitere Bewegung: `patchGen('gen-http2', {rate: parseInt(value)})`
4. `fetch(PATCH http://localhost:8000/generator/gen-http2, body: {"rate": N})`
5. Controller-Endpoint `patch_generator()` validiert den Namen, ruft `_call("patch", "http://gen-http2:7001/config", json={"rate": N})`, loggt die Änderung
6. Der HTTP/2-Generator selbst übernimmt im `PATCH /config`-Handler: `state.update({k: v for k, v in body.model_dump(exclude_none=True).items() if k in state})` → `state["rate"]` ändert sich
7. Beim nächsten `_generate()`-Zyklus liest der Generator den neuen `state["rate"]` und sendet entsprechend schneller/langsamer
8. Beim nächsten Dashboard-Poll (≤2s später) meldet `GET /status` den neuen `rate`-Wert zurück, und `renderStatus()` synchronisiert den Slider (außer der Nutzer zieht ihn gerade wieder – Render-Sync-Pattern)

### 4.4 Phasen-/Ramp-/Adaptive-Anzeige

**`PHASE_DESCRIPTIONS`** (Zeile 989-1001) – ein statisches JS-Objekt mit einem Beschreibungstext pro Phasenname (für alle 9 Phasennamen aus den 3 YAML-Profilen, plus `warmup`/`cooldown`). Wird in `renderStatus()` als Tooltip-Text unter dem Phasennamen angezeigt.

**Ramp-Anzeige** (Zeile 1111-1131):
```javascript
const ramp = data.ramp_status;
if (ramp && (data.active_phase === 'warmup' || data.active_phase === 'cooldown')) {
  const pct = Math.round(ramp.progress * 100);
  phaseEl.textContent = `Phase: ${label} - rates ramping ${ramp.phase === 'warmup' ? 'up' : 'down'} (${pct}%)`;
} else {
  phaseEl.textContent = data.active_phase ? `Phase: ${data.active_phase}` : '';
}
```
Solange `ramp_status` vom Controller `nicht null` ist (also während Warmup/Cooldown läuft), zeigt das Dashboard einen Live-Fortschrittsbalken in Prozent (`ramp.progress`, 0.0-1.0) statt nur des Phasennamens.

**Adaptive-Status-Tabelle** (`renderAdaptive()`, Zeile 1413-1454): zeigt pro Generator Fehlerrate, aktuellen Multiplikator, angewandte Rate(n) und Aktion (↑/↓/-) – baut die Tabellenzeilen dynamisch aus `data.adaptive_status` (demselben Dict, das der Controller in Modul 3 in `state["adaptive_status"]` pflegt).

**Angreifer-Sicht** (`renderAttacker()`, Zeile 1456-1494) – das einzig "analytische" UI-Element: berechnet pro Protokoll, was ein passiver Beobachter (z.B. Wireshark am Kabel) **ohne Entschlüsselung** ableiten könnte, rein aus Paketgröße, Timing und Metadaten. `FINGERPRINT_NOTES` (Zeile 1018-1046) enthält pro Protokoll eine Funktion, die aus dem aktuellen Live-Status einen Beschreibungstext baut, z.B. für TCP/UDP:
```javascript
'gen-tcpudp': (g) => {
  const sizeNote = g.mode === 'stealth' ? 'variable packet size' : 'fixed packet size';
  const patternNote = {
    constant: 'fixed send intervals (constant rate)',
    periodic_burst: `periodic bursts of ${g.burst_size} packets every ${Math.round(g.burst_interval*1000)}ms`,
    random: 'Poisson-distributed (random) intervals',
  }[g.pattern] || 'fixed send intervals (constant rate)';
  return `${sizeNote} with ${patternNote}` + (g.mode === 'stealth' && g.pattern === 'random'
    ? '; resembles real user traffic, statistically hard to distinguish from background noise.'
    : '; produces a recognizable statistical fingerprint.');
}
```
Das ist die direkte UI-Umsetzung von **Task 3 (Behavioral Fingerprinting)** aus der Aufgabenstellung.

### Check-Fragen Modul 4

1. Warum würde ohne das `activeElement`-Pattern ein Slider "zucken", während man ihn bedient – was genau überschreibt was, und alle wie viele Sekunden?
2. Beschreibe den vollständigen Pfad eines Wertes vom Schieberegler bis zur State-Variable im Generator-Prozess (mind. 6 Schritte).
3. Welche Handler-Funktionen debouncen mit `setTimeout(400)`, welche feuern sofort – und warum genau diese Aufteilung?
4. Was zeigt das Dashboard an, wenn `ramp_status` nicht `null` ist, und woher kommen `phase`/`progress` ursprünglich (welches Modul berechnet sie)?

---

## Modul 5 – YAML-Profile & Konfigurationsflexibilität

**Dateien:** `config/balanced.yaml`, `config/http2_heavy.yaml`, `config/mqtt_heavy.yaml`

Gemeinsame Struktur: ein `global`-Block (`duration`, `warmup`, `cooldown` – global für das ganze Profil) und eine Liste `phases`, jede mit `name`, `duration`, optional `description`, optional `adaptive_control`, und einem `protocols`-Block mit Unter-Blocks pro Protokoll (`http2`, `quic`, `mqtt`, `tcp`, `udp`, `tcpudp`).

### `balanced.yaml` – die "Alles-Demo"

```yaml
global:
  duration: 300
  warmup: 30
  cooldown: 30

phases:
  - name: balanced_constant      # Phase 1, 100s
    description: "Even distribution, constant rate; ideal for protocol hierarchy analysis"
    protocols:
      http2: {rate: 50, payload_size: 2048, method_distribution: {GET: 70, POST: 30}, concurrent_streams: 3}
      quic:  {rate: 40, payload_size: 1024, stream_count: 2, use_0rtt: false}
      mqtt:  {rate: 30, payload_size: 256, qos_distribution: {0: 40, 1: 40, 2: 20}, topics: [...]}
      tcp:   {rate: 15, packet_size: 512, mode: normal}
      udp:   {rate: 10, packet_size: 256}

  - name: balanced_with_stealth  # Phase 2, 100s
    description: "Stealth mode active; for behavioral fingerprinting analysis"
    protocols:
      http2:  {rate: 50, payload_size: 2048, pattern: random}
      quic:   {rate: 40, payload_size: 1024, pattern: random}
      mqtt:   {rate: 30, payload_size: 256, qos_distribution: {0: 50, 1: 30, 2: 20}, pattern: random}
      tcpudp: {mode: stealth, mean_interval: 0.100, min_size: 64, max_size: 1400, tcp_ratio: 60, pattern: random}

  - name: balanced_adaptive      # Phase 3, 100s
    description: "Adaptive Control active; system automatically adjusts rates based on error rate/latency"
    adaptive_control:
      enabled: true
      check_interval: 5
      scale_up_threshold:   {error_rate_max: 0.0, latency_max_ms: 50}
      scale_down_threshold: {error_rate_min: 0.05, latency_min_ms: 150}
      scale_up_factor: 1.2
      scale_down_factor: 0.5
      min_multiplier: 0.1
      max_multiplier: 5.0
    protocols:
      http2: {rate: 50, payload_size: 2048}
      quic:  {rate: 40}
      mqtt:  {rate: 30}
      tcp:   {rate: 15, mode: normal}
```
Kernidee jeder Phase: **balanced_constant** = Baseline für Protokollhierarchie-Analyse (Task 1/2), **balanced_with_stealth** = kombiniert Stealth-Mode (Größe/Timing-Tarnung bei TCP/UDP) **und** Random/Poisson-Pattern auf **allen** 4 Protokollen gleichzeitig – das ist die einzige Phase im ganzen Projekt, die Task 3 (Fingerprinting) und Task 5 (Temporal Analysis) gemeinsam zeigt. **balanced_adaptive** = aktiviert Adaptive Control mit aggressiven, demo-tauglichen Schwellen (Check alle 5s) für Task 4 (Failure Visibility).

### `http2_heavy.yaml` – HTTP/2-Fokus

```yaml
global: {duration: 300, warmup: 15, cooldown: 15}
phases:
  - name: http2_dominant   # 120s: hohe HTTP/2-Last, andere Protokolle minimal
    protocols:
      http2: {rate: 150, payload_size: 8192, method_distribution: {GET:60,POST:40}, concurrent_streams: 8, pattern: constant}
      mqtt: {rate: 5, qos: 0, payload_size: 128}
      tcp: {rate: 3, packet_size: 512, mode: normal}
      quic: {rate: 0}          # explizit AUS in dieser Phase
      udp: {rate: 2}

  - name: http2_burst      # 120s: periodische Bursts
    protocols:
      http2: {rate: 50, payload_size: 4096, pattern: periodic_burst, burst_rate: 400, burst_duration: 5, burst_interval: 30, concurrent_streams: 10}
      mqtt: {rate: 8, qos: 1}
      tcp: {rate: 5, mode: normal}

  - name: cooldown_phase   # 60s: reduzierte Raten vor Profil-Ende
    protocols:
      http2: {rate: 20, payload_size: 1024, pattern: constant}
      mqtt: {rate: 3}
```
Zeigt HTTP/2-Multiplexing unter Last (`concurrent_streams: 8`) und das `periodic_burst`-Pattern explizit konfiguriert (8x Burst-Rate für 5s alle 30s) – direkt sichtbar als periodische Spitzen im Wireshark-I/O-Graph.

### `mqtt_heavy.yaml` – QoS-Vergleich

```yaml
global: {duration: 300, warmup: 20, cooldown: 20}
phases:
  - name: mqtt_qos0_heavy        # 90s
    description: "QoS 0, fire and forget. One packet per message. Fast, unreliable."
    protocols:
      mqtt: {rate: 200, payload_size: 64, qos_distribution: {0:100, 1:0, 2:0}, topics: [5 Topics]}
      http2: {rate: 10}
      tcp: {rate: 2}

  - name: mqtt_qos2_comparison   # 90s
    description: "QoS 2, exactly once. Four packets per message. In Wireshark: 4x more traffic."
    protocols:
      mqtt: {rate: 50, payload_size: 64, qos_distribution: {0:0, 1:0, 2:100}, topics: [3 kritische Topics]}
      http2: {rate: 10}
      tcp: {rate: 2}

  - name: mqtt_mixed             # 90s
    description: "Mixed QoS levels, as in a real IoT system"
    protocols:
      mqtt: {rate: 100, payload_size: 128, qos_distribution: {0:50, 1:30, 2:20}, topics: [5 Topics]}
      http2: {rate: 15}
      quic: {rate: 5}
```
Kernidee: direkter Vergleich derselben Anwendung (Sensor-Daten/IoT) unter QoS 0 vs. QoS 2 vs. gemischt – bei QoS 2 ist die **Message-Rate** mit 50/s niedriger als bei QoS 0 (200/s), aber die **Paket-Rate** ist trotzdem höher (50 × 4 = 200 Pakete/s vs. 200 × 1 = 200 Pakete/s) – ein gutes Beispiel dafür, dass "Rate" im YAML immer **Nachrichten**rate meint, nicht Paketrate.

### Zusammenspiel mit Modul 3

Jedes Feld unter `protocols.http2.*`/`protocols.quic.*`/`protocols.mqtt.*` muss **exakt** so heißen wie das entsprechende Feld im jeweiligen Generator-`state`-Dict (Modul 2), weil `_phase_to_gen_configs()` die Felder unverändert durchreicht. Ein Tippfehler im YAML (z.B. `payload_size` statt `payload_size` mit Tippfehler `payload_siz`) würde **keinen Fehler werfen** – das Pydantic-Modell `GeneratorConfig` hat `model_config = ConfigDict(extra="allow")`, und der Generator filtert beim Update sowieso nur auf bekannte Keys (`if k in state`). Das falsch geschriebene Feld würde also einfach **stillschweigend ignoriert**, ohne Warnung – ein wichtiger Stolperfall beim Debuggen eigener YAML-Änderungen.

### Check-Fragen Modul 5

1. Nenne für jedes der 3 YAML-Profile die "Kernidee", die es demonstrieren soll, in einem Satz.
2. Was würde passieren, wenn ein Feld in der YAML existiert, das der jeweilige Generator nicht kennt (z.B. Tippfehler)? Würde der Controller einen Fehler werfen?
3. Wie würdest du eine 4. Phase hinzufügen, die nur QUIC mit 0-RTT testet (welche Felder, welche anderen Protokolle würdest du auf `rate: 0` setzen)?
4. Warum ist bei `mqtt_qos2_comparison` die `rate` niedriger als bei `mqtt_qos0_heavy`, obwohl die Beschreibung "4x more traffic" verspricht – ist das ein Widerspruch?

---

## Modul 6 – API-Dokumentation: OpenAPI/Swagger

**Dateien:** `docs/api/swagger.html`, `docs/api/openapi_controller.json`, `docs/api/openapi_gen-http2.json`, `docs/api/openapi_gen-quic.json`, `docs/api/openapi_gen-mqtt.json`, `docs/api/openapi_gen-tcpudp.json`

### Wie entstehen die OpenAPI-Specs?

FastAPI generiert für jeden Service (Controller + 4 Generatoren) automatisch eine OpenAPI-3.1-Spezifikation **aus den Pydantic-Modellen und Decorator-Metadaten**, die wir in den vorherigen Modulen gesehen haben – jedes `summary=`, `description=`, `tags=` und jedes Feld in `GeneratorConfig`/`StatusResponse` landet 1:1 im generierten JSON-Schema. Man muss dafür nichts händisch schreiben; `app.openapi()` (intern von FastAPI aufgerufen) baut das Schema aus dem Python-Code.

Ein Hilfsskript (`regen_swagger.py`, projektintern zur Doku-Pflege genutzt, nicht Teil der Laufzeit-Container) importiert jedes der 5 FastAPI-App-Module dynamisch, ruft `.openapi()` auf jeder App auf, schreibt das Ergebnis als `docs/api/openapi_<service>.json`, und baut anschließend den `SPECS`-JavaScript-Block in `swagger.html` neu zusammen (per Regex-Ersetzung, weil ein simples String-Replace an `\u`-Escape-Sequenzen in den JSON-Strings scheitern würde).

### `swagger.html` im Detail

```html
<select id="service-picker">
  <option value="controller">Traffic Controller - :8000 - ...</option>
  <option value="gen-http2">HTTP/2 Generator - :7001 - ...</option>
  <option value="gen-quic">QUIC/HTTP3 Generator - :7002 - ...</option>
  <option value="gen-mqtt">MQTT Generator - :7003 - ...</option>
  <option value="gen-tcpudp">TCP/UDP Generator - :7004 - ...</option>
</select>
<div id="swagger-ui"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.js"></script>
<script>
  const SPECS = {
    "controller":  { /* komplettes OpenAPI-3.1-JSON, ca. 7000 Zeichen */ },
    "gen-http2":   { /* ... */ },
    "gen-quic":    { /* ... */ },
    "gen-mqtt":    { /* ... */ },
    "gen-tcpudp":  { /* ... */ },
  };
  function render(key) {
    ui = SwaggerUIBundle({ spec: SPECS[key], dom_id: '#swagger-ui', ... });
  }
  document.getElementById('service-picker').addEventListener('change', (e) => render(e.target.value));
  render('controller');   // Default beim Laden
</script>
```
Eine einzige statische HTML-Datei lädt die offizielle **swagger-ui**-Bibliothek von einem CDN und rendert beim Laden direkt das Controller-Schema; über das Dropdown kann man zwischen den 5 Services wechseln, **ohne** dass die Seite neu geladen wird (alle 5 Specs sind bereits inline im `SPECS`-Objekt eingebettet, kein erneuter Server-Request nötig).

### Wann muss man `regen_swagger.py` erneut laufen lassen?

Immer dann, wenn sich an einem der 5 FastAPI-Apps etwas ändert, das Auswirkungen auf das generierte Schema hat: ein neues Feld in einem `GeneratorConfig`/`StatusResponse`-Modell, ein neuer Endpoint, eine geänderte `summary`/`description`, ein neuer `tags`-Eintrag. **Nicht** nötig bei reinen Implementierungsänderungen, die kein Pydantic-Modell und keinen Decorator betreffen (z.B. eine geänderte Sleep-Formel im `_send_loop()`).

### Check-Fragen Modul 6

1. Wenn du im Controller ein neues Feld zu `StatusResponse` hinzufügst – welche 2 Schritte sind danach nötig, damit Swagger aktuell ist?
2. Warum kann man im Swagger-UI-Dropdown zwischen 5 Services wechseln, ohne dass die Seite neu lädt oder ein Server-Request nötig ist?
3. Was würde passieren, wenn du in einem Generator ein Feld umbenennst (z.B. `payload_size` → `payload_bytes`), aber `regen_swagger.py` **nicht** erneut laufen lässt?

---

## Anhang A: Lösungshinweise

Kurze Stichpunkte, keine vollständigen Musterlösungen – zum Selbstüberprüfen nach dem eigenständigen Beantworten.

**Modul 0:** (1) gen-http2→target-http2, gen-quic→target-quic, gen-mqtt→mosquitto, gen-tcpudp→target-tcpudp. (2) Healthcheck sitzt AUF `metrics`; andere Services warten auf `metrics`, nicht umgekehrt; Trennung der Zuständigkeiten (Aggregation vs. Orchestrierung). (3) neuer Service-Block + Build, `TARGET_*`/`METRICS_URL`-Env, `GEN_<NAME>_URL` im Controller-Environment, gleiches Netzwerk, kein externer Port nötig.

**Modul 1:** (1) nur verschluckt, keine Auswertung. (2) bis zu 6 Einträge (4 Generatoren + target-http2 + target-quic); target-tcpudp fehlt garantiert (kein `_report()`). (3) `generator`, `running`, `packets_sent`, `bytes_sent`, `errors`, ggf. `rate_bps`/`rate`/`latency_ms`; `_ts` (Zeitstempel) und der eigentliche Payload-Inhalt werden NICHT mitgeschickt.

**Modul 2.1:** (1) zwei verschachtelte Exponentialverteilungen (Basis-Interval in `_stealth_params` + zusätzliches Poisson-Gap im `_send_loop`). (2) Vorteil von Reuse: kein Handshake-Overhead pro Paket; hier bewusst nicht gemacht, weil jeder TCP-Connect sichtbaren SYN/FIN-Verkehr für die Wireshark-Analyse erzeugen soll.

**Modul 2.2:** (1) `["/"]`/`["/data"]`; Dashboard-Textfeld, direkter API-PATCH, YAML-Phase. (2) zufällig via `random.choice`; Zielserver fängt jeden Pfad über Catch-all-Routes ab, kein 404. (3) bezieht sich auf einen ganzen Multi-Stream-Zyklus; mit `1/effective_rate` würde die Gesamtrate um den Faktor `streams` zu hoch werden.

**Modul 2.3:** (1) Handshake-Overhead würde bei jedem Request mitgemessen, bei den anderen (mit Connection-Reuse) nicht. (2) `zero_rtt_used` erfordert eine tatsächlich vom Server bestätigte Session-Resumption (`tls.session_resumed`), nicht nur die Absicht (`use_0rtt=True`). (3) es ist eine Beobachtung, kein Eingabewert; ein PATCH-Versuch würde stillschweigend ignoriert (gefiltert über `_READONLY_FIELDS`).

**Modul 2.4:** (1) damit das Broker-Fan-out sichtbar wird. (2) Liste `[w0,w1,w2]` oder Dict `{"0":w0,...}`, weil YAML Integer-Keys über JSON zu Strings macht. (3) QoS 0 = 1 Paket, QoS 2 = 4 Pakete; "50 Nachrichten/Sek bei QoS 2" bedeutet faktisch 200 Pakete/Sek.

**Modul 2 (übergreifend):** (1) `state`-Dict, `GeneratorConfig`/`StatusResponse`, `_lock`, 4 REST-Endpoints, `_metrics_loop` – bewusste Konsistenz für Wartbarkeit, nicht zufälliges Copy-Paste. (4) `random` ersetzt das feste `sleep(interval)` durch `sleep(expovariate(1/interval))`/`sleep(np.random.exponential(interval))` – Exponentialverteilung ist exakt die Verteilung der Inter-Arrival-Zeiten eines Poisson-Prozesses mit demselben Mittelwert.

**Modul 3:** (2) bei `warmup=0` startet Phase 1 sofort auf voller Rate; nur das Cooldown-Ramping am Ende läuft. (4) Beide könnten theoretisch gleichzeitig versuchen, dieselbe Rate per `PATCH /config` zu setzen; der zuletzt ausgeführte Call gewinnt (kein Locking zwischen beiden Mechanismen) – in der Praxis ist das unkritisch, weil Adaptive Control nur in der `balanced_adaptive`-Phase aktiv ist, die selbst kein Warmup/Cooldown durchläuft.

**Modul 4:** (1) der Server-Wert würde den Slider alle 2 Sekunden (Polling-Intervall) zurücksetzen. (3) kontinuierliche Drag-Werte (Slider) debouncen, diskrete Klicks/Auswahl (Toggle, Select) nicht.

**Modul 5:** (2) kein Fehler – `extra="allow"` plus Key-Filter im Generator ignoriert unbekannte Felder stillschweigend. (4) kein Widerspruch: "Rate" meint Nachrichtenrate; QoS 2 erzeugt 4 Pakete pro Nachricht, also trotz niedrigerer Message-Rate eine vergleichbare oder höhere Paket-Rate.

**Modul 6:** (1) Pydantic-Modell ändern + `regen_swagger.py` ausführen. (3) Swagger würde den alten Feldnamen weiter anzeigen, obwohl die Laufzeit-API den neuen Namen erwartet – Diskrepanz zwischen Dokumentation und Code, bis das Skript erneut läuft.

---

## Anhang B: Zwei End-to-End-Reisen

Diese zwei "Reisen" verbinden alle Module zu einem durchgehenden Ablauf. Am besten: erst selbst Schritt für Schritt durchgehen (mündlich oder schriftlich), dann mit den Modul-Erklärungen oben vergleichen.

### Reise A: Dashboard "Start" mit Profil `http2_heavy` → erstes Paket in Wireshark

1. Nutzer klickt "HTTP/2 Dominant"-Button → `loadProfile('http2_heavy')` → `POST /config/load` mit `{"profile": "http2_heavy"}`.
2. Nutzer klickt "Start system" → `startSystem()` liest `getActiveProfile()` (= `'http2_heavy'`) → `POST /start?profile=http2_heavy`.
3. Controller-Endpoint `start()`: lädt `config/http2_heavy.yaml` per `_load_yaml()`, setzt `state["running"]=True`, `state["active_profile"]="http2_heavy"`.
4. `_phase_to_gen_configs(phases[0])` baut die Configs für Phase `http2_dominant`. Da `warmup=15 > 0`: alle Rate-Felder werden auf 0 gesetzt (`start_configs`), die übrigen Felder (payload_size, method_distribution, etc.) bleiben wie konfiguriert.
5. Für jeden Generator mit nicht-leerer Config: `POST {url}/start` mit dieser Rate-0-Config. Jeder Generator setzt `state["running"]=True` und übernimmt die übergebenen Felder.
6. Controller startet `_run_phases()` als Hintergrund-Task.
7. `_run_phases()`: da `warmup=15>0`, ruft `_ramp_rates(_phase_to_gen_configs(phases[0]), 15, "up", "warmup")` auf → 15 Sekunden lang, in bis zu 20 Schritten, steigt z.B. `gen-http2`s `rate` linear von 0 auf 150 (das Ziel aus `http2_dominant`).
8. Pro Schritt: `PATCH {url}/config` mit der skalierten Rate. Der HTTP/2-Generator übernimmt `state["rate"] = neue_zahl`.
9. Im HTTP/2-Generator läuft parallel (seit Container-Start) der Daemon-Thread mit `_generate()`. Sobald `state["running"]=True` und `state["rate"]>0`, wird `_send_one()` für `streams` Requests gleichzeitig aufgerufen.
10. `_send_one()`: Münzwurf GET/POST (`method_get_pct`), Pfad zufällig aus `get_paths`/`post_paths` gewählt (Standard `["/"]`/`["/data"]`), `httpx.AsyncClient` sendet das erste echte HTTP/2-Paket über die TCP-Verbindung zu `target-http2:8080`.
11. **Dieses erste TCP/HTTP-2-Paket ist das, was du in Wireshark siehst** – TLS-Handshake (falls erste Verbindung) gefolgt vom eigentlichen HTTP/2-Frame.
12. Der HTTP/2-Target-Server empfängt es, zählt `requests++`, antwortet, und meldet (alle 10s) seine Zähler an den Metrics Collector.
13. Nach den 15 Sekunden Warmup beendet `_ramp_rates` mit `state["ramp_status"]=None`; `_run_phases` setzt `active_phase="http2_dominant"` und läuft die normale 120-Sekunden-Phase.
14. Das Dashboard pollt parallel alle 2 Sekunden `GET /status`, sieht `running=True`, `active_phase`, `ramp_status` während des Warmups, und rendert all das in `renderStatus()`.

### Reise B: Fehlerrate steigt während `balanced_adaptive` → Rate wird automatisch reduziert

1. Profil `balanced` läuft, Phase `balanced_adaptive` ist aktiv. `_run_phases()` hat beim Eintritt in diese Phase `_start_adaptive(adaptive_cfg)` aufgerufen (weil `phase.get("adaptive_control",{}).get("enabled")` true ist), was `_adaptive_loop()` als Hintergrund-Task startet.
2. `_adaptive_loop()` hat zu Beginn `baseline = await _get_current_rates()` gelesen – die aktuelle Rate jedes Generators als Fix-Basis.
3. Nutzer zieht im Fault-Injection-Panel den HTTP/2-Fehlerraten-Slider hoch (z.B. auf 20%). `onFaultChange('gen-http2', 20)` aktualisiert die Anzeige sofort, setzt nach 400ms `patchGen('gen-http2', {fault_rate: 0.2})`.
4. Der Controller-Endpoint `patch_generator()` leitet das per `PATCH http://gen-http2:7001/config` weiter; der HTTP/2-Generator übernimmt `state["fault_rate"]=0.2`.
5. Ab jetzt simuliert `_send_one()` bei 20% der Zyklen einen Fehler (`if fault_rate > 0 and random.random() < fault_rate`) – `state["errors"]` steigt, ohne dass tatsächlich gesendet wird.
6. Der HTTP/2-Generator meldet alle 5 Sekunden seinen aktuellen `packets_sent`/`errors`-Stand an den Metrics Collector.
7. Nach `check_interval=5` Sekunden wacht `_adaptive_loop()` wieder auf, holt `GET /metrics`, berechnet `d_errors/d_packets` seit dem letzten Check für `gen-http2` – das ergibt ungefähr 20%.
8. Da `error_rate (≈0.2) >= error_rate_min (0.05)`: `action="down"`. `multipliers["gen-http2"] = max(0.1, multipliers["gen-http2"] * 0.5)`.
9. `applied = {"rate": max(1, round(baseline_rate * neuer_multiplier))}` wird per `PATCH http://gen-http2:7001/config` an den Generator geschickt – die tatsächliche Sende-Rate sinkt.
10. `state["adaptive_status"]["gen-http2"]` wird mit der neuen Entscheidung aktualisiert; `_log()` schreibt einen `"adaptive"`-Log-Eintrag.
11. Beim nächsten Dashboard-Poll zeigt `renderAdaptive()` die neue Zeile: Fehlerrate ≈20%, Faktor (z.B. ×0.5), angewandte Rate, Aktion "↓ rate decreased". Der Log-Bereich zeigt den neuen Eintrag farblich hervorgehoben (Level `"adaptive"`).
12. Würde der Nutzer den Fault-Slider wieder auf 0 zurückziehen, würde die Fehlerrate beim nächsten Check unter `error_rate_max (0.0)` fallen → `action="up"` → die Rate steigt wieder, multiplikativ mit `×1.2` pro Check-Intervall, bis maximal `max_multiplier=5.0`.

---

## Anhang C: Abschluss-Quiz (für später, ohne Vorbereitung beantworten)

Diese Fragen verknüpfen bewusst mehrere Module. Am besten frei beantworten, ohne im Handbuch nachzuschlagen, dann gegenprüfen.

1. Welche 3 Mechanismen im Projekt erzeugen "Poisson-artiges" Timing, und wie unterscheiden sie sich (welcher State/welches Feld steuert jeden)?
2. Wenn `gen-quic` im Status `errors` erhöht, woher könnte das kommen – nenne mindestens 3 verschiedene Code-Stellen, die `state["errors"] += 1` ausführen können.
3. Warum hat `RATE_FIELDS["gen-tcpudp"]` zwei Einträge, aber alle anderen nur einen – und wie wirkt sich das konkret in `_ramp_rates()` und im Adaptive-Loop aus?
4. Ein Generator-Container crasht und startet neu (leerer State, Defaults). Was zeigt das Dashboard in den nächsten 2 Sekunden an, und was passiert beim nächsten Phasenwechsel im Controller?
5. Wo im Code wird zwischen "Nachrichtenrate" und "Paketrate" unterschieden – und wo würde eine naive Vergleichsrechnung (rate × Zeit) in die Irre führen?
6. Welche Endpunkte existieren am Controller, aber NICHT an den Generatoren (oder umgekehrt) – und warum genau diese Asymmetrie?
7. Was passiert, wenn man während des Warmups (`active_phase="warmup"`) den Rate-Slider eines Generators im Dashboard manuell verstellt – wer "gewinnt", der manuelle PATCH oder der nächste `_ramp_rates()`-Schritt?
8. Beschreibe, wie `qos_distribution` im YAML (`{0: 40, 1: 40, 2: 20}`), im Controller (`_phase_to_gen_configs`), im Generator-Pydantic-Modell und im tatsächlichen Publish-Aufruf jeweils repräsentiert wird – an wie vielen Stellen ändert sich das Datenformat?
9. Welche Komponente entscheidet, ob ein Burst "gerade aktiv" ist, bei HTTP/2 vs. bei TCP/UDP – sind das dieselben Mechanismen?
10. Ein neues Pydantic-Feld wird zu `GeneratorConfig` im MQTT-Generator hinzugefügt. Liste alle Dateien/Schritte auf, die danach (im Idealfall) angepasst werden müssten, damit Dashboard, Controller-YAML-Support und Swagger konsistent bleiben.

