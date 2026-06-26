"""
HTTP/2 Traffic Generator
------------------------
Sends HTTP/2 GET and POST requests using httpx (HTTP/2 enabled).
Supports multiplexed streams over a single connection.

Request rate, payload size, the GET/POST method distribution, the number of
concurrently multiplexed streams, and the set of target paths for GET and
POST requests (get_paths / post_paths) are all configurable at runtime.

The overall sending cadence is controlled by `pattern`: 'constant' (steady
`rate`), 'periodic_burst' (alternates between the base `rate` and a much
higher `burst_rate` for `burst_duration` seconds every `burst_interval`
seconds - useful for demonstrating multiplexed-stream bursts in Wireshark
I/O graphs), 'random' (exponentially-distributed/Poisson gaps between
sends, with the same mean rate as 'constant' - mimics bursty, human-driven
request traffic for the Temporal Analysis task), or 'ramp' (the effective
rate increases linearly from `ramp_start_rate` to `ramp_end_rate` over
`ramp_duration` seconds, then holds at `ramp_end_rate` - the assignment's
"ramping (linear increase over time)" sending pattern, applied per-phase
and independent of the controller's global warmup/cooldown ramp).

Exposes a small REST API so the Traffic Controller can start/stop/reconfigure
this generator at runtime and read its live statistics (used, among other
things, as the latency signal for Adaptive Control).
"""

import os, time, asyncio, random, threading
from typing import Optional


import httpx
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

app = FastAPI(
    title="MIC HTTP/2 Generator",
    description="Generates configurable, multiplexed HTTP/2 GET/POST traffic against the HTTP/2 target server.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TARGET_URL  = os.getenv("TARGET_URL",  "http://target-http2:8080")
METRICS_URL = os.getenv("METRICS_URL", "http://metrics:9090")
PORT        = int(os.getenv("PORT", "7001"))

state = {
    "running":            False,
    "rate":               50,
    "payload_size":       2048,
    "method_get_pct":     70,    # 70% GET, 30% POST
    "concurrent_streams": 3,
    "get_paths":          ["/"],       # target paths for GET requests, one is chosen at random per request
    "post_paths":         ["/data"],   # target paths for POST requests, one is chosen at random per request
    "pattern":            "constant",  # "constant" | "periodic_burst" | "random" | "ramp"
    "burst_rate":         400,         # requests/sec used during a burst window
    "burst_duration":     5,           # seconds: length of each burst window
    "burst_interval":     30,          # seconds: period between burst windows
    "ramp_start_rate":    0,           # requests/sec at the start of a 'ramp' pattern
    "ramp_end_rate":      100,         # requests/sec at the end of a 'ramp' pattern
    "ramp_duration":      60,          # seconds: how long the linear ramp takes
    "packets_sent":       0,
    "bytes_sent":         0,
    "errors":             0,
    "rate_bps":           0,
    "latency_ms":         0,
    "fault_rate":         0.0,   # 0.0-1.0: probability that a request is simulated as a failure (no real send)
    "extra_latency_ms":   0,     # artificial extra delay injected before each request
}

_lock = threading.Lock()
_bytes_window: list[tuple[float, int]] = []
_latency_window: list[tuple[float, float]] = []

# Wall-clock timestamp at which the current 'ramp' pattern run began. Reset
# whenever `pattern` transitions into 'ramp' (from something else) or the
# generator is (re)started while already in 'ramp' mode, so each ramp run
# starts counting from 0 again rather than picking up mid-ramp.
_ramp_started_at: Optional[float] = None
_last_pattern: Optional[str] = None

# Ring buffer of recent metric snapshots (one per _metrics() tick, ~5s apart),
# capped to the last 5 minutes. Exposed via /status so the dashboard can draw
# live rate/latency/error sparklines without polling a separate endpoint.
_HISTORY_MAXLEN = 60
_history: list[dict] = []


# ── Models (Swagger) ────────────────────────────────────────────────────────

class GeneratorConfig(BaseModel):
    """Configurable parameters. All fields optional; only provided keys are updated."""
    model_config = ConfigDict(extra="allow", json_schema_extra={
        "example": {"rate": 100, "payload_size": 4096, "method_get_pct": 70, "concurrent_streams": 5,
                     "get_paths": ["/", "/api/items"], "post_paths": ["/data", "/api/upload"],
                     "fault_rate": 0.0, "extra_latency_ms": 0}
    })
    rate: Optional[float] = Field(
        None, ge=0,
        description="Target send rate in requests per second (across all concurrent streams)."
    )
    payload_size: Optional[int] = Field(
        None, ge=0,
        description="Size (bytes) of the random payload sent as the body of each POST request."
    )
    method_get_pct: Optional[int] = Field(
        None, ge=0, le=100,
        description="Percentage (0-100) of requests sent as GET; the remainder are sent as POST. "
                     "Controls the request method distribution."
    )
    concurrent_streams: Optional[int] = Field(
        None, ge=1, le=20,
        description="Number of requests fired concurrently each cycle, all multiplexed over the "
                     "single shared HTTP/2 connection to the target."
    )
    get_paths: Optional[list[str]] = Field(
        None,
        description="List of target paths used for GET requests; one is chosen at random for "
                     "each GET request. Allows the generator to spread traffic across multiple "
                     "endpoints instead of always hitting the same path."
    )
    post_paths: Optional[list[str]] = Field(
        None,
        description="List of target paths used for POST requests; one is chosen at random for "
                     "each POST request."
    )
    pattern: Optional[str] = Field(
        None,
        description="Overall sending cadence: 'constant' (steady `rate`), 'periodic_burst' "
                     "(alternates between `rate` and `burst_rate` for `burst_duration` seconds "
                     "every `burst_interval` seconds), 'random' (exponentially-distributed/"
                     "Poisson gaps between sends, same mean rate as 'constant'), or 'ramp' "
                     "(effective rate increases linearly from `ramp_start_rate` to "
                     "`ramp_end_rate` over `ramp_duration` seconds, then holds at "
                     "`ramp_end_rate`)."
    )
    burst_rate: Optional[float] = Field(
        None, ge=0,
        description="Request rate (requests/sec) used during a burst window when "
                     "pattern='periodic_burst'."
    )
    burst_duration: Optional[float] = Field(
        None, ge=0,
        description="Length (seconds) of each burst window."
    )
    burst_interval: Optional[float] = Field(
        None, ge=0,
        description="Period (seconds) between the start of consecutive burst windows."
    )
    ramp_start_rate: Optional[float] = Field(
        None, ge=0,
        description="Request rate (requests/sec) at the start of a linear ramp, when "
                     "pattern='ramp'."
    )
    ramp_end_rate: Optional[float] = Field(
        None, ge=0,
        description="Request rate (requests/sec) at the end of a linear ramp, when "
                     "pattern='ramp'. The rate holds at this value once `ramp_duration` "
                     "has elapsed."
    )
    ramp_duration: Optional[float] = Field(
        None, gt=0,
        description="Duration (seconds) over which the rate increases linearly from "
                     "`ramp_start_rate` to `ramp_end_rate`, when pattern='ramp'."
    )
    fault_rate: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Fraction of requests (0-1) deliberately treated as failures, without sending, "
                     "for resilience and failure-injection demos."
    )
    extra_latency_ms: Optional[float] = Field(
        None, ge=0,
        description="Artificial extra delay (ms) injected before every request, simulating "
                     "network congestion or an overloaded target."
    )


