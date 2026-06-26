"""
Network Analyzer (sidecar)
---------------------------
Attached to one target container's network namespace via `network_mode:
"service:<target>"` in docker-compose.yml. Runs `tshark` on that namespace's
interface and observes the *actual* packets crossing the wire - independent
of what the generators self-report in metrics/collector.py.

Classifies packets by port into the same protocol buckets used throughout
this project (http2/quic/mqtt/tcpudp), keeps a running total per protocol
plus a 1-second-bucketed history (~2 minutes) for a live I/O graph, and
POSTs a snapshot to the Metrics Collector every second via POST /analysis/update.
"""

import os, json, subprocess, threading, time, urllib.request
from collections import deque

IFACE          = os.getenv("IFACE", "eth0")
ANALYZER_NAME  = os.getenv("ANALYZER_NAME", "unknown")
METRICS_URL    = os.getenv("METRICS_URL", "http://metrics:9090")
HISTORY_SECONDS = 120

# Port -> protocol label, identical across every analyzer instance/vantage point.
PORT_PROTOCOL = {
    8080: "http2",
    1883: "mqtt",
    4433: "quic",
    9999: "tcpudp",
}

# Packet-size and inter-arrival-gap buckets for the Behavioral Fingerprinting
# histograms (Analysis Task 3) - upper bounds in bytes / milliseconds, the
# label list has one extra entry for the "everything above the last edge" bucket.
SIZE_EDGES  = [64, 128, 256, 512, 1024, 1500]
SIZE_LABELS = ["<64", "64-128", "128-256", "256-512", "512-1024", "1024-1500", "1500+"]

GAP_EDGES_MS  = [1, 5, 10, 25, 50, 100, 250, 500, 1000]
GAP_LABELS    = ["<1ms", "1-5ms", "5-10ms", "10-25ms", "25-50ms", "50-100ms",
                  "100-250ms", "250-500ms", "500-1000ms", "1000ms+"]

# Per-second decay applied to size_hist/gap_hist so they reflect *recent*
# behavior (~5s half-life) instead of all-time cumulative counts - flipping
# e.g. TCP/UDP stealth mode on visibly reshapes the histogram within seconds,
# instead of the old shape being diluted by hours of prior history.
HIST_DECAY = 0.88

# How long a protocol that was previously active must go fully quiet before
# we declare "silence" (Analysis Task 4 fallback signal - see _check_silence).
# Needs to be well above the longest gap a low-rate/Poisson("random" pattern)
# phase produces by chance: at a 1 pkt/s mean rate, P(gap > 3s) ~ 5% per gap
# (too noisy, fires during normal operation); P(gap > 8s) ~ 0.03%.
SILENCE_THRESHOLD_S = 8.0
FAILURE_EVENTS_MAXLEN = 50

_lock = threading.Lock()
_totals: dict[str, dict[str, int]] = {}             # protocol -> {packets, bytes}
_buckets: dict[int, dict[str, dict[str, int]]] = {}  # epoch second -> protocol -> {packets, bytes}
_bucket_order: deque = deque()                       # epoch seconds, oldest first

_size_hist: dict[str, dict[str, float]] = {}  # protocol -> {size label -> decayed count}
_gap_hist:  dict[str, dict[str, float]] = {}  # protocol -> {gap label -> decayed count}
_last_seen: dict[str, float] = {}             # protocol -> timestamp of its last packet

_last_packet_ts: dict[str, float] = {}  # protocol -> capture timestamp of its most recent packet
_is_silent: dict[str, bool] = {}        # protocol -> whether it's currently flagged as silent
_failure_events: deque = deque(maxlen=FAILURE_EVENTS_MAXLEN)  # {ts, protocol, signal, detail}


def _bucket_label(value: float, edges: list[float], labels: list[str]) -> str:
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


def _classify(tcp_src: str, tcp_dst: str, udp_src: str, udp_dst: str) -> str:
    for p in (tcp_src, tcp_dst, udp_src, udp_dst):
        if p and int(p) in PORT_PROTOCOL:
            return PORT_PROTOCOL[int(p)]
    return "other"


def _record(ts: float, length: int, proto: str, is_rst: bool, mqtt_msgtype: str):
    sec = int(ts)
    with _lock:
        total = _totals.setdefault(proto, {"packets": 0, "bytes": 0})
        total["packets"] += 1
        total["bytes"]   += length

        if sec not in _buckets:
            _buckets[sec] = {}
            _bucket_order.append(sec)
            while len(_bucket_order) > HISTORY_SECONDS:
                _buckets.pop(_bucket_order.popleft(), None)

        bucket = _buckets[sec].setdefault(proto, {"packets": 0, "bytes": 0})
        bucket["packets"] += 1
        bucket["bytes"]   += length

        size_label = _bucket_label(length, SIZE_EDGES, SIZE_LABELS)
        sizes = _size_hist.setdefault(proto, {})
        sizes[size_label] = sizes.get(size_label, 0.0) + 1

        last = _last_seen.get(proto)
        _last_seen[proto] = ts
        if last is not None and ts > last:
            gap_label = _bucket_label((ts - last) * 1000, GAP_EDGES_MS, GAP_LABELS)
            gaps = _gap_hist.setdefault(proto, {})
            gaps[gap_label] = gaps.get(gap_label, 0.0) + 1

        _last_packet_ts[proto] = ts

        # Explicit, protocol-specific failure signals (Analysis Task 4). Both
        # are detected per-packet, the instant the signal crosses the wire.
        if is_rst:
            _failure_events.append({"ts": ts, "protocol": proto, "signal": "tcp_rst",
                                     "detail": "TCP RST"})
        if mqtt_msgtype == "14":
            _failure_events.append({"ts": ts, "protocol": proto, "signal": "mqtt_disconnect",
                                     "detail": "MQTT DISCONNECT"})


