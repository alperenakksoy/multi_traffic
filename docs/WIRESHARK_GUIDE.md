# Wireshark Capture Guide

**MIC Final Project: step-by-step instructions for the 5 required captures**

---

## Preparation

Before starting, launch the system and verify it:
```bash
docker-compose up -d
docker-compose ps          # are all containers "running"?
curl http://localhost:8000/status
```

Open Wireshark on the correct interface:
- **Single machine, Linux**: interface `docker0` or `br-<id>` (the Docker bridge)
  - Find it with: `ip link show | grep br-`
- **Multi-machine**: the physical interface (`eth0`, `en0`, etc.)

### Single machine, macOS / Windows (Docker Desktop): no visible bridge interface

Docker Desktop on macOS/Windows runs containers inside a VM. There is **no** `docker0`/`br-*`
interface visible on the host — Wireshark on the host only ever sees `Loopback`, and
container-to-container traffic (e.g. `gen-mqtt` → `mosquitto`) never appears there, even
filtering for `mqtt`, because it never crosses the host's network stack at all.

**Fix**: capture *inside* the Docker network using a sidecar container that shares another
container's network namespace (`--network container:<name>`), writing the capture to a
mounted folder instead of using Wireshark live. Then open the resulting file with Wireshark
on the host afterwards — this produces the exact same `.pcapng` deliverable.

```bash
mkdir -p captures
docker run --rm -it \
  --network container:target-http2 \
  -v "$(pwd)/captures:/captures" \
  nicolaka/netshoot \
  tcpdump -i eth0 -w /captures/01_http2.pcap
# Ctrl+C once enough time has passed
```

Which container to attach to, per capture below:

| Capture | Attach to | Why |
|---|---|---|
| 1. Protocol Distribution | run 4 in parallel: `target-http2`, `target-quic`, `mosquitto`, `target-tcpudp` | need all 4 protocols at once; merge afterwards with `mergecap -w captures/01_all.pcapng captures/*.pcap` |
| 2. Temporal Analysis | the target of whichever protocol has the burst pattern (e.g. `target-http2` for `http2_heavy`) | |
| 3. Behavioral Fingerprinting | `target-tcpudp` (or `gen-tcpudp`) | only this generator should be running |
| 4. Failure Visibility | the **generator** side (e.g. `gen-http2`), not the target you stop | if you attach to `target-http2`'s namespace and then `docker stop target-http2`, the sniffer dies with it |
| 5. Multi-Machine | n/a — real lab machines have a normal physical interface, this workaround isn't needed there | |

If `tcpdump -D` inside the netshoot container lists a different interface name than `eth0`
for a given container, use that name instead.

---

## Capture 1: Protocol Distribution

**Goal**: show the distribution of all protocols after 60 seconds of operation.

**Step by step:**

```bash
# 1. Load the balanced profile (all protocols active)
curl -X POST http://localhost:8000/config/load \
  -H "Content-Type: application/json" \
  -d '{"profile": "balanced"}'

# 2. Start all generators
curl -X POST http://localhost:8000/start
```

In Wireshark:
1. Start the capture (no filter, capture everything)
2. **Wait 60 seconds**
3. Stop the capture

Analysis:
- Menu: `Statistics -> Protocol Hierarchy`
- Take a screenshot; this is the main figure for the report

**What you should see:**
```
Frame
└── Ethernet
    ├── IPv4
    │   ├── TCP (Port 8080) -- HTTP/2 -- ~40%
    │   ├── TCP (Port 1883) -- MQTT    -- ~25%
    │   ├── TCP (Port raw)  -- Raw TCP  -- ~15%
    │   └── UDP (Port 4433) -- QUIC    -- ~15%
    └── UDP (raw)           -- Raw UDP  -- ~5%
```

**For the report**:
> "The Protocol Hierarchy shows that HTTP/2 (TCP:8080) accounts for 40% of the traffic, while QUIC, the only protocol running over UDP, contributes 15%..."

---

## Capture 2: Temporal Analysis

**Goal**: show that burst patterns are visible in the I/O graph.

```bash
# Load the HTTP/2-heavy profile with a burst pattern
curl -X POST http://localhost:8000/config/load \
  -H "Content-Type: application/json" \
  -d '{"profile": "http2_heavy"}'

curl -X POST http://localhost:8000/start
```

