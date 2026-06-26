"""
MQTT Traffic Generator
----------------------
Publishes messages to a Mosquitto broker at a configurable rate, across
multiple topics and QoS levels.

Also subscribes to the same set of topics, so the broker's fan-out traffic
(publish -> broker -> subscriber) is visible for the Wireshark analysis.
The number of topics in rotation is configurable (topic_count), and the QoS
of each published message can either be fixed (qos) or drawn from a
configurable probability distribution across QoS 0/1/2 (qos_distribution).

The overall sending cadence is controlled by `pattern`: 'constant' (fixed
1/rate interval between publishes), 'random' (exponentially-distributed/
Poisson gaps between publishes, with the same mean rate as 'constant') -
mimics bursty sensor/event traffic for the Temporal Analysis task,
'periodic_burst' (alternates between the base `rate` and a much higher
`burst_rate` for `burst_duration` seconds every `burst_interval` seconds),
or 'ramp' (the effective rate increases linearly from `ramp_start_rate` to
`ramp_end_rate` over `ramp_duration` seconds, then holds at `ramp_end_rate`).

Exposes a small REST API so the Traffic Controller can start/stop/reconfigure
this generator at runtime and read its live statistics.
"""

import os, time, threading, random, string
from typing import Optional, Union

import requests
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