class StatusResponse(BaseModel):
    running: bool
    rate: float
    payload_size: int
    method_get_pct: int
    concurrent_streams: int
    get_paths: list[str]
    post_paths: list[str]
    pattern: str
    burst_rate: float
    burst_duration: float
    burst_interval: float
    burst_active: bool = Field(
        False, description="True if a burst window is currently active (pattern='periodic_burst' only)."
    )
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


# ── Traffic loop ──────────────────────────────────────────────────────────────

async def _send_one(client: httpx.AsyncClient):
    with _lock:
        use_get       = random.randint(1, 100) <= state["method_get_pct"]
        payload_size  = state["payload_size"]
        fault_rate    = state["fault_rate"]
        extra_latency = state["extra_latency_ms"]
        get_paths     = state["get_paths"]  or ["/"]
        post_paths    = state["post_paths"] or ["/data"]

    t0 = time.perf_counter()

    if extra_latency > 0:
        await asyncio.sleep(extra_latency / 1000)

    if fault_rate > 0 and random.random() < fault_rate:
        # Injected fault: simulate a failed request without contacting the target.
        elapsed_ms = (time.perf_counter() - t0) * 1000
        with _lock:
            state["errors"] += 1
        _latency_window.append((time.time(), elapsed_ms))
        return

    try:
        if use_get:
            path = random.choice(get_paths)
            r = await client.get(f"{TARGET_URL}{path}", timeout=5.0)
            n = len(r.content)
        else:
            path = random.choice(post_paths)
            payload = os.urandom(payload_size)
            r = await client.post(f"{TARGET_URL}{path}", content=payload, timeout=5.0)
            n = payload_size

        elapsed_ms = (time.perf_counter() - t0) * 1000
        with _lock:
            state["packets_sent"] += 1
            state["bytes_sent"]   += n
        _bytes_window.append((time.time(), n))
        _latency_window.append((time.time(), elapsed_ms))

    except Exception:
        with _lock:
            state["errors"] += 1


def _is_burst_active(pattern: str, burst_duration: float, burst_interval: float) -> bool:
    """Returns True if, for pattern='periodic_burst', the current moment falls inside
    a burst window. Bursts recur every `burst_interval` seconds and last
    `burst_duration` seconds, aligned to the wall clock (epoch time) so the cadence
    is stable across restarts and observable in Wireshark I/O graphs."""
    if pattern != "periodic_burst" or burst_interval <= 0:
        return False
    return (time.time() % burst_interval) < burst_duration


