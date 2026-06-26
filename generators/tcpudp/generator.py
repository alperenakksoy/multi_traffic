"""
TCP/UDP Raw Traffic Generator
------------------------------
Sends raw TCP and UDP packets at a configurable rate.

Two size/timing modes (key feature for Wireshark Behavioral Fingerprinting analysis):

  NORMAL mode  -> fixed packet size (512B), fixed interval (100ms)
                  Creates a clear statistical fingerprint, easily detected.

  STEALTH mode -> random packet size (64-1400B), Poisson-distributed timing
                  Mimics real user traffic, statistically indistinguishable.

Independently of `mode`, a `pattern` controls the overall sending cadence
(temporal shape of the traffic, relevant for the Temporal Analysis task):

  constant       -> one packet per interval, interval derived from tcp_rate/udp_rate
  periodic_burst -> sends `burst_size` packets back-to-back, then idles for
                     `burst_interval` seconds, repeating periodically
  random         -> exponentially-distributed (Poisson) gaps between sends
  ramp           -> total packet rate increases linearly from `ramp_start_rate`
                     to `ramp_end_rate` (packets/sec, combined TCP+UDP) over
                     `ramp_duration` seconds, then holds at `ramp_end_rate`;
                     the combined rate is split into TCP/UDP using `tcp_ratio`,
                     same as the other patterns

Exposes a small REST API so the Traffic Controller can start/stop/reconfigure
this generator at runtime and read its live statistics (including TCP connect
latency, used as a signal for Adaptive Control).
"""

import os, time, socket, random, threading
from typing import Optional

import numpy as np
import requests as req_sync
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

app = FastAPI(
    title="MIC TCP/UDP Generator",
    description="Sends raw TCP/UDP traffic with configurable rate, packet size, protocol mix and "
                 "Normal vs. Stealth timing/size patterns (used for the Behavioral Fingerprinting analysis).",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TARGET_HOST = os.getenv("TARGET_HOST", "target-tcpudp")
TARGET_PORT = int(os.getenv("TARGET_PORT", "9999"))
METRICS_URL = os.getenv("METRICS_URL", "http://metrics:9090")
PORT        = int(os.getenv("PORT", "7004"))

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
    "pattern":      "constant", # "constant" | "periodic_burst" | "random" | "ramp"
    "burst_size":     10,       # packets per burst (periodic_burst)
    "burst_interval": 1.0,      # seconds to idle between bursts (periodic_burst)
    "ramp_start_rate":  0,      # combined TCP+UDP packets/sec at the start of a 'ramp' pattern
    "ramp_end_rate":    50,     # combined TCP+UDP packets/sec at the end of a 'ramp' pattern
    "ramp_duration":    60,     # seconds: how long the linear ramp takes
    "packets_sent": 0,
    "bytes_sent":   0,
    "errors":       0,
    "rate_bps":     0,
    "latency_ms":   0,
    "fault_rate":         0.0,   # 0.0-1.0: probability that a send is simulated as a failure (no real send)
    "extra_latency_ms":   0,     # artificial extra delay injected before each send
}

_lock = threading.Lock()
_bytes_window: list[tuple[float, int]] = []
_latency_window: list[tuple[float, float]] = []

# Wall-clock timestamp at which the current 'ramp' pattern run began. See the
# HTTP/2 generator for the full rationale; same mechanism here.
_ramp_started_at: Optional[float] = None
_last_pattern: Optional[str] = None

# Ring buffer of recent metric snapshots (one per _metrics_loop() tick, ~5s apart),
# capped to the last 5 minutes. Exposed via /status so the dashboard can draw
# live rate/latency/error sparklines without polling a separate endpoint.
_HISTORY_MAXLEN = 60
_history: list[dict] = []


# ── Models (Swagger) ────────────────────────────────────────────────────────