app = FastAPI(
    title="MIC MQTT Generator",
    description="Publishes configurable MQTT traffic (rate, payload size, QoS, topics) to the Mosquitto broker.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BROKER_HOST  = os.getenv("BROKER_HOST", "mosquitto")
BROKER_PORT  = int(os.getenv("BROKER_PORT", "1883"))
METRICS_URL  = os.getenv("METRICS_URL", "http://metrics:9090")
PORT         = int(os.getenv("PORT", "7003"))

BASE_TOPICS = [
    "sensors/temperature",
    "sensors/humidity",
    "sensors/pressure",
    "actuators/control",
    "status/heartbeat",
]

QOS_OPTIONS = [0, 1, 2]

MAX_TOPIC_COUNT = 20


def _topic_list(n: int) -> list[str]:
    """Returns the n topic names currently in rotation for publish + subscribe.
    The first len(BASE_TOPICS) names are the descriptive base topics; beyond
    that, additional generic topics are generated on demand."""
    n = max(1, min(MAX_TOPIC_COUNT, n))
    if n <= len(BASE_TOPICS):
        return BASE_TOPICS[:n]
    return BASE_TOPICS + [f"load/topic-{i}" for i in range(len(BASE_TOPICS), n)]


# ── Generator state ───────────────────────────────────────────────────────────

state = {
    "running":      False,
    "rate":         10,       # messages per second
    "payload_size": 128,      # bytes
    "qos":          1,
    "topic_count":  len(BASE_TOPICS),  # number of topics rotated through (publish + subscribe)
    "qos_distribution": None,          # optional [w0, w1, w2] weights; overrides `qos` if set
    "pattern":      "constant",        # "constant" | "random" | "periodic_burst" | "ramp"
    "burst_rate":         100,         # messages/sec used during a burst window (periodic_burst)
    "burst_duration":     5,           # seconds: length of each burst window
    "burst_interval":     30,          # seconds: period between burst windows
    "ramp_start_rate":    0,           # messages/sec at the start of a 'ramp' pattern
    "ramp_end_rate":      50,          # messages/sec at the end of a 'ramp' pattern
    "ramp_duration":      60,          # seconds: how long the linear ramp takes
    "packets_sent": 0,
    "bytes_sent":   0,
    "messages_received": 0,   # messages received via our own subscription(s)
    "bytes_received":    0,
    "errors":       0,
    "rate_bps":     0,
    "latency_ms":   0,
    "fault_rate":         0.0,   # 0.0-1.0: probability that a publish is simulated as a failure (no real send)
    "extra_latency_ms":   0,     # artificial extra delay injected before each publish
}

_lock  = threading.Lock()
_bytes_window: list[tuple[float, int]] = []  # (timestamp, bytes)
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

# Topics we are currently subscribed to (kept in sync with state["topic_count"]).
_subscribed_topics: set[str] = set()


# ── Models (Swagger) ────────────────────────────────────────────────────────

class GeneratorConfig(BaseModel):
    """Configurable parameters. All fields optional; only provided keys are updated."""
    model_config = ConfigDict(extra="allow", json_schema_extra={
        "example": {"rate": 30, "payload_size": 256, "qos": 1, "topic_count": 5,
                     "qos_distribution": [0.5, 0.3, 0.2], "fault_rate": 0.0, "extra_latency_ms": 0}
    })
    rate: Optional[float] = Field(None, ge=0, description="Publish rate in messages per second.")
    payload_size: Optional[int] = Field(None, ge=0, description="Size (bytes) of each published message payload.")
    qos: Optional[int] = Field(
        None, ge=0, le=2,
        description="Fixed QoS level (0/1/2) used for every publish, unless qos_distribution is set."
    )
    topic_count: Optional[int] = Field(
        None, ge=1, le=MAX_TOPIC_COUNT,
        description="Number of topics rotated through for publishing and subscribing "
                     f"(1-{MAX_TOPIC_COUNT}). The first {len(BASE_TOPICS)} are the descriptive "
                     "base topics (sensors/..., actuators/..., status/...); additional topics "
                     "are named load/topic-N. Changing this re-subscribes to the new topic set."
    )
    qos_distribution: Optional[Union[list[float], dict[str, float]]] = Field(
        None,
        description="Optional probability weights for QoS levels 0/1/2, either as a list "
                     "[w0, w1, w2] or as a mapping {\"0\": w0, \"1\": w1, \"2\": w2} (as used by "
                     "the YAML profiles). If set (3 non-negative numbers, not all zero), each "
                     "publish draws its QoS from this distribution instead of using the fixed "
                     "`qos` value. Pass null/empty to fall back to the fixed `qos`."
    )
    pattern: Optional[str] = Field(
        None,
        description="Overall sending cadence: 'constant' (fixed 1/rate interval between "
                     "publishes), 'random' (exponentially-distributed/Poisson gaps between "
                     "publishes, same mean rate as 'constant'), 'periodic_burst' (alternates "
                     "between `rate` and `burst_rate` for `burst_duration` seconds every "
                     "`burst_interval` seconds), or 'ramp' (effective rate increases linearly "
                     "from `ramp_start_rate` to `ramp_end_rate` over `ramp_duration` seconds, "
                     "then holds at `ramp_end_rate`)."
    )
    burst_rate: Optional[float] = Field(
        None, ge=0,
        description="Publish rate (messages/sec) used during a burst window when pattern='periodic_burst'."
    )
    burst_duration: Optional[float] = Field(
        None, ge=0, description="Length (seconds) of each burst window."
    )
    burst_interval: Optional[float] = Field(
        None, ge=0, description="Period (seconds) between the start of consecutive burst windows."
    )
    ramp_start_rate: Optional[float] = Field(
        None, ge=0, description="Publish rate (messages/sec) at the start of a linear ramp, when pattern='ramp'."
    )
    ramp_end_rate: Optional[float] = Field(
        None, ge=0,
        description="Publish rate (messages/sec) at the end of a linear ramp, when pattern='ramp'. "
                     "Holds at this value once `ramp_duration` has elapsed."
    )
    ramp_duration: Optional[float] = Field(
        None, gt=0,
        description="Duration (seconds) over which the rate increases linearly from "
                     "`ramp_start_rate` to `ramp_end_rate`, when pattern='ramp'."
    )
    fault_rate: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Fraction of publishes (0-1) deliberately treated as failures, without sending, "
                     "for resilience and failure-injection demos."
    )
    extra_latency_ms: Optional[float] = Field(
        None, ge=0,
        description="Artificial extra delay (ms) injected before every publish, simulating "
                     "network congestion or an overloaded broker."
    )


