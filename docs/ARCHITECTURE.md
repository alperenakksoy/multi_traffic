# System Architecture

**MIC Final Project: Multi-Protocol Traffic Generation and Analysis**

---

## Overview

The system is built around the **Producer-Consumer pattern**: generators produce traffic, target services receive it, the metrics collector aggregates the numbers, and the controller coordinates everything through a REST API. The dashboard only reads from and writes to the controller; it never accesses the generators directly.

### Design decision: why no event bus?

During the design phase, the question arose whether the controller should distribute commands through a message bus (such as MQTT itself) or directly over HTTP. We use **direct HTTP calls** because:
- They are easier to debug (logs are immediately readable)
- They involve fewer moving parts (no additional broker needed for control)
- REST is intuitive to explain during the demo

---

## Network design

All containers run in the same Docker network `mic-net` (bridge). This means:
- Containers address each other **by name**: `gen-http2` can call `http://target-http2:8080`
- No port mapping is needed for internal communication
- Only the ports that need to be exposed to the outside (host) are mapped in `docker-compose.yml`

```
Host machine (your laptop / lab PC)
    │
    │   :3000 (Dashboard)
    │   :8000 (Controller API)
    │   :9090 (Metrics)
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  Docker Network: mic-net                            │
│                                                     │
│  controller:8000   ←──►  gen-http2                  │
│  dashboard:3000    ←──►  gen-quic                   │
│  metrics:9090      ←──►  gen-mqtt    ←──►  mosquitto:1883  │
│  target-http2:8080 ←──►  gen-tcpudp                 │
│  target-quic:4433                                   │
└─────────────────────────────────────────────────────┘
```

---

## Container descriptions

### 1. Traffic Controller (`controller/`)

**Technology**: Python 3.11 + FastAPI + Uvicorn

**Role**: The brain of the system. It reads the YAML configuration and translates it into concrete HTTP commands for the generators. It logs every configuration change with a timestamp.

**Why FastAPI?** We need an async REST API; FastAPI is the standard choice for this in Python and natively supports HTTP/2 through Uvicorn.

**API endpoints:**
```
POST /start?profile=<name>        -> Load a YAML profile, run warmup -> phases -> cooldown
POST /stop                        -> Stop the phase runner, Adaptive Control, all generators
POST /config/load                 -> Set the active profile (applies phase 1 if running)
PATCH /generator/{name}            -> Forward arbitrary overrides to one generator's /config
POST /generator/{name}/start       -> Start a single generator only
POST /generator/{name}/stop        -> Stop a single generator only
GET  /status                       -> Full status: running/phase/ramp, all generators, metrics, log
GET  /profiles                     -> List available YAML profiles in config/
GET  /log                          -> Full configuration / adaptive-control log
GET  /adaptive/status              -> Current Adaptive Control state + last decision per generator
POST /adaptive/toggle?enabled=bool -> Manually enable/disable Adaptive Control
GET  /health                       -> Liveness check
```
Full schemas for every field: `docs/api/swagger.html` (combined Swagger UI for all 5 services).

**Configuration log example:**
```
[2026-06-05 14:32:11] LOAD_CONFIG profile=mqtt_heavy
[2026-06-05 14:32:15] START all_generators
[2026-06-05 14:33:01] PATCH gen-http2 rate=200
[2026-06-05 14:35:00] PHASE_CHANGE http2_dominant -> balanced
```

---

### 2. HTTP/2 Traffic Generator (`generators/http2/`)

**Technology**: Python + `httpx` (HTTP/2 capable)

