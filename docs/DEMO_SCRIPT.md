# Demo Script: 15-20 Minutes

**Live demonstration for the project submission**

---

## Before the Demo: Checklist

```bash
# The evening before:
docker-compose build   # make sure everything builds
docker-compose up -d   # run for 10 minutes, check everything
docker-compose down

# On demo day, 30 min before:
docker-compose pull    # current images
docker-compose build   # again
docker system prune -f # free up disk space

# Prepare tabs:
# Tab 1: Terminal (docker-compose commands)
# Tab 2: Browser -> http://localhost:3000 (Dashboard)
# Tab 3: Browser -> http://localhost:8000/status (Controller API)
# Tab 4: Wireshark (prepared, interface selected)
```

---

## Phase 1: System Start (3 minutes)

**What you say:**
> "The system consists of 10 Docker containers, which all start with a single command."

**What you do:**
```bash
docker-compose up
```

Wait until all containers are green. Then show the dashboard:
> "The dashboard shows me the live status of all generators: packets per second, error rate, active connections."

**What the professor wants to see**: single-command startup, all containers running.

**If something does not start:**
```bash
docker-compose logs gen-quic    # logs of the problematic container
docker-compose restart gen-quic # restart a single container
```

---

## Phase 2: Protocol Overview and Configuration (4 minutes)

**What you say:**
> "The system is currently running with the balanced profile, with all protocols active at the same time. I will now show you how easily this can be changed at runtime."

**Show the dashboard**, briefly explaining:
- HTTP/2: TCP-based, multiplexed streams
- QUIC: UDP-based, HTTP/3
- MQTT: publish/subscribe for IoT simulation
- TCP/UDP: raw packets with burst patterns

**Live config switch:**
```bash
# Switch to HTTP/2-heavy
curl -X POST http://localhost:8000/config/load \
  -d '{"profile": "http2_heavy"}'
```

> "Can you see in the dashboard how HTTP/2 now makes up 80% of the traffic? The configuration is applied live, no restart needed."

**Adjust a single generator:**
```bash
curl -X PATCH http://localhost:8000/generator/gen-mqtt \
  -d '{"rate": 200}'
```

> "I can also adjust individual generators at runtime; here I am doubling the MQTT rate to 200 messages per second."

---

## Phase 3: Wireshark Live (5 minutes)

**What you say:**
> "Now let's look at the network. I will show you three things: protocol distribution, bursts in the I/O graph, and the difference between normal mode and stealth mode."

**Start Wireshark**, interface docker0, no filter, start the capture.

**Step 1: Protocol Hierarchy:**
After 30 seconds: `Statistics -> Protocol Hierarchy`
> "Here you can see the exact distribution: HTTP/2 over TCP port 8080, QUIC as the only protocol over UDP port 4433, and MQTT on TCP port 1883."

**Step 2: I/O Graph with bursts:**
```bash
curl -X POST http://localhost:8000/config/load \
  -d '{"profile": "http2_heavy"}'  # profile with burst pattern
```
`Statistics -> I/O Graph`; show the peaks.
> "The periodic peaks every 30 seconds correspond exactly to the burst configuration in the YAML file: 5 seconds at 5x the rate, then 25 seconds at the normal rate."

**Step 3: Behavioral Fingerprinting (the highlight):**
```bash
# Normal mode
curl -X PATCH http://localhost:8000/generator/gen-tcpudp \
  -d '{"mode": "normal"}'
```
Wireshark filter: `ip.dst == <target-tcpudp-ip>`
`Statistics -> Packet Lengths` -> screenshot: everything at 512 bytes

> "In normal mode: a classic generator fingerprint, every packet exactly 512 bytes, every 100ms. An IDS detects this immediately."

```bash
# Stealth mode
curl -X PATCH http://localhost:8000/generator/gen-tcpudp \
  -d '{"mode": "stealth"}'
```
`Statistics -> Packet Lengths` again -> screenshot: wide distribution

> "In stealth mode: random packet sizes between 64 and 1400 bytes, Poisson-distributed timing. Statistically identical to real browser traffic. This is traffic obfuscation, the same technique VPN providers use to make their traffic look like normal HTTPS."

---

## Phase 3b: Adaptive Control + Fault Injection (3 minutes)

**What you say:**
> "The controller can also react autonomously to failures, without me touching anything."