class GeneratorConfig(BaseModel):
    """Configurable parameters. All fields optional; only provided keys are updated."""
    model_config = ConfigDict(extra="allow", json_schema_extra={
        "example": {"mode": "stealth", "pattern": "periodic_burst", "tcp_rate": 20, "udp_rate": 10,
                     "tcp_ratio": 60, "burst_size": 10, "burst_interval": 1.0,
                     "fault_rate": 0.0, "extra_latency_ms": 0}
    })
    mode: Optional[str] = Field(
        None, description="Packet-size/timing-base mode: 'normal' (fixed size) or 'stealth' (random size)."
    )
    pattern: Optional[str] = Field(
        None,
        description="Overall sending cadence: 'constant' (one packet per interval derived from "
                     "tcp_rate/udp_rate), 'periodic_burst' (burst_size packets back-to-back, then "
                     "idle for burst_interval seconds), 'random' (Poisson/exponentially-"
                     "distributed gaps between sends), or 'ramp' (combined TCP+UDP packet rate "
                     "increases linearly from `ramp_start_rate` to `ramp_end_rate` over "
                     "`ramp_duration` seconds, then holds at `ramp_end_rate`; split into "
                     "TCP/UDP using `tcp_ratio`)."
    )
    tcp_rate: Optional[float] = Field(None, ge=0, description="TCP packets/sec target (used by 'constant'/'random' patterns).")
    udp_rate: Optional[float] = Field(None, ge=0, description="UDP packets/sec target (used by 'constant'/'random' patterns).")
    packet_size: Optional[int] = Field(None, ge=0, description="Fixed packet size (bytes) in 'normal' mode.")
    mean_interval: Optional[float] = None
    min_size: Optional[int] = Field(None, ge=0, description="Minimum packet size (bytes) in 'stealth' mode.")
    max_size: Optional[int] = Field(None, ge=0, description="Maximum packet size (bytes) in 'stealth' mode.")
    tcp_ratio: Optional[int] = Field(None, ge=0, le=100, description="Percentage of packets sent as TCP (rest UDP).")
    burst_size: Optional[int] = Field(
        None, ge=1, le=200,
        description="Number of packets sent back-to-back per burst, when pattern='periodic_burst'."
    )
    burst_interval: Optional[float] = Field(
        None, ge=0,
        description="Seconds to idle between bursts, when pattern='periodic_burst'."
    )
    ramp_start_rate: Optional[float] = Field(
        None, ge=0,
        description="Combined TCP+UDP packets/sec at the start of a linear ramp, when pattern='ramp'."
    )
    ramp_end_rate: Optional[float] = Field(
        None, ge=0,
        description="Combined TCP+UDP packets/sec at the end of a linear ramp, when pattern='ramp'. "
                     "Holds at this value once `ramp_duration` has elapsed."
    )
    ramp_duration: Optional[float] = Field(
        None, gt=0,
        description="Duration (seconds) over which the combined rate increases linearly from "
                     "`ramp_start_rate` to `ramp_end_rate`, when pattern='ramp'."
    )
    fault_rate: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Fraction of sends (0-1) deliberately treated as failures, without sending, "
                     "for resilience and failure-injection demos."
    )
    extra_latency_ms: Optional[float] = Field(
        None, ge=0,
        description="Artificial extra delay (ms) injected before every send, simulating "
                     "network congestion or an overloaded target."
    )


class StatusResponse(BaseModel):
    running: bool
    mode: str
    pattern: str
    tcp_rate: float
    udp_rate: float
    packet_size: int
    mean_interval: float
    min_size: int
    max_size: int
    tcp_ratio: int
    burst_size: int
    burst_interval: float
    ramp_start_rate: float
    ramp_end_rate: float
    ramp_duration: float
    ramp_progress: float = Field(
        0.0, description="Fraction (0.0-1.0) of `ramp_duration` elapsed since the current "
                          "'ramp' pattern run started (pattern='ramp' only; 0.0 otherwise)."
    )
    packets_sent: int
    bytes_sent: int
    errors: int
    rate_bps: int
    latency_ms: float
    fault_rate: float
    extra_latency_ms: float
    history: list[dict] = Field(
        default_factory=list,
        description="Recent metric snapshots (~5s apart, up to 5 minutes), each "
                     "{ts, rate_bps, latency_ms, errors, packets_sent}. For live dashboard charts."
    )


class OkResponse(BaseModel):
    ok: bool = True
    mode: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normal_params() -> tuple[int, float, str]:
    """Fixed size and fixed interval; leaves a clear Wireshark fingerprint."""
    with _lock:
        size     = state["packet_size"]
        tcp_rate = state["tcp_rate"]
        udp_rate = state["udp_rate"]
        tcp_pct  = state["tcp_ratio"]

    proto    = "tcp" if random.randint(1, 100) <= tcp_pct else "udp"
    rate     = tcp_rate if proto == "tcp" else udp_rate
    interval = 1.0 / rate if rate > 0 else 0.5
    return size, interval, proto