**Configurable parameters** (field names exactly as accepted by `PATCH /config` / the YAML `protocols.http2` block):
```yaml
http2:
  rate: 100                  # Requests per second (across all concurrent streams)
  payload_size: 4096         # Bytes per POST body
  method_get_pct: 70         # 70% GET, 30% POST (or use method_distribution: {GET: 70, POST: 30}
                              # in the YAML - the controller normalizes it into method_get_pct)
  concurrent_streams: 5      # Multiplexed streams fired per cycle
  get_paths: ["/", "/api/items"]    # one chosen at random per GET (default: ["/"])
  post_paths: ["/data"]             # one chosen at random per POST (default: ["/data"])
  pattern: constant          # constant | periodic_burst | random | ramp
  burst_rate: 400            # req/s during a burst window (pattern=periodic_burst)
  burst_duration: 5          # seconds per burst window
  burst_interval: 30         # seconds between burst windows
  ramp_start_rate: 0         # req/s at the start of the ramp (pattern=ramp)
  ramp_end_rate: 100         # req/s the ramp climbs to, then holds (pattern=ramp)
  ramp_duration: 60          # seconds for the linear climb (pattern=ramp)
  fault_rate: 0.0            # 0-1: fraction of requests simulated as failures
  extra_latency_ms: 0        # artificial delay before every request
```
The target (`http://target-http2:8080`) is configured once via the `TARGET_URL` environment variable in `docker-compose.yml`, not per-phase in the YAML.

**Important for Wireshark**: HTTP/2 runs over TCP. In Wireshark, you will see TCP connections on port 8080, and the Follow TCP Stream view reveals the HTTP/2 frames. With multiplexing, one TCP connection carries multiple parallel streams; this is the main difference from HTTP/1.1.

---

### 3. QUIC/HTTP/3 Traffic Generator (`generators/quic/`)

**Technology**: Python + `aioquic`

**Configurable parameters:**
```yaml
quic:
  rate: 50               # Connection cycles per second
  payload_size: 1024     # Bytes per HTTP/3 POST, on every stream
  stream_count: 3        # Parallel multiplexed streams per cycle
  use_0rtt: false        # Cache + offer the TLS session ticket for 0-RTT resumption
  pattern: constant      # constant | periodic_burst | random | ramp
  burst_rate: 100        # cycles/s during a burst window (pattern=periodic_burst)
  burst_duration: 5      # seconds per burst window
  burst_interval: 30     # seconds between burst windows
  ramp_start_rate: 0     # cycles/s at the start of the ramp (pattern=ramp)
  ramp_end_rate: 40      # cycles/s the ramp climbs to, then holds (pattern=ramp)
  ramp_duration: 60      # seconds for the linear climb (pattern=ramp)
  fault_rate: 0.0
  extra_latency_ms: 0
```
`zero_rtt_used` (read-only, in `/status`) reports whether 0-RTT resumption actually succeeded on the current connection — distinct from `use_0rtt`, which only expresses the *intent*. Target host/port (`target-quic:4433`) are set once via `TARGET_HOST`/`TARGET_PORT` environment variables, not per-phase YAML fields. The generator keeps a single QUIC connection open across many requests (instead of reconnecting per request) so the TLS handshake cost doesn't dominate the measured latency.

**Why QUIC is interesting for Wireshark**: QUIC runs over **UDP**, not TCP. This is unusual for application-level traffic. In Wireshark, you will see UDP packets on port 4433. QUIC frames are encrypted (TLS 1.3), so the payload is not readable, but connection IDs, packet sizes, and timings are visible.

**Implementation note**: QUIC is the technically most demanding protocol. If time becomes tight, implement it last. A working system with 4 protocols and excellent analysis is better than an incomplete system with 5.

---

### 4. MQTT Traffic Generator (`generators/mqtt/`)

**Technology**: Python + `paho-mqtt`