def _decay_histograms():
    with _lock:
        for hist in (_size_hist, _gap_hist):
            for proto in hist:
                for label in hist[proto]:
                    hist[proto][label] *= HIST_DECAY


def _check_silence():
    """
    Generic fallback failure signal: if a protocol that was sending packets
    suddenly has none for SILENCE_THRESHOLD_S, flag it - this is what makes
    QUIC failures visible too. A QUIC CONNECTION_CLOSE frame on an established
    (1-RTT, short-header) connection is encrypted and not visible to a passive
    observer without TLS key material, unlike a TCP RST or an MQTT DISCONNECT;
    "the responses just stop" is the honest, *actually* observable signal.
    Edge-triggered (fires once per active->silent / silent->active
    transition), not once per second while it stays silent.
    """
    now = time.time()
    with _lock:
        # Only the 4 real protocols have an actual generator behind them whose
        # disappearance is meaningful; "other" (ARP/control noise) is sparse
        # and bursty by nature, so silence/recovery there isn't a failure signal.
        for proto in PORT_PROTOCOL.values():
            last = _last_packet_ts.get(proto)
            if last is None:
                continue
            silent_now = (now - last) > SILENCE_THRESHOLD_S
            was_silent = _is_silent.get(proto, False)
            if silent_now and not was_silent:
                _failure_events.append({
                    "ts": now, "protocol": proto, "signal": "silence",
                    "detail": f"no packets for >{SILENCE_THRESHOLD_S:.0f}s",
                })
            elif not silent_now and was_silent:
                _failure_events.append({
                    "ts": now, "protocol": proto, "signal": "recovered",
                    "detail": "packets resumed",
                })
            _is_silent[proto] = silent_now


# ── Capture (tshark subprocess) ─────────────────────────────────────────────

def _tshark_loop():
    cmd = [
        "tshark", "-i", IFACE, "-l", "-n",
        # Exclude this analyzer's own reporting traffic to the Metrics Collector -
        # it shares the target's network namespace, so without this it would
        # capture (and misclassify as "other") its own POST /analysis/update calls.
        "-f", "not port 9090",
        "-T", "fields",
        "-e", "frame.time_epoch",
        "-e", "frame.len",
        "-e", "tcp.srcport", "-e", "tcp.dstport",
        "-e", "udp.srcport", "-e", "udp.dstport",
        "-e", "tcp.flags.reset",
        "-e", "mqtt.msgtype",
        "-E", "separator=\t",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, bufsize=1)
    for line in proc.stdout:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 8:
            continue
        ts_s, len_s, tsrc, tdst, usrc, udst, rst, msgtype = parts
        try:
            ts, length = float(ts_s), int(len_s)
        except ValueError:
            continue
        proto = _classify(tsrc, tdst, usrc, udst)
        _record(ts, length, proto, rst == "1", msgtype)
    proc.wait()


def _tshark_loop_forever():
    while True:
        try:
            _tshark_loop()
        except Exception as e:
            print(f"[analyzer:{ANALYZER_NAME}] tshark error: {e}")
        time.sleep(2)  # interface may not be up yet on first try, or tshark died


# ── Reporting (POST snapshot every ~1s) ─────────────────────────────────────

def _report_loop():
    while True:
        time.sleep(1)
        _decay_histograms()
        _check_silence()
        with _lock:
            totals_snapshot = {k: dict(v) for k, v in _totals.items()}
            io_graph = [
                {"ts": sec, **{p: dict(v) for p, v in _buckets[sec].items()}}
                for sec in sorted(_bucket_order)
            ]
            # Round for transport; only relative shape/percentages are used downstream.
            size_hist_snapshot = {p: {l: round(c, 2) for l, c in v.items()}
                                   for p, v in _size_hist.items()}
            gap_hist_snapshot  = {p: {l: round(c, 2) for l, c in v.items()}
                                   for p, v in _gap_hist.items()}
            failure_events_snapshot = list(_failure_events)

        payload = {
            "analyzer":       ANALYZER_NAME,
            "iface":          IFACE,
            "totals":         totals_snapshot,
            "io_graph":       io_graph,
            "size_hist":      size_hist_snapshot,
            "gap_hist":       gap_hist_snapshot,
            "failure_events": failure_events_snapshot,
        }
        try:
            req = urllib.request.Request(
                f"{METRICS_URL}/analysis/update",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:
            pass


if __name__ == "__main__":
    print(f"[analyzer:{ANALYZER_NAME}] capturing on {IFACE}, reporting to {METRICS_URL}")
    threading.Thread(target=_tshark_loop_forever, daemon=True).start()
    _report_loop()