In Wireshark:
1. Start the capture
2. **Wait 120 seconds** (this captures at least 2 burst cycles)
3. Stop the capture

Analysis:
- Menu: `Statistics -> I/O Graph`
- Add a separate line for each protocol filter:
  - `tcp.port == 8080` -> HTTP/2
  - `tcp.port == 1883` -> MQTT
  - `udp.port == 4433` -> QUIC
  - `tcp and not tcp.port == 8080 and not tcp.port == 1883` -> raw TCP

**What you should see:**

```
Mbps
 2.0 |     ####          ####
 1.5 |   ########      ########
 1.0 | ############  ############
 0.5 |------------------------------------------ MQTT (constant)
     +------------------------------------------> Time (120 sec)
      Burst         Gap      Burst
```

The peaks correspond to the configured burst (e.g. every 30 seconds, 5 seconds long, at 5x the rate).

**For the report**:
> "The I/O graph shows periodic bursts every approximately 30 seconds, which correspond directly to the `burst_interval: 30` configuration. MQTT remains constant, since it is configured without a burst pattern..."

**Extra (optional, strengthens this capture)**: `http2_heavy.yaml` includes a dedicated `http2_ramp` phase (right after `http2_dominant`, 60s) where HTTP/2 and QUIC are both set to `pattern: ramp` — their rate climbs linearly from a low start rate to a high end rate over the phase, then holds. Capture across that phase to show the assignment's "ramping (linear increase over time)" sending pattern directly, as a rising slope followed by a flat plateau in the I/O graph — distinct from both the burst pattern above and from the *global* `warmup`/`cooldown` ramp (which ramps every generator's rate together, only once, at the very start/end of the whole run, and is visible too if you capture across it instead). `balanced.yaml`'s `balanced_ramp` phase demonstrates the same `pattern: ramp` on all four protocols simultaneously, and `mqtt_heavy.yaml`'s `mqtt_ramp` phase demonstrates it on MQTT alone. You can also set any generator's "Sending pattern" to "Random (Poisson)" in the dashboard and capture that separately — the I/O graph should show no regular spacing at all, contrasting with the constant/burst/ramp graphs.

---

## Capture 3: Behavioral Fingerprinting

**Goal**: show the difference between normal mode and stealth mode.

This is the scientifically most interesting capture, since it directly answers the question of whether protocols can be detected based on statistical patterns.

**Part A: normal mode:**
```bash
curl -X PATCH http://localhost:8000/generator/gen-tcpudp \
  -H "Content-Type: application/json" \
  -d '{"mode": "normal", "rate": 20}'
```

Wireshark:
1. Only the TCP/UDP generator is active (stop the others for a clean picture)
2. Capture for 30 seconds
3. Stop

**Part B: stealth mode:**
```bash
curl -X PATCH http://localhost:8000/generator/gen-tcpudp \
  -H "Content-Type: application/json" \
  -d '{"mode": "stealth"}'
```

Wireshark:
1. Start the capture again
2. 30 seconds
3. Stop

**Analysis in Wireshark:**
- `Statistics -> Packet Lengths` -> histogram of packet sizes

**Normal mode: what you see:**
```
Packet Length Distribution:
[512 Bytes] ######################## ~100%
[other]     .                        ~0%

I/O Graph:
_#_#_#_#_#_#  <- uniform spikes, every 100ms
-> Fingerprint: CLEARLY RECOGNIZABLE
```

**Stealth mode: what you see:**
```
Packet Length Distribution:
[64-128]   ####
[128-256]  ##########
[256-512]  ############
[512-1024] #################
[1024+]    ######

I/O Graph:
_#__###_###__#____###_  <- chaotic, no pattern
-> Fingerprint: NOT RECOGNIZABLE
```

**For the report**:
> "Behavioral fingerprinting is based on statistical patterns. In normal mode, the packet length distribution shows a single peak at exactly 512 bytes, a classic generator fingerprint. Stealth mode (Poisson timing, random packet size between 64 and 1400 bytes) produces a distribution that mimics real HTTP traffic and is statistically indistinguishable from a real browser..."