**What you do:**
1. In the dashboard, turn on the **Adaptive Control** toggle (or load `balanced` and let it reach the `balanced_adaptive` phase, where it's enabled automatically).
2. In the **Fault Injection** panel, drag the HTTP/2 "Injected Error Rate" slider up to ~20%.
3. Wait ~5-10 seconds (the check interval).

> "Watch the Adaptive Control table: the error rate is picked up, and within one check interval the HTTP/2 rate is automatically scaled down — no manual intervention. If I bring the error rate back to 0%, it scales back up."

4. Optionally also demonstrate the **Random (Poisson)** sending pattern: switch any generator's "Sending pattern" dropdown to "Random (Poisson)" and point out in the Attacker's View panel that the interval column now reads "Poisson" instead of a fixed millisecond value.

**What the professor wants to see**: a closed-loop, autonomous resilience mechanism — not just a manual rate slider.

---

## Phase 4: Failure Scenario (3 minutes)

**What you say:**
> "Now I will demonstrate how the system reacts to failures, and how this becomes visible in Wireshark."

Wireshark is running, filter: `tcp.flags.reset == 1`

```bash
docker stop target-http2
```

> "Can you see the red RST packets? TCP detects the failure in under a second. gen-http2 tries to re-establish the connection, receives an RST, and reports the error to the metrics collector."

The dashboard shows: the HTTP/2 error rate rises, while the other protocols keep running.

```bash
docker start target-http2   # show recovery
```

> "Recovery: within 3 seconds, HTTP/2 is back to normal operation. The other protocols were unaffected the whole time."

---

## Phase 5: Multi-Machine (2 minutes, if lab machines are available)

**If two machines are available:**

```bash
# Machine B (started earlier, before the demo):
docker-compose -f docker-compose.targets.yml up -d

# Machine A:
docker-compose -f docker-compose.generators.yml up -d   # TARGET_B_IP set via .env
```

> "The target services run on lab machine B. The generators here send real network traffic over the lab network."

Show Wireshark on the physical interface; real IP addresses instead of 172.x Docker IPs.

> "The difference: in the single-machine setup, everything runs over docker0 (loopback). On two machines, we see real Ethernet traffic, higher latency, and realistic packet fragmentation."

---

## Phase 6: Prepare for Questions

**Typical questions from the professor and answers:**

**"Why does QUIC run over UDP and not TCP?"**
> "QUIC implements its own reliability and flow control at the application layer. The advantage is that there is no head-of-line blocking as with TCP multiplexing. A lost packet does not block all other streams on the same connection."

**"What is the difference between QoS 0 and QoS 2 in MQTT?"**
> "QoS 0: fire and forget, the packet is sent once with no acknowledgment. QoS 2: an exactly-once guarantee, 4 packets per message (PUBLISH, PUBREC, PUBREL, PUBCOMP). You can see this directly in Wireshark: 4x more packets for QoS 2 with the same payload."

**"How does the TCP/UDP generator control packet size and timing without a framework like Scapy?"**
> "Plain Python sockets are enough here: TCP packet size is just the length of the random payload passed to `sendall()`, and timing is controlled by `time.sleep()` between sends — drawn from a fixed interval in Normal Mode or from an exponential (Poisson) distribution in Stealth Mode. Each TCP send opens and closes its own connection, which is actually useful for the demo: it produces a real SYN/SYN-ACK/FIN sequence per packet, visible in Wireshark."

**"What would be different in a real system?"**
> "TLS everywhere, including MQTT and raw TCP. Authentication on the Mosquitto broker. Rate limiting in the controller so that no generator floods the network. Persistence in the metrics collector (e.g. Prometheus). For a lab project, these simplifications are acceptable."

**"How did you use AI?"**
> "Claude (Anthropic) was used for the initial architecture design and for code scaffolding. All AI-generated parts were manually reviewed, debugged, and tested. The scientific content, in particular the analysis of the Wireshark captures and the stealth mode theory, was developed independently." *(An honest answer, well documented in the report)*

---

## Emergency Backup

If the system does not start:

1. **Show the Wireshark captures** (pre-recorded .pcapng files from `captures/`)
2. **Explain the architecture using the diagrams** in `docs/ARCHITECTURE.md`
3. **Show the code**, explaining normal vs. stealth mode directly in the generator code

> A well-explained system that does not run is better than a running system you cannot explain.

---

## Schedule

| Min | What |
|---|---|
| 0-3 | Start the system, all containers green |
| 3-7 | Dashboard, live config switch demonstration |
| 7-12 | Wireshark: Protocol Hierarchy -> bursts -> stealth mode |
| 12-15 | Adaptive Control + Fault Injection (skip or shorten if time is tight) |
| 15-17 | Failure scenario with TCP RST |
| 17-19 | Multi-machine (if available) |
| 19-20 | Questions |

*If running long, the safest cuts are Phase 3b (Adaptive Control) and Phase 5 (Multi-Machine) — both are also covered by a dedicated Wireshark capture you can fall back to showing instead.*

---

*Practice the demo at least twice, all the way through. The first run shows you what goes wrong. The second makes you confident.*