**Configurable parameters:**
```yaml
mqtt:
  rate: 30               # Messages per second
  payload_size: 256      # Bytes
  topic_count: 5         # How many topics to rotate through (1-20) for publish + subscribe
                          # (or use topics: [a, b, c, ...] in the YAML - the controller
                          # translates the list length into topic_count)
  qos: 1                 # Fixed QoS, used unless qos_distribution is set
  qos_distribution:      # Optional: weighted random QoS per publish (overrides `qos`)
    0: 50                # 50% QoS 0 (fire and forget)
    1: 30                # 30% QoS 1 (at least once)
    2: 20                # 20% QoS 2 (exactly once)
  pattern: constant      # constant | periodic_burst | random | ramp
  burst_rate: 100        # messages/s during a burst window (pattern=periodic_burst)
  burst_duration: 5      # seconds per burst window
  burst_interval: 30     # seconds between burst windows
  ramp_start_rate: 0     # messages/s at the start of the ramp (pattern=ramp)
  ramp_end_rate: 50      # messages/s the ramp climbs to, then holds (pattern=ramp)
  ramp_duration: 60      # seconds for the linear climb (pattern=ramp)
  fault_rate: 0.0
  extra_latency_ms: 0
```
The broker (`mosquitto:1883`) is configured once via `BROKER_HOST`/`BROKER_PORT` environment variables. Topics are **not** named individually — the generator only supports rotating through a fixed-size set of built-in topics (`sensors/temperature`, `sensors/humidity`, `sensors/pressure`, `actuators/control`, `status/heartbeat`, then generic `load/topic-N`), sized by `topic_count`. Several YAML profiles list an explicit `topics: [...]` array for documentation/readability (e.g. naming the topics a real deployment might use); the controller's `_translate_mqtt()` (in `controller/main.py`) converts this into `topic_count: len(topics)` before forwarding the phase config to the generator — the actual topic *names* in that list aren't used (the generator's built-in rotation determines the real names), only the count.

This generator also **subscribes** to its own topic set, so the broker's fan-out (PUBLISH → broker → subscriber) is visible in captures too, not just the publish-side traffic.

**Important for Wireshark**: MQTT runs over TCP port 1883. In Wireshark, the filter `mqtt` shows all MQTT packets. With QoS 2, you will see a 4-way handshake (PUBLISH -> PUBREC -> PUBREL -> PUBCOMP), which produces more packets than QoS 0 with the same payload. This explains why a higher QoS increases the packet rate even at a lower message rate.

---

### 5. TCP/UDP Raw Traffic Generator (`generators/tcpudp/`)

**Technology**: Python, plain `socket` module (TCP: one new connection per packet via `connect()`/`sendall()`/`close()`; UDP: connectionless `sendto()`). *Note: the original design considered Scapy for raw packet crafting, but the implementation uses standard sockets — simpler, and the per-packet TCP connect/close cycle already produces the SYN/SYN-ACK/FIN sequence needed for the Wireshark analysis.*

This is the most important generator for the **Behavioral Fingerprinting analysis**. It has two **independent** configuration axes: `mode` (packet size + base interval) and `pattern` (overall sending cadence — see below).

**Normal Mode** (recognizable fingerprint) — fixed size, fixed interval derived from `tcp_rate`/`udp_rate`:
```python
def _normal_params():
    size = state["packet_size"]              # FIXED, e.g. 512B
    interval = 1.0 / rate                    # REGULAR, e.g. every 100ms at rate=10
    return size, interval, proto
```

**Stealth Mode** (no recognizable fingerprint) — random size, Poisson-distributed interval:
```python
def _stealth_params():
    size = random.randint(min_size, max_size)        # RANDOM, e.g. 64-1400B
    interval = np.random.exponential(mean_interval)  # POISSON-DISTRIBUTED
    return size, interval, proto
```

Independently of `mode`, the `pattern` field shapes the overall sending cadence on top of that base interval: `constant` (use the interval as-is), `periodic_burst` (send `burst_size` packets back-to-back, then idle `burst_interval` seconds), `random` (an *additional* Poisson gap on top of the mode's own interval), or `ramp` (combined TCP+UDP packet rate climbs linearly from `ramp_start_rate` to `ramp_end_rate` over `ramp_duration` seconds, then holds — overrides the per-protocol interval `_normal_params`/`_stealth_params` would otherwise compute from `tcp_rate`/`udp_rate`, but the TCP/UDP protocol split itself still comes from `tcp_ratio`, and packet size still comes from `mode`).

Configuration:
```yaml
tcpudp:
  mode: normal           # normal | stealth
  pattern: constant      # constant | periodic_burst | random | ramp
  tcp_rate: 20            # TCP packets/sec (normal mode)
  udp_rate: 10            # UDP packets/sec (normal mode)
  packet_size: 512        # Bytes (normal mode, fixed)
  mean_interval: 0.100    # Poisson mean, seconds (stealth mode)
  min_size: 64             # Bytes (stealth mode)
  max_size: 1400           # Bytes (stealth mode)
  tcp_ratio: 60            # 60% TCP, 40% UDP
  burst_size: 10           # packets per burst (pattern=periodic_burst)
  burst_interval: 1.0      # seconds idle between bursts (pattern=periodic_burst)
  ramp_start_rate: 0       # combined TCP+UDP packets/sec at ramp start (pattern=ramp)
  ramp_end_rate: 50        # combined TCP+UDP packets/sec the ramp climbs to (pattern=ramp)
  ramp_duration: 60        # seconds for the linear climb (pattern=ramp)
  fault_rate: 0.0
  extra_latency_ms: 0
```

Detailed explanation of stealth mode: see [`STEALTH_MODE.md`](STEALTH_MODE.md).

---

### 6. Target Services

**HTTP/2 Server** (`targets/http2_server/`):
- Hypercorn (ASGI server with HTTP/2 support) + FastAPI
- Responds to GET with JSON, to POST with an echo
- Logs every request for metrics

**QUIC Server** (`targets/quic_server/`):
- aioquic-based server
- Port 4433/UDP
- TLS certificate (self-signed, generated inside the container)

**MQTT Broker** (Mosquitto, official Docker image):
- Standard Eclipse Mosquitto
- Configured with `config/mosquitto.conf`
- No authentication (for lab purposes)

---

### 7. Metrics Collector (`metrics/`)

**Technology**: Python + Flask

Every generator sends its status to the collector every 5 seconds:
```json
{
  "generator": "gen-http2",
  "timestamp": "2026-06-05T14:33:45Z",
  "packets_sent": 15420,
  "bytes_transferred": 63078400,
  "errors": 3,
  "active_connections": 5,
  "current_rate": 98.7
}
```

The collector aggregates this and exposes it at `/metrics`:
```json
{
  "total_packets": 89234,
  "total_bytes": 412847102,
  "by_protocol": {
    "http2": {"packets": 31200, "errors": 3},
    "quic":  {"packets": 18900, "errors": 0},
    "mqtt":  {"packets": 24100, "errors": 1},
    "tcp":   {"packets": 9800,  "errors": 0},
    "udp":   {"packets": 5234,  "errors": 2}
  },
  "uptime_seconds": 847
}
```

---

### 8. Network Analyzer (`analyzer/`)

**Technology**: Python (stdlib only) driving a `tshark` subprocess.

**Why this exists**: every component above only reports what it *thinks* it sent (`packets_sent`, `rate_bps`, ...) — self-reported application-level counters, not ground truth. The 5 required Wireshark analyses instead need what actually crossed the wire, and until now that only existed as a manual, offline `tcpdump`/Wireshark session (see [`WIRESHARK_GUIDE.md`](WIRESHARK_GUIDE.md)), disconnected from the dashboard. The Network Analyzer closes that gap: it captures real packets continuously and feeds live protocol-distribution and I/O-graph data back into the same dashboard you use to generate the traffic, so a config change becomes a visible effect *on the wire* within a couple of seconds.

**Deployment**: one sidecar per target container — `analyzer-http2`, `analyzer-quic`, `analyzer-mqtt`, `analyzer-tcpudp` — each attached via `network_mode: "service:<target>"` instead of sniffing the `mic-net` bridge directly. This is the same workaround `WIRESHARK_GUIDE.md` already documents for Docker Desktop on macOS/Windows (no host-visible `docker0`/`br-*` interface), just automated and continuous instead of a one-shot manual `tcpdump`. Each container needs `cap_add: [NET_RAW, NET_ADMIN]` for `tshark` to capture.

**Capture**: a single `tshark` invocation per sidecar, filtered to exclude its own reporting traffic:
```
tshark -i eth0 -f "not port 9090" -T fields \
  -e frame.time_epoch -e frame.len \
  -e tcp.srcport -e tcp.dstport -e udp.srcport -e udp.dstport
```
The `-f "not port 9090"` capture filter matters: since the sidecar *shares* its target's network namespace, its own `POST /analysis/update` calls to the Metrics Collector would otherwise be captured and misclassified as traffic too.

**Classification**: purely by port number, matching the same ports `WIRESHARK_GUIDE.md` already uses as Wireshark filters (`tcp.port==8080`→http2, `tcp.port==1883`→mqtt, `udp.port==4433`→quic, port 9999→tcpudp). Each sidecar keeps a running total per protocol plus a 1-second-bucketed history (~2 minutes) and POSTs both to the Metrics Collector every second via `POST /analysis/update` — the same `/update` pattern the generators already use, just for captured packets instead of self-reports.

**Aggregation**: each sidecar only sees its own target's namespace, so it only ever reports its own protocol (cross-talk is structurally impossible). `GET /analysis` on the Metrics Collector therefore reconstructs the full picture by simply summing every sidecar's totals and merging their bucketed histories by timestamp — mirroring the manual "capture 4 in parallel, then `mergecap`" workflow from `WIRESHARK_GUIDE.md`, but live. The Controller proxies this into `GET /status` (`status.analysis`) exactly like it already does for `metrics`, so the dashboard needs no second polling target.

**Dashboard**: the "Live Network Analysis" section (between Live Performance and Traffic Profile) shows a live byte-share bar per protocol and a multi-line captured-throughput graph, replacing guesswork with measured numbers.

**Behavioral fingerprinting histograms (Analysis Task 3)**: each sidecar also buckets every captured packet by size (`<64`, `64-128`, ... `1500+` bytes) and by inter-arrival gap since the *previous* packet of the same protocol (`<1ms`, `1-5ms`, ... `1000ms+`), reported as `size_hist`/`gap_hist` alongside `totals`/`io_graph`. Both histograms are exponentially decayed by `HIST_DECAY = 0.88` once per second (~5s half-life) instead of kept as an all-time cumulative count — this is what makes them *live*: switching `gen-tcpudp` from `normal` to `stealth` mode visibly reshapes the histogram over the next ~15-25 seconds as old samples fade out, rather than the new shape being permanently diluted by hours of prior history. The dashboard's "Attacker's View" section renders both histograms per protocol as small bar charts and derives its verdict text (e.g. "clear, repeatable fingerprint" vs "spread out, hard to distinguish from background noise") from the dominant bucket's share of the total — measured, not hand-written.

**Real finding from building this**: with `tcp_ratio > 0` (the default), TCP/UDP's `stealth` mode does *not* fully defeat fingerprinting the way the qualitative description in [`WIRESHARK_GUIDE.md`](WIRESHARK_GUIDE.md) suggests. Every TCP send is a fresh `connect()`/`sendall()`/`close()` cycle (see generator section 5 above), so each logical send produces a stereotyped burst of small SYN/ACK/FIN control packets within sub-millisecond gaps of each other - and that burst pattern is identical whether `mode` is `normal` or `stealth`, since `mode` only randomizes the *data* packet's size/timing, not the connection overhead around it. Measured live: at `tcp_ratio=60` (the default), both modes showed a histogram dominated >75% by the `64-128`/`<1ms` buckets (the control-packet burst), masking the data-size randomization almost entirely. Only with `tcp_ratio=0` (pure UDP, no connection overhead) does the expected contrast appear cleanly: normal mode converges to 97% in one size bucket, stealth mode spreads across five buckets with no bucket above 40%. Worth citing directly in the Task 3 report section as a "distinguishable vs. overlapping characteristics" finding - TCP connection churn is itself a fingerprint, independent of payload obfuscation.

**Failure visibility (Analysis Task 4)**: each sidecar additionally extracts `tcp.flags.reset` and `mqtt.msgtype` per packet and watches a per-protocol "last packet seen" timestamp, reporting a `failure_events` list (capped at the 50 most recent) alongside the other fields:
- **Explicit signals** (fire the instant the packet crosses the wire): a TCP RST (`signal: tcp_rst`) on any protocol, an MQTT DISCONNECT (`mqtt.msgtype == 14`, `signal: mqtt_disconnect`).
- **Generic fallback** (`_check_silence()`, edge-triggered on active→silent / silent→active transitions): if a protocol that was sending packets has none for `SILENCE_THRESHOLD_S = 8.0` seconds, flag `signal: silence`; when it resumes, flag `signal: recovered`. This is what makes a QUIC failure visible at all - a `CONNECTION_CLOSE` frame on an established (1-RTT, short-header) connection is encrypted and not visible to a passive observer without TLS key material, unlike a TCP RST or an MQTT DISCONNECT, so "the responses just stop" is the honest, *actually observable* signal for QUIC.

The Metrics Collector concatenates all sidecars' `failure_events`, sorts by timestamp, and caps to the most recent 100. The dashboard's "Failure Signals (Live)" panel (between Fault Injection and Attacker's View) renders these as a deduplicated, color-coded feed, reusing the System Log's box/line styling.

**Calibrating `SILENCE_THRESHOLD_S`**: started at 3.0s, which turned out to fire constantly during *normal* operation - at a Poisson (`pattern: random`) mean rate of 1 pkt/s, `P(gap > 3s) ≈ e⁻³ ≈ 5%` per inter-arrival gap, so dozens of false "silence" events appeared per minute on legitimately low-rate phases. Raised to 8.0s (`P(gap > 8s) ≈ e⁻⁸ ≈ 0.03%`), verified stable under normal traffic. Silence events very early in a `warmup` ramp (rates still near zero) are expected and benign, not failures - same caveat applies to a real Wireshark capture spanning a warmup period.

**Real finding from building this - target-side capture goes blind on `docker stop`**: verified live by running `docker stop target-http2` while watching the dashboard. `analyzer-http2` shares `target-http2`'s network namespace, which is torn down the instant the container stops - its `tshark` immediately errors with `"There is no device named eth0"` and stays blind even after the target is *restarted* (the namespace is recreated fresh; the analyzer must be recreated too, e.g. `docker compose up -d --force-recreate analyzer-http2`, to reattach). This is the exact constraint [`WIRESHARK_GUIDE.md`](WIRESHARK_GUIDE.md) already documents for the manual method ("attach to the **generator** side, not the target you stop"), now confirmed to apply equally to the live analyzer. Practical consequence: with only the 4 target-side sidecars that exist today, the live dashboard can only observe a failure when a **generator** is stopped, not when a **target** is stopped (for the latter, fall back to the manual `WIRESHARK_GUIDE.md` procedure, or add generator-side sidecars as a future extension).

**Second real finding - stopping a generator produces a graceful close, not a RST**: verified live by running `docker stop gen-http2` against a stable baseline. The explicit `tcp_rst`/`mqtt_disconnect` signals did **not** fire; only the `silence` fallback did, exactly 8.0s after the last packet (matching `SILENCE_THRESHOLD_S` precisely - `docker stop`'s SIGTERM lets the OS close the generator's sockets cleanly before the connection goes quiet). A TCP RST specifically requires the *other* side to refuse/abort an established connection or a new SYN, which is the "target stopped" scenario above - and per the first finding, that's the one vantage point this architecture can't currently see. So today's live setup reliably proves "failure becomes visible," with a known ~8s latency floor from the silence threshold; demonstrating the sub-second RST timing the assignment's own example describes still needs either a generator-side sidecar or the manual `WIRESHARK_GUIDE.md` capture.

**Known limitations**:
- Encrypted payloads (QUIC/TLS) are no more visible here than in Wireshark itself — same constraint, not a new one.
- This does **not** replace the required `.pcapng` deliverables or their official Wireshark screenshots; it is a live, continuous complement that makes the *effect* of a config change visible immediately, while the formal captures for the report are still taken separately.
- Analyzer totals are cumulative since each sidecar's own start and are not wired into the existing `POST /reset` (which only resets generator counters) — restart the analyzer containers to zero them.
- See the two "real finding" call-outs above for the current failure-visibility blind spots (target-side capture dies with its target; RST specifically needs the generator-side vantage point this setup doesn't have yet).

**Fixed bug found while building this**: `gen-quic` used to report successful sends (`packets_sent` climbing, `errors=0`) while genuinely zero UDP packets reached `target-quic` - aioquic's default 60s `idle_timeout` silently terminated the connection during any `rate=0` period (nothing was sending keep-alives), and nothing checked `conn._closed` before calling `transmit()`/`send_data()` again. Fixed in `generators/quic/generator.py`'s `_run_connection()`: it now returns (triggering a fresh reconnect) as soon as `conn._closed.is_set()`. Verified live: QUIC's protocol-distribution share went from ~0% to a sustained ~15-30%, matching the other protocols.

---

## Autonomous features (beyond the baseline requirements)

### Warmup / Cooldown (global) vs. the per-protocol `ramp` pattern

The `global.warmup`/`global.cooldown` YAML fields are implemented in the controller's `_ramp_rates()` function: rate fields are linearly stepped from 0 up to the configured target (warmup, at profile start) or from the target down to 0 (cooldown, at profile end), in up to 20 steps via periodic `PATCH /config` calls, across *every* generator simultaneously. The dashboard shows live progress (`ramp_status.progress`, 0.0–1.0) while either is active.

This is **not** the same mechanism as the assignment's "ramping (linear increase over time)" sending pattern, even though both produce a rising/falling slope in a Wireshark I/O graph. The assignment lists ramping alongside constant/burst/random as one of several sending-pattern *options*, selectable per protocol and per phase — so each generator additionally supports `pattern: ramp` (`ramp_start_rate`, `ramp_end_rate`, `ramp_duration`), computed entirely inside the generator from a wall-clock anchor (`_ramp_started_at`/`_ramp_progress()` in each `generators/*/generator.py`), independent of the controller and of warmup/cooldown. A phase can therefore set, say, `mqtt: {pattern: ramp, ramp_start_rate: 5, ramp_end_rate: 180, ramp_duration: 40}` while `http2` in the same phase stays `constant` — something the global warmup/cooldown ramp alone could never express, since it always ramps *every* generator's generic rate field together, only at the very start/end of the whole profile run.

The two mechanisms can run concurrently without conflicting, with one caveat: while a generator's `pattern` is `ramp`, it computes its effective rate from `ramp_start_rate`/`ramp_end_rate` and ignores the generic `rate` field, so a PATCH from `_ramp_rates()` (or from Adaptive Control, see below) that targets `rate`/`tcp_rate`/`udp_rate` has no visible effect on that generator until its phase's pattern changes away from `ramp`. In practice this only matters if a profile sets `pattern: ramp` on a generator during the very first or very last phase (i.e. overlapping with the global warmup/cooldown window) — none of the bundled profiles do this; `balanced.yaml`'s `balanced_ramp`, `http2_heavy.yaml`'s `http2_ramp`, and `mqtt_heavy.yaml`'s `mqtt_ramp` phases all sit safely between warmup and cooldown.

`config/balanced.yaml`'s `balanced_ramp` phase demonstrates `pattern: ramp` on all 4 generators at once; `http2_heavy.yaml`'s `http2_ramp` and `mqtt_heavy.yaml`'s `mqtt_ramp` demonstrate it on individual protocols alongside others left at a steady rate.

### Periodic burst pattern on all 4 protocols

All 4 generators support `pattern: periodic_burst` (`burst_rate`, `burst_duration`, `burst_interval`) — not just HTTP/2 and TCP/UDP, which had it from the start. HTTP/2, QUIC, and MQTT use the same wall-clock-modulo mechanism (`_is_burst_active()`: the current moment falls inside a burst window if `time.time() % burst_interval < burst_duration`), so the cadence stays stable across restarts. TCP/UDP's burst implementation predates this and instead sends a literal `burst_size` packets back-to-back before idling `burst_interval` seconds — a count-based rather than wall-clock-based burst, which is why its config fields are named differently (`burst_size` instead of `burst_rate`/`burst_duration`).

### Random (Poisson) pattern on all 4 protocols

All 4 generators (not just TCP/UDP) support `pattern: random`, implemented as `random.expovariate(1/interval)` (or `np.random.exponential(interval)` for TCP/UDP) instead of a fixed `sleep(interval)` — exponentially-distributed inter-arrival times with the same mean rate as `constant`, i.e. a Poisson process. `config/balanced.yaml`'s `balanced_with_stealth` phase demonstrates this on all protocols simultaneously, alongside TCP/UDP stealth mode.

### Fault Injection

Every generator accepts `fault_rate` (0–1, fraction of sends simulated as failures *without* actually sending) and `extra_latency_ms` (artificial delay before every send). Controllable per-protocol from the dashboard's "Fault Injection" panel — used to demonstrate Analysis Task 4 (Failure Visibility) and to drive Adaptive Control reactions live.

### Adaptive Control

An autonomous closed-loop controller (`_adaptive_loop()` in `controller/main.py`): every `check_interval` seconds, it computes each generator's error rate (and latency, where reported) since the last check, and multiplicatively scales that generator's rate up (`scale_up_factor`) or down (`scale_down_factor`) against configurable thresholds, bounded by `min_multiplier`/`max_multiplier`. Enabled either via a phase's `adaptive_control` block in the YAML (e.g. `balanced.yaml`'s `balanced_adaptive` phase) or manually via `POST /adaptive/toggle`. This goes beyond the assignment's baseline requirements but directly demonstrates "resilience patterns" from the course objectives.

---

## Data flow

```
                    ┌──────────┐
Browser ──────────► │Dashboard │
                    └────┬─────┘
                         │ REST (HTTP)
                    ┌────▼─────┐
                    │Controller│ ◄── YAML Config
                    └────┬─────┘
                         │ HTTP commands to generators
         ┌───────────────┼────────────────────┐
         ▼               ▼                    ▼
    ┌─────────┐     ┌─────────┐          ┌─────────┐
    │gen-http2│     │gen-quic │   ...    │gen-tcpudp│
    └────┬────┘     └────┬────┘          └────┬────┘
         │               │                    │
         │ HTTP/2        │ QUIC/UDP           │ TCP/UDP
         ▼               ▼                    ▼
    ┌─────────┐     ┌─────────┐          [no target,
    │target-  │     │target-  │           raw packets]
    │http2    │     │quic     │
    └────┬────┘     └────┬────┘                │
         │ shared netns  │ shared netns        │ shared netns
         ▼               ▼                     ▼
    ┌─────────┐     ┌─────────┐          ┌─────────────┐
    │analyzer-│ ... │analyzer-│          │analyzer-tcpudp│  (tshark, real packets)
    │http2    │     │quic     │          └─────────────┘
    └────┬────┘     └────┬────┘                │
         │               │                     │
         └───────┬───────┴─────────────────────┘
                 │ Stats (self-reported)  +  /analysis/update (captured)
                 ▼
           ┌──────────┐
           │ metrics  │ ◄── generators send self-reported stats
           └──────────┘ ◄── analyzers send captured totals + io_graph
                 ▲
          Controller ── GET /metrics + GET /analysis, both proxied into /status
                 ▲
    Dashboard ───┘ (GET /status every 2 sec)
```

---

## Multi-Machine Deployment

For Analysis Task 5, the system is distributed across 2 lab machines:

**Machine A (generators)**:
```yaml
# docker-compose.generators.yml
services:
  controller: ...
  gen-http2: ...
  gen-quic: ...
  gen-mqtt: ...
  gen-tcpudp: ...
```

**Machine B (targets)**:
```yaml
# docker-compose.targets.yml
services:
  target-http2: ...
  target-quic: ...
  mosquitto: ...
  metrics: ...
```

The generators on machine A point to the IP address of machine B via the `TARGET_B_IP` environment variable (set in a `.env` file next to `docker-compose.generators.yml`, copy from `.env.example`). Wireshark runs on the **physical** network interface of either machine (not `docker0`/`br-*`), so the traffic is genuinely inter-machine rather than looped back over the Docker bridge. Machine B's firewall must allow inbound connections on 8080/tcp, 4433/udp, 9999/tcp+udp, 1883/tcp, 9090/tcp.

---

## Implementation order (recommended)

```
Week 1: Foundation
  ├── docker-compose.yml skeleton
  ├── Mosquitto + gen-mqtt -> working?
  └── HTTP/2 server + gen-http2

Week 2: Core
  ├── Controller + YAML config
  ├── Metrics collector
  ├── TCP/UDP generator (normal mode)
  └── QUIC (last, most difficult part)

Week 3: Polish
  ├── Stealth mode in the TCP/UDP generator
  ├── Dashboard
  ├── Adaptive phase logic in the controller
  └── Wireshark captures for all 5 analyses
```

---

*Detailed step-by-step instructions for each container are in the respective subfolder README.*