def _stealth_params() -> tuple[int, float, str]:
    """Random size and Poisson timing; mimics real user traffic patterns."""
    with _lock:
        mean     = state["mean_interval"]
        min_s    = state["min_size"]
        max_s    = state["max_size"]
        tcp_pct  = state["tcp_ratio"]

    size     = random.randint(min_s, max_s)
    # Exponential distribution = inter-arrival time of a Poisson process
    interval = np.random.exponential(mean)
    proto    = "tcp" if random.randint(1, 100) <= tcp_pct else "udp"
    return size, interval, proto


def _send_tcp(data: bytes):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    t0 = time.perf_counter()
    try:
        s.connect((TARGET_HOST, TARGET_PORT))
        s.sendall(data)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        with _lock:
            state["packets_sent"] += 1
            state["bytes_sent"]   += len(data)
        _bytes_window.append((time.time(), len(data)))
        _latency_window.append((time.time(), elapsed_ms))
    except Exception:
        with _lock:
            state["errors"] += 1
    finally:
        s.close()


def _send_udp(data: bytes):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(data, (TARGET_HOST, TARGET_PORT))
        with _lock:
            state["packets_sent"] += 1
            state["bytes_sent"]   += len(data)
        _bytes_window.append((time.time(), len(data)))
    except Exception:
        with _lock:
            state["errors"] += 1
    finally:
        s.close()


# ── Pattern helpers (ramp progress tracking) ───────────────────────────────────

def _ramp_progress(pattern: str, ramp_duration: float) -> float:
    """Returns the fraction (0.0-1.0) of `ramp_duration` elapsed since the current
    'ramp' run started. See the HTTP/2 generator for the full rationale."""
    global _ramp_started_at
    if pattern != "ramp" or ramp_duration <= 0:
        return 0.0
    if _ramp_started_at is None:
        _ramp_started_at = time.time()
    elapsed = time.time() - _ramp_started_at
    return max(0.0, min(1.0, elapsed / ramp_duration))


def _note_pattern_transition(pattern: str):
    """Resets the ramp anchor whenever `pattern` transitions into 'ramp'."""
    global _last_pattern, _ramp_started_at
    if pattern == "ramp" and _last_pattern != "ramp":
        _ramp_started_at = time.time()
    elif pattern != "ramp":
        _ramp_started_at = None
    _last_pattern = pattern


def _ramp_effective_rate(ramp_start_rate: float, ramp_end_rate: float, progress: float) -> float:
    """Linearly interpolates between ramp_start_rate and ramp_end_rate. Used here as
    a single combined TCP+UDP packets/sec target; the per-packet protocol choice
    still comes from `tcp_ratio` inside _normal_params/_stealth_params, so the
    ramp only overrides the *interval* those helpers would otherwise compute from
    the fixed tcp_rate/udp_rate."""
    return ramp_start_rate + (ramp_end_rate - ramp_start_rate) * progress


# ── Traffic loop ──────────────────────────────────────────────────────────────

def _send_loop():
    while True:
        with _lock:
            running        = state["running"]
            mode           = state["mode"]
            pattern        = state["pattern"]
            fault_rate     = state["fault_rate"]
            extra_latency  = state["extra_latency_ms"]
            burst_size     = max(1, state["burst_size"])
            burst_interval = max(0.0, state["burst_interval"])
            ramp_start     = state["ramp_start_rate"]
            ramp_end       = state["ramp_end_rate"]
            ramp_duration  = state["ramp_duration"]

        _note_pattern_transition(pattern)

        if not running:
            time.sleep(0.1)
            continue

        # If ramping, compute one combined-rate interval up front for this
        # iteration's packet(s); _normal_params/_stealth_params still supply
        # packet size and TCP/UDP protocol choice (via tcp_ratio), but their
        # own rate-derived interval is overridden below when pattern='ramp'.
        ramp_interval = None
        if pattern == "ramp":
            progress = _ramp_progress(pattern, ramp_duration)
            effective_rate = _ramp_effective_rate(ramp_start, ramp_end, progress)
            if effective_rate <= 0:
                time.sleep(0.1)
                continue
            ramp_interval = 1.0 / effective_rate

        # `pattern` controls the overall sending cadence; `mode` (handled inside
        # _normal_params/_stealth_params) independently controls packet size and
        # the base interval derived from tcp_rate/udp_rate.
        n_packets = burst_size if pattern == "periodic_burst" else 1

        for i in range(n_packets):
            size, interval, proto = (
                _normal_params() if mode == "normal" else _stealth_params()
            )
            if ramp_interval is not None:
                interval = ramp_interval

            if extra_latency > 0:
                time.sleep(extra_latency / 1000)

            if fault_rate > 0 and random.random() < fault_rate:
                # Injected fault: simulate a failed send without touching the socket.
                with _lock:
                    state["errors"] += 1
                _latency_window.append((time.time(), extra_latency))
            else:
                payload = os.urandom(size)
                if proto == "tcp":
                    _send_tcp(payload)
                else:
                    _send_udp(payload)

            if pattern == "periodic_burst":
                # Tight gap between packets within a burst; the real pause
                # happens once after the whole burst, below.
                if i < n_packets - 1:
                    time.sleep(0.005)
            elif pattern == "random":
                # Exponential inter-arrival time => Poisson process, but
                # independent of the size mode's own interval.
                time.sleep(np.random.exponential(interval))
            else:  # "constant"
                time.sleep(interval)

        if pattern == "periodic_burst":
            time.sleep(burst_interval)