def _ramp_progress(pattern: str, ramp_duration: float) -> float:
    """Returns the fraction (0.0-1.0) of `ramp_duration` elapsed since the current
    'ramp' run started. Tracks the start time in `_ramp_started_at`, resetting it
    whenever `pattern` just transitioned into 'ramp' (see _note_pattern_transition).
    Stateless with respect to wall-clock restarts: if the process restarts mid-ramp,
    the anchor is simply re-set, restarting the ramp from 0 rather than guessing."""
    global _ramp_started_at
    if pattern != "ramp" or ramp_duration <= 0:
        return 0.0
    if _ramp_started_at is None:
        _ramp_started_at = time.time()
    elapsed = time.time() - _ramp_started_at
    return max(0.0, min(1.0, elapsed / ramp_duration))


def _note_pattern_transition(pattern: str):
    """Resets the ramp anchor whenever `pattern` transitions into 'ramp' from
    something else, so each fresh ramp run starts counting from 0 again."""
    global _last_pattern, _ramp_started_at
    if pattern == "ramp" and _last_pattern != "ramp":
        _ramp_started_at = time.time()
    elif pattern != "ramp":
        _ramp_started_at = None
    _last_pattern = pattern


def _ramp_effective_rate(ramp_start_rate: float, ramp_end_rate: float, progress: float) -> float:
    """Linearly interpolates between ramp_start_rate and ramp_end_rate at the
    given progress fraction (0.0-1.0). This is the per-phase, per-protocol
    counterpart to the controller's global warmup/cooldown ramp: that one
    ramps the generic `rate` field for *all* generators in lockstep from/to 0
    at the very start/end of an entire profile run; this one ramps a single
    generator's own rate between two arbitrary values, for the duration of
    whichever phase sets pattern='ramp', and can run alongside other
    generators that are simultaneously constant/bursty/random."""
    return ramp_start_rate + (ramp_end_rate - ramp_start_rate) * progress


async def _generate():
    # httpx HTTP/2 client: keeps one TCP connection, multiplexes streams
    async with httpx.AsyncClient(http2=True, verify=False) as client:
        while True:
            with _lock:
                running        = state["running"]
                rate           = state["rate"]
                streams        = state["concurrent_streams"]
                pattern        = state["pattern"]
                burst_rate     = state["burst_rate"]
                burst_duration = state["burst_duration"]
                burst_interval = state["burst_interval"]
                ramp_start     = state["ramp_start_rate"]
                ramp_end       = state["ramp_end_rate"]
                ramp_duration  = state["ramp_duration"]

            _note_pattern_transition(pattern)

            if not running:
                await asyncio.sleep(0.1)
                continue

            if pattern == "ramp":
                progress = _ramp_progress(pattern, ramp_duration)
                effective_rate = _ramp_effective_rate(ramp_start, ramp_end, progress)
            else:
                effective_rate = rate
                if _is_burst_active(pattern, burst_duration, burst_interval):
                    effective_rate = max(rate, burst_rate)

            if effective_rate <= 0:
                await asyncio.sleep(0.1)
                continue

            # Fire `streams` concurrent requests, then wait for the right interval
            tasks = [_send_one(client) for _ in range(streams)]
            await asyncio.gather(*tasks, return_exceptions=True)

            interval = streams / effective_rate
            if pattern == "random":
                # Exponential inter-arrival time => Poisson process, same mean rate.
                await asyncio.sleep(random.expovariate(1.0 / interval))
            else:
                await asyncio.sleep(interval)


async def _metrics():
    while True:
        await asyncio.sleep(5)
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
                "generator":    "gen-http2",
                "running":      state["running"],
                "packets_sent": state["packets_sent"],
                "bytes_sent":   state["bytes_sent"],
                "errors":       state["errors"],
                "rate_bps":     state["rate_bps"],
                "rate":         state["rate"],
                "latency_ms":   state["latency_ms"],
            }

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(f"{METRICS_URL}/update", json=payload)
        except Exception:
            pass


def _run_background():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(_generate())
    loop.create_task(_metrics())
    loop.run_forever()


threading.Thread(target=_run_background, daemon=True).start()


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
           description="Updates any subset of: rate, payload_size, method_get_pct, concurrent_streams, "
                        "get_paths, post_paths, pattern ('constant'|'periodic_burst'|'random'|'ramp'), "
                        "burst_rate, burst_duration, burst_interval, ramp_start_rate, ramp_end_rate, "
                        "ramp_duration, fault_rate, extra_latency_ms.")
async def config(body: GeneratorConfig = Body(...)):
    with _lock:
        state.update({k: v for k, v in body.model_dump(exclude_none=True).items() if k in state})
    return {"ok": True}


@app.get("/status", response_model=StatusResponse, summary="Get live status and statistics")
async def status():
    with _lock:
        result = dict(state)
        result["burst_active"] = _is_burst_active(
            result["pattern"], result["burst_duration"], result["burst_interval"]
        )
        result["ramp_progress"] = _ramp_progress(result["pattern"], result["ramp_duration"])
        result["history"] = list(_history)
        return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)