class StatusResponse(BaseModel):
    running: bool
    rate: float
    payload_size: int
    qos: int
    topic_count: int
    qos_distribution: Optional[list[float]] = None
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
    messages_received: int
    bytes_received: int
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


def _normalize_qos_distribution(value):
    """Accepts either [w0, w1, w2] or {"0": w0, "1": w1, "2": w2} (the form used by the
    YAML profiles, where YAML's integer keys 0/1/2 become string keys over JSON) and
    returns a plain [w0, w1, w2] list, or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        return [float(value.get(str(q), value.get(q, 0)) or 0) for q in (0, 1, 2)]
    return [float(w) for w in value]


# ── MQTT client setup ─────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(client_id="gen-mqtt")

def _resubscribe(topic_count: int):
    """Sync the broker subscriptions with the current topic_count: subscribe to
    any newly-added topics and unsubscribe from any that fell out of range."""
    global _subscribed_topics
    new_topics = set(_topic_list(topic_count))
    for topic in _subscribed_topics - new_topics:
        mqtt_client.unsubscribe(topic)
    for topic in new_topics - _subscribed_topics:
        mqtt_client.subscribe(topic, qos=0)
    _subscribed_topics = new_topics


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] Connected to broker {BROKER_HOST}:{BROKER_PORT}")
        with _lock:
            topic_count = state["topic_count"]
        global _subscribed_topics
        _subscribed_topics = set()  # broker session is fresh; re-subscribe to everything
        _resubscribe(topic_count)
    else:
        print(f"[MQTT] Connection failed rc={rc}")


def on_message(client, userdata, msg):
    """Counts messages we receive on our own subscriptions (i.e. the broker's
    fan-out of the traffic we and any other publishers generate)."""
    with _lock:
        state["messages_received"] += 1
        state["bytes_received"]    += len(msg.payload)


mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.loop_start()

def _connect_with_retry():
    while True:
        try:
            mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            return
        except Exception as e:
            print(f"[MQTT] Broker not ready, retrying: {e}")
            time.sleep(2)

threading.Thread(target=_connect_with_retry, daemon=True).start()


# ── Traffic generation loop ───────────────────────────────────────────────────

def _is_burst_active(pattern: str, burst_duration: float, burst_interval: float) -> bool:
    """Returns True if, for pattern='periodic_burst', the current moment falls inside
    a burst window. Same wall-clock-modulo mechanism as the HTTP/2 generator."""
    if pattern != "periodic_burst" or burst_interval <= 0:
        return False
    return (time.time() % burst_interval) < burst_duration


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
    """Linearly interpolates between ramp_start_rate and ramp_end_rate."""
    return ramp_start_rate + (ramp_end_rate - ramp_start_rate) * progress


def _send_loop():
    while True:
        with _lock:
            running        = state["running"]
            rate           = state["rate"]
            payload_size   = state["payload_size"]
            qos            = state["qos"]
            qos_dist       = state["qos_distribution"]
            topic_count    = state["topic_count"]
            pattern        = state["pattern"]
            burst_rate     = state["burst_rate"]
            burst_duration = state["burst_duration"]
            burst_interval = state["burst_interval"]
            ramp_start     = state["ramp_start_rate"]
            ramp_end       = state["ramp_end_rate"]
            ramp_duration  = state["ramp_duration"]
            fault_rate     = state["fault_rate"]
            extra_latency  = state["extra_latency_ms"]

        _note_pattern_transition(pattern)

        if not running:
            time.sleep(0.1)
            continue

        if pattern == "ramp":
            progress = _ramp_progress(pattern, ramp_duration)
            effective_rate = _ramp_effective_rate(ramp_start, ramp_end, progress)
        else:
            effective_rate = rate
            if _is_burst_active(pattern, burst_duration, burst_interval):
                effective_rate = max(rate, burst_rate)

        if effective_rate <= 0:
            time.sleep(0.1)
            continue

        interval = 1.0 / effective_rate
        if pattern == "random":
            # Exponential inter-arrival time => Poisson process, same mean rate.
            sleep_time = random.expovariate(1.0 / interval)
        else:
            sleep_time = interval

        t0 = time.perf_counter()

        if extra_latency > 0:
            time.sleep(extra_latency / 1000)

        if fault_rate > 0 and random.random() < fault_rate:
            # Injected fault: simulate a failed publish without contacting the broker.
            elapsed_ms = (time.perf_counter() - t0) * 1000
            with _lock:
                state["errors"] += 1
            _latency_window.append((time.time(), elapsed_ms))
            time.sleep(sleep_time)
            continue

        topic   = random.choice(_topic_list(topic_count))
        payload = "".join(random.choices(string.ascii_letters + string.digits, k=payload_size))

        # If a valid qos_distribution is configured, draw QoS from it; otherwise
        # use the fixed `qos` value.
        publish_qos = qos
        if qos_dist and len(qos_dist) == 3 and all(w >= 0 for w in qos_dist) and sum(qos_dist) > 0:
            publish_qos = random.choices(QOS_OPTIONS, weights=qos_dist)[0]

        try:
            result = mqtt_client.publish(topic, payload, qos=publish_qos)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                n = len(payload.encode())
                elapsed_ms = (time.perf_counter() - t0) * 1000
                with _lock:
                    state["packets_sent"] += 1
                    state["bytes_sent"]   += n
                _bytes_window.append((time.time(), n))
                _latency_window.append((time.time(), elapsed_ms))
            else:
                with _lock:
                    state["errors"] += 1
        except Exception:
            with _lock:
                state["errors"] += 1

        time.sleep(sleep_time)


def _metrics_loop():
    """Push stats to metrics collector every 5 seconds."""
    while True:
        time.sleep(5)

        # calculate bytes/sec over last 10 seconds
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
                "generator":    "gen-mqtt",
                "running":      state["running"],
                "packets_sent": state["packets_sent"],
                "bytes_sent":   state["bytes_sent"],
                "errors":       state["errors"],
                "rate_bps":     state["rate_bps"],
                "rate":         state["rate"],
                "latency_ms":   state["latency_ms"],
            }

        try:
            requests.post(f"{METRICS_URL}/update", json=payload, timeout=2)
        except Exception:
            pass


threading.Thread(target=_send_loop,   daemon=True).start()
threading.Thread(target=_metrics_loop, daemon=True).start()


# ── REST API ──────────────────────────────────────────────────────────────────

@app.post("/start", response_model=OkResponse, summary="Start generating traffic",
          description="Starts the generator and optionally applies an initial configuration (same fields as PATCH /config).")
async def start(body: GeneratorConfig = Body(default=GeneratorConfig())):
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items() if k in state}
    if "qos_distribution" in updates:
        updates["qos_distribution"] = _normalize_qos_distribution(updates["qos_distribution"])
    with _lock:
        state["running"] = True
        state.update(updates)
        topic_count = state["topic_count"]
    if "topic_count" in updates:
        _resubscribe(topic_count)
    return {"ok": True}


@app.post("/stop", response_model=OkResponse, summary="Stop generating traffic")
async def stop():
    with _lock:
        state["running"] = False
    return {"ok": True}


@app.patch("/config", response_model=OkResponse, summary="Update configuration at runtime",
           description="Updates any subset of: rate, payload_size, qos (0/1/2), topic_count, "
                        "qos_distribution, pattern ('constant'|'random'|'periodic_burst'|'ramp'), "
                        "burst_rate, burst_duration, burst_interval, ramp_start_rate, ramp_end_rate, "
                        "ramp_duration, fault_rate, extra_latency_ms. Changing topic_count "
                        "re-subscribes this generator to the new set of topics.")
async def config(body: GeneratorConfig = Body(...)):
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items() if k in state}
    if "qos_distribution" in updates:
        updates["qos_distribution"] = _normalize_qos_distribution(updates["qos_distribution"])
    with _lock:
        state.update(updates)
        topic_count = state["topic_count"]
    if "topic_count" in updates:
        _resubscribe(topic_count)
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