def _metrics_loop():
    while True:
        time.sleep(5)
        now    = time.time()
        recent = [b for ts, b in _bytes_window if now - ts <= 10]
        _bytes_window[:] = [(ts, b) for ts, b in _bytes_window if now - ts <= 10]
        rate_bps = sum(recent) / 10 if recent else 0

        recent_lat = [l for ts, l in _latency_window if now - ts <= 10]
        _latency_window[:] = [(ts, l) for ts, l in _latency_window if now - ts <= 10]
        avg_latency = sum(recent_lat) / len(recent_lat) if recent_lat else 0

        with _lock:
            state["rate_bps"]   = int(rate_bps)
            state["latency_ms"] = round(avg_latency, 2)
            _history.append({
                "ts":           now,
                "rate_bps":     state["rate_bps"],
                "latency_ms":   state["latency_ms"],
                "errors":       state["errors"],
                "packets_sent": state["packets_sent"],
            })
            del _history[:-_HISTORY_MAXLEN]
            payload = {
                "generator":    "gen-tcpudp",
                "running":      state["running"],
                "mode":         state["mode"],
                "packets_sent": state["packets_sent"],
                "bytes_sent":   state["bytes_sent"],
                "errors":       state["errors"],
                "rate_bps":     state["rate_bps"],
                "latency_ms":   state["latency_ms"],
            }

        try:
            req_sync.post(f"{METRICS_URL}/update", json=payload, timeout=2)
        except Exception:
            pass


threading.Thread(target=_send_loop,    daemon=True).start()
threading.Thread(target=_metrics_loop, daemon=True).start()


# ── REST API ──────────────────────────────────────────────────────────────────

@app.post("/start", response_model=OkResponse, summary="Start generating traffic",
          description="Starts the generator and optionally applies an initial configuration (same fields as PATCH /config).")
async def start(body: GeneratorConfig = Body(default=GeneratorConfig())):
    with _lock:
        state["running"] = True
        state.update({k: v for k, v in body.model_dump(exclude_none=True).items() if k in state})
    return {"ok": True}


@app.post("/stop", response_model=OkResponse, summary="Stop generating traffic")
async def stop():
    with _lock:
        state["running"] = False
    return {"ok": True}


@app.patch("/config", response_model=OkResponse, summary="Update configuration at runtime",
           description="Updates any subset of: mode ('normal'|'stealth'), pattern "
                        "('constant'|'periodic_burst'|'random'|'ramp'), tcp_rate, udp_rate, "
                        "packet_size, mean_interval, min_size, max_size, tcp_ratio, "
                        "burst_size, burst_interval, ramp_start_rate, ramp_end_rate, "
                        "ramp_duration, fault_rate, extra_latency_ms.")
async def config(body: GeneratorConfig = Body(...)):
    with _lock:
        state.update({k: v for k, v in body.model_dump(exclude_none=True).items() if k in state})
        mode = state["mode"]
    return {"ok": True, "mode": mode}


@app.get("/status", response_model=StatusResponse, summary="Get live status and statistics")
async def status():
    with _lock:
        result = dict(state)
        result["ramp_progress"] = _ramp_progress(result["pattern"], result["ramp_duration"])
        result["history"] = list(_history)
        return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