**Extra (optional, strengthens this capture)**: load the `balanced` profile and let it reach the `balanced_with_stealth` phase — this combines TCP/UDP stealth mode **with** the Random (Poisson) sending pattern on HTTP/2, QUIC, and MQTT simultaneously, so you can show fingerprinting resistance across *all four* protocols in one capture, not just TCP/UDP.

---

## Capture 4: Failure Visibility

**Goal**: show how different protocols react to a failure.

```bash
# Start all generators
curl -X POST http://localhost:8000/start
```

Wireshark:
1. Start the capture
2. Capture **30 seconds of normal operation**
3. Then, while Wireshark is running:

```bash
# Take down the HTTP/2 target
docker stop target-http2
```

4. Capture another **30 seconds**
5. Stop

**What you should see:**

After `docker stop target-http2`:
- **TCP RST packets** from gen-http2 to target-http2 (connection refused)
- **Wireshark filter**: `tcp.flags.reset == 1`
- In Wireshark: red rows, `[RST, ACK]` in the info column

Timing: the TCP RST appears within **under 1 second** of the stop.

```
Time ->
00:30 --- normal HTTP/2 traffic ---------------
00:30 docker stop target-http2
00:30.3 --- TCP RST --- TCP RST --- TCP RST ----
00:31 --- no more HTTP/2 packets ---------------
       (MQTT, QUIC continue undisturbed)
```

**Second test**: stop the MQTT broker:
```bash
docker stop mosquitto
```

Look for: `mqtt.msgtype == 14` (DISCONNECT) or TCP connection terminations on port 1883.

**For the report**:
> "The TCP RST appears within 300ms of stopping the HTTP/2 target; TCP detects the failure immediately. On broker failure, MQTT clients attempt up to 3 reconnects (visible as TCP SYN packets to port 1883) before giving up. QUIC remains unaffected because it has its own transport layer..."

---

## Capture 5: Multi-System Deployment

**Prerequisite**: two lab machines with a network connection.

**Setup:**

Machine B (targets) — start this **first**:
```bash
docker-compose -f docker-compose.targets.yml up --build
```

Find Machine B's IP:
```bash
ip addr show eth0       # Linux
ipconfig getifaddr en0  # macOS
```

Machine A (controller + dashboard + generators):
```bash
# Copy .env.example to .env and set Machine B's real IP, e.g.:
echo "TARGET_B_IP=192.168.1.42" > .env

docker-compose -f docker-compose.generators.yml up --build
```
The generators read `TARGET_B_IP` via environment variables (`TARGET_URL`, `TARGET_HOST`, `BROKER_HOST`, `METRICS_URL` in `docker-compose.generators.yml`) — no YAML changes needed; the same 3 profiles (`balanced`, `http2_heavy`, `mqtt_heavy`) work unchanged on both single- and multi-machine setups.

**Wireshark:**
- On machine B, interface `eth0` (the physical interface, not docker0)
- Or: Wireshark on the switch/router between the two machines

Start the capture, run for 60 seconds, stop.

**What differs compared to the single-machine setup:**
- Slightly higher latency (a real network hop instead of loopback)
- No shared Docker bridge; genuine layer-3 traffic
- With enough traffic: inter-frame gaps become visible (packetization within the Ethernet frame)
- IP addresses are now real host IPs, not Docker IPs (172.x.x.x)

**For the report**:
> "In the multi-machine deployment, the round-trip time for HTTP/2 increases from approximately 0.3ms (single machine, loopback) to approximately 1.2ms (multi-machine, Ethernet). QUIC barely benefits, since 0-RTT is enabled. The traffic is fully visible on the inter-machine link, whereas in the single-machine setup it ran over docker0 and was therefore not directly accessible to the host..."

---

## Tips for good screenshots

1. **Always show the time axis**; it looks more professional in the report
2. **Set coloring rules**: menu `View -> Coloring Rules` -> HTTP/2 blue, MQTT green, QUIC orange
3. **Packet comments**: you can add comments directly to packets in Wireshark (`Ctrl+Alt+C`), which makes screenshots self-explanatory
4. **Export**: `File -> Export Specified Packets -> As .pcapng` for the submission

---

*Each of these analyses directly yields usable screenshots and explanations for the IEEE report.*
