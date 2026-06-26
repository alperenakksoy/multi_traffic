"""
Traffic Controller
------------------
Central REST API for starting/stopping generators, loading YAML profiles,
running multi-phase traffic plans, and autonomously adapting traffic rates
based on live error-rate (and latency, where reported) feedback.

All configuration changes and adaptive decisions are logged with timestamps
and exposed via GET /log and GET /status.
"""

import os, glob, asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Optional

import httpx
import yaml
from fastapi import FastAPI, Body, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict

# ── OpenAPI metadata ─────────────────────────────────────────────────────────

tags_metadata = [
    {
        "name": "Control",
        "description": "Start or stop the entire traffic-generation system (all generators, all phases).",
    },
    {
        "name": "Configuration",
        "description": "Load YAML traffic profiles and patch individual generators at runtime.",
    },
    {
        "name": "Monitoring",
        "description": "Inspect live status, aggregated metrics and the timestamped configuration-change log.",
    },
    {
        "name": "Adaptive Control",
        "description": (
            "Autonomous, closed-loop traffic shaping: the controller periodically reads each "
            "generator's error rate (and latency, where reported) and automatically scales "
            "its rate up or down according to the thresholds defined in the active YAML "
            "profile's `adaptive_control` block; no human interaction required."
        ),
    },
]

app = FastAPI(
    title="MIC Traffic Controller",
    description=(
        "Central configuration & orchestration API for the **Multi-Protocol Traffic "
        "Generation and Analysis** system (HSRW · Mobile & Internet Computing · SS2026).\n\n"
        "Coordinates the HTTP/2, QUIC, MQTT and TCP/UDP generators, loads multi-phase "
        "YAML traffic profiles, exposes live metrics, and runs an autonomous "
        "Adaptive Control loop that re-tunes generator rates based on observed error rates."
    ),
    version="1.0.0",
    openapi_tags=tags_metadata,
    contact={"name": "MIC Final Project · SS2026", "url": "https://www.hochschule-rhein-waal.de"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────

GENERATORS = {
    "gen-http2":  os.getenv("GEN_HTTP2_URL",  "http://gen-http2:7001"),
    "gen-quic":   os.getenv("GEN_QUIC_URL",   "http://gen-quic:7002"),
    "gen-mqtt":   os.getenv("GEN_MQTT_URL",   "http://gen-mqtt:7003"),
    "gen-tcpudp": os.getenv("GEN_TCPUDP_URL", "http://gen-tcpudp:7004"),
}
METRICS_URL  = os.getenv("METRICS_URL", "http://metrics:9090")
CONFIG_DIR   = "/app/config"

# Docker containers default to UTC, but the dashboard's "now()" uses the
# browser's local time (Europe/Berlin, i.e. CET/CEST). Without this, every
# log entry written here would be off by 1-2 hours compared to the
# client-side log entries the dashboard adds for the same action.
LOG_TZ = ZoneInfo(os.getenv("LOG_TZ", "Europe/Berlin"))

# Which state keys hold "rate" for each generator (used by Adaptive Control)
RATE_FIELDS = {
    "gen-http2":  ["rate"],
    "gen-quic":   ["rate"],
    "gen-mqtt":   ["rate"],
    "gen-tcpudp": ["tcp_rate", "udp_rate"],
}

# ── State ─────────────────────────────────────────────────────────────────────

state: dict[str, Any] = {
    "running":         False,
    "active_profile":  None,
    "active_phase":    None,
    "phase_task":      None,   # background asyncio task running the phase plan
    "adaptive_enabled": False,
    "adaptive_task":    None,  # background asyncio task running the adaptive loop
    "adaptive_status":  {},    # generator -> last adaptive decision
    "ramp_status":      None,  # {"phase": "warmup"|"cooldown", "progress": 0.0-1.0} while ramping
}
log_entries: list[dict] = []


def _log(message: str, level: str = "info"):
    entry = {
        "time":    datetime.now(LOG_TZ).strftime("%H:%M:%S"),
        "level":   level,
        "message": message,
    }
    log_entries.append(entry)
    if len(log_entries) > 200:
        log_entries.pop(0)
    print(f"[{entry['time']}] {message}")


# ── Pydantic models (for Swagger / OpenAPI) ─────────────────────────────────────

class OkResponse(BaseModel):
    ok: bool = True
    note: Optional[str] = Field(None, description="Optional human-readable detail.")


class StartStopResponse(BaseModel):
    ok: bool = True
    profile: Optional[str] = Field(None, description="Profile that was (re)started, if any.")
    note: Optional[str] = None


class ConfigLoadRequest(BaseModel):
    profile: str = Field(..., description="Name of a YAML file in config/ (without extension)", examples=["mqtt_heavy"])


class GeneratorPatch(BaseModel):
    """Arbitrary key/value overrides forwarded verbatim to the target generator's `/config` endpoint."""
    model_config = ConfigDict(extra="allow", json_schema_extra={
        "example": {"rate": 80, "payload_size": 4096}
    })


class LogEntry(BaseModel):
    time: str = Field(..., description="HH:MM:SS timestamp")
    level: str = Field(..., description="info | success | adaptive")
    message: str


class LogResponse(BaseModel):
    log: list[LogEntry]


class ProfilesResponse(BaseModel):
    profiles: list[str] = Field(..., description="Names of available YAML profiles in config/")


class HealthResponse(BaseModel):
    ok: bool = True


class RampStatus(BaseModel):
    phase: str = Field(..., description="'warmup' or 'cooldown'")
    progress: float = Field(..., description="Ramp progress, 0.0 (just started) to 1.0 (complete)")


class StatusResponse(BaseModel):
    running: bool
    active_profile: Optional[str]
    active_phase: Optional[str]
    adaptive_enabled: bool
    adaptive_status: dict[str, Any] = Field(default_factory=dict, description="Most recent Adaptive Control decision per generator")
    ramp_status: Optional[RampStatus] = Field(None, description="Active warmup/cooldown ramp, if any")
    generators: dict[str, Any] = Field(..., description="Live /status response of each generator")
    metrics: dict[str, Any] = Field(..., description="Aggregated /metrics response of the Metrics Collector")
    analysis: dict[str, Any] = Field(
        default_factory=dict,
        description="Aggregated live network-analysis snapshot (real captured protocol "
                     "distribution + I/O graph) from the network-analyzer sidecars, if any are running.",
    )
    log: list[LogEntry]


class AdaptiveDecision(BaseModel):
    multiplier: float = Field(..., description="Current scaling factor applied to the profile's base rate(s)")
    error_rate: float = Field(..., description="Error rate observed in the last check interval")
    action: str = Field(..., description="'up', 'down', or 'hold'")
    applied: dict[str, float] = Field(..., description="Rate values that were sent to the generator")
    checked_at: str


class AdaptiveStatusResponse(BaseModel):
    enabled: bool
    generators: dict[str, AdaptiveDecision]


class AdaptiveToggleResponse(BaseModel):
    ok: bool = True
    enabled: bool


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _call(method: str, url: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await getattr(client, method)(url, **kwargs)
            return r.json()
    except Exception as e:
        return {"error": str(e)}


def _load_yaml(profile: str) -> dict:
    path = os.path.join(CONFIG_DIR, f"{profile}.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Profile '{profile}' not found in {CONFIG_DIR}")
    with open(path) as f:
        return yaml.safe_load(f)


async def _ramp_rates(configs: dict[str, dict], duration: float, direction: str, label: str):
    """
    Linearly ramps the rate field(s) (per RATE_FIELDS) of `configs` from 0 up to
    their target values (direction='up'), or from their target values down to 0
    (direction='down'), over `duration` seconds, via periodic PATCH /config calls.

    This implements the global `warmup`/`cooldown` periods: every generator's
    rate climbs from/to 0 once, at the very start/end of an entire profile run.

    This is distinct from each generator's own pattern='ramp' sending pattern
    (see e.g. generators/http2/generator.py): that one ramps a single
    generator's rate between two arbitrary values (ramp_start_rate ->
    ramp_end_rate) for the duration of whichever phase requests it, computed
    entirely inside the generator from a wall-clock anchor, with no PATCH
    polling from here required. The two mechanisms can run independently:
    a phase using pattern='ramp' for one protocol is unaffected by this
    function, since _phase_to_gen_configs simply forwards ramp_start_rate/
    ramp_end_rate/ramp_duration/pattern through to that generator's config
    like any other field, and this function only touches RATE_FIELDS
    ("rate", "tcp_rate", "udp_rate") - not the ramp_* fields. Note, however,
    that if warmup/cooldown ramps the generic `rate` field on a generator
    that is *currently* in pattern='ramp' mode, that PATCH has no visible
    effect, since the generator ignores `rate` in favor of ramp_start_rate/
    ramp_end_rate while pattern='ramp' is active; the generator's own ramp
    still runs to completion based on its phase duration.

    Non-rate fields in `configs` (payload size, mode, etc.) are left untouched here -
    callers apply those separately via the normal phase config, since only the rate
    fields need to change gradually.
    """
    targets: dict[str, dict[str, float]] = {}
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


def _translate_http2(p: dict) -> dict:
    """Translates YAML-friendly keys into the fields gen-http2 actually reads.
    `method_distribution: {GET: 70, POST: 30}` -> `method_get_pct: 70` (normalized
    so it doesn't matter if the two percentages don't sum to exactly 100). Any
    other keys (rate, payload_size, pattern, burst_*, ramp_*, concurrent_streams,
    fault_rate, ...) are forwarded unchanged - they already match the generator's
    field names 1:1."""
    cfg = {k: v for k, v in p.items() if k != "method_distribution"}
    dist = p.get("method_distribution")
    if isinstance(dist, dict) and dist:
        get_pct = float(dist.get("GET", dist.get("get", 0)) or 0)
        post_pct = float(dist.get("POST", dist.get("post", 0)) or 0)
        total = get_pct + post_pct
        cfg["method_get_pct"] = round((get_pct / total) * 100) if total > 0 else 100
    return cfg


def _translate_mqtt(p: dict) -> dict:
    """Translates YAML-friendly keys into the fields gen-mqtt actually reads.
    `topics: [a, b, c]` -> `topic_count: 3` - the generator only supports
    rotating through a fixed-size topic set (named sensors/..., actuators/...,
    status/... up to len(BASE_TOPICS), then generic load/topic-N beyond that),
    not arbitrary topic *names*, so the YAML topic list is used purely as a
    convenient way to say how many topics should be in rotation. Any other
    keys (rate, payload_size, qos, qos_distribution, pattern, burst_*, ramp_*,
    fault_rate, ...) are forwarded unchanged."""
    cfg = {k: v for k, v in p.items() if k != "topics"}
    topics = p.get("topics")
    if isinstance(topics, list) and topics:
        cfg["topic_count"] = len(topics)
    return cfg


def _phase_to_gen_configs(phase: dict) -> dict[str, dict]:
    """Convert a phase's 'protocols' block into per-generator configs."""
    p = phase.get("protocols", {})
    return {
        "gen-http2":  _translate_http2(p.get("http2", {})),
        "gen-quic":   {k: v for k, v in p.get("quic",  {}).items()},
        "gen-mqtt":   _translate_mqtt(p.get("mqtt", {})),
        "gen-tcpudp": {
            "tcp_rate":    p.get("tcp", {}).get("rate", 0),
            "udp_rate":    p.get("udp", {}).get("rate", 0),
            "packet_size": p.get("tcp", {}).get("packet_size", 512),
            # Forward the rest of the tcpudp block as-is: mode, mean_interval,
            # min_size, max_size, tcp_ratio, pattern, burst_size, burst_interval,
            # ramp_start_rate, ramp_end_rate, ramp_duration.
            **p.get("tcpudp", {}),
        },
    }


# ── Adaptive Control (autonomous rate scaling) ──────────────────────────────────

async def _get_current_rates() -> dict[str, dict[str, float]]:
    """Read each generator's current rate value(s) to use as the adaptive baseline."""
    rates: dict[str, dict[str, float]] = {}
    for name, url in GENERATORS.items():
        status = await _call("get", f"{url}/status")
        rates[name] = {}
        for field in RATE_FIELDS[name]:
            value = status.get(field, 1) or 1
            rates[name][field] = float(value)
    return rates


async def _adaptive_loop(adaptive_cfg: dict):
    """
    Autonomous control loop.

    Every `check_interval` seconds, computes the error rate (errors / packets sent
    since the last check) for each generator. If the error rate is at or below
    `scale_up_threshold.error_rate_max`, the generator's rate is scaled up by
    `scale_up_factor`. If it is at or above `scale_down_threshold.error_rate_min`,
    the rate is scaled down by `scale_down_factor`. Otherwise the rate is held.

    `scale_up_threshold.latency_max_ms` / `scale_down_threshold.latency_min_ms` are
    also honoured for generators that report a `latency_ms` stat (currently
    gen-http2 and gen-tcpudp).

    Note: this loop scales the generic rate field(s) in RATE_FIELDS (rate /
    tcp_rate / udp_rate). If a generator is currently running pattern='ramp',
    it computes its own effective rate from ramp_start_rate/ramp_end_rate
    instead of the generic rate field, so a PATCH applied here has no visible
    effect on that generator until its phase's pattern changes away from
    'ramp'. This mirrors the same pre-existing interaction with the global
    warmup/cooldown ramp (see _ramp_rates).
    """
    check_interval = adaptive_cfg.get("check_interval", 10)
    up_cfg   = adaptive_cfg.get("scale_up_threshold", {})
    down_cfg = adaptive_cfg.get("scale_down_threshold", {})
    up_factor   = adaptive_cfg.get("scale_up_factor", 1.1)
    down_factor = adaptive_cfg.get("scale_down_factor", 0.8)
    max_multiplier = adaptive_cfg.get("max_multiplier", 5.0)
    min_multiplier = adaptive_cfg.get("min_multiplier", 0.2)

    error_rate_max = up_cfg.get("error_rate_max", 0.0)
    latency_max_ms = up_cfg.get("latency_max_ms")
    error_rate_min = down_cfg.get("error_rate_min", 1.0)
    latency_min_ms = down_cfg.get("latency_min_ms")

    baseline = await _get_current_rates()
    multipliers = {name: 1.0 for name in baseline}
    prev_totals: dict[str, dict[str, int]] = {}

    _log(
        f"Adaptive Control started "
        f"(check every {check_interval}s, "
        f"scale up if error_rate<={error_rate_max:.0%}, "
        f"scale down if error_rate>={error_rate_min:.0%})",
        "adaptive",
    )

    try:
        while True:
            await asyncio.sleep(check_interval)

            metrics = await _call("get", f"{METRICS_URL}/metrics")
            gens = metrics.get("generators", {})

            for name, base_rates in baseline.items():
                g = gens.get(name, {})
                packets = g.get("packets_sent", 0)
                errors  = g.get("errors", 0)
                latency = g.get("latency_ms")

                prev = prev_totals.get(name, {"packets": 0, "errors": 0})
                d_packets = max(0, packets - prev["packets"])
                d_errors  = max(0, errors - prev["errors"])
                error_rate = (d_errors / d_packets) if d_packets > 0 else 0.0
                prev_totals[name] = {"packets": packets, "errors": errors}

                action = "hold"
                if d_packets == 0:
                    action = "hold"
                elif error_rate >= error_rate_min or (
                    latency_min_ms is not None and latency is not None and latency >= latency_min_ms
                ):
                    action = "down"
                elif error_rate <= error_rate_max and (
                    latency_max_ms is None or latency is None or latency <= latency_max_ms
                ):
                    action = "up"

                if action == "up":
                    multipliers[name] = min(max_multiplier, multipliers[name] * up_factor)
                elif action == "down":
                    multipliers[name] = max(min_multiplier, multipliers[name] * down_factor)

                applied = {k: max(1, round(v * multipliers[name])) for k, v in base_rates.items()}

                state["adaptive_status"][name] = {
                    "multiplier":  round(multipliers[name], 3),
                    "error_rate":  round(error_rate, 4),
                    "action":      action,
                    "applied":     applied,
                    "checked_at":  datetime.now(LOG_TZ).strftime("%H:%M:%S"),
                }

                if action in ("up", "down"):
                    await _call("patch", f"{GENERATORS[name]}/config", json=applied)
                    arrow = "↑" if action == "up" else "↓"
                    _log(
                        f"ADAPTIVE {arrow} {name}: error_rate={error_rate:.1%} "
                        f"→ ×{multipliers[name]:.2f} → {applied}",
                        "adaptive",
                    )
    except asyncio.CancelledError:
        _log("Adaptive Control stopped", "adaptive")
        raise


def _start_adaptive(adaptive_cfg: dict):
    if state["adaptive_task"]:
        state["adaptive_task"].cancel()
    state["adaptive_enabled"] = True
    state["adaptive_status"] = {}
    loop = asyncio.get_event_loop()
    state["adaptive_task"] = loop.create_task(_adaptive_loop(adaptive_cfg))


def _stop_adaptive():
    if state["adaptive_task"]:
        state["adaptive_task"].cancel()
        state["adaptive_task"] = None
    state["adaptive_enabled"] = False


# ── Phase runner (background task) ────────────────────────────────────────────

async def _run_phases(profile_data: dict):
    global_cfg = profile_data.get("global", {})
    warmup     = global_cfg.get("warmup", 0)
    cooldown   = global_cfg.get("cooldown", 0)
    phases     = profile_data.get("phases", [])

    # Warmup: linearly ramp phase 1's rates from 0 up to their target values.
    # /start already started the generators at rate=0 (see /start), so this is
    # the rate's first real ramp-up - both the configured "warmup period" and
    # the "ramping (linear increase)" sending pattern in one mechanism.
    if phases and warmup > 0 and state["running"]:
        state["active_phase"] = "warmup"
        _log(f"Warmup: ramping rates up over {warmup}s", "info")
        await _ramp_rates(_phase_to_gen_configs(phases[0]), warmup, "up", "warmup")

    for phase in phases:
        if not state["running"]:
            break
        state["active_phase"] = phase.get("name", "?")
        _log(f"Phase → {phase['name']} ({phase.get('duration', '?')}s)")

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

    # Cooldown: linearly ramp the last phase's rates back down to 0 before stopping.
    if phases and cooldown > 0 and state["running"]:
        state["active_phase"] = "cooldown"
        _log(f"Cooldown: ramping rates down over {cooldown}s", "info")
        await _ramp_rates(_phase_to_gen_configs(phases[-1]), cooldown, "down", "cooldown")

    state["running"] = False
    state["active_phase"] = None
    _log("All phases completed", "success")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post(
    "/start",
    response_model=StartStopResponse,
    tags=["Control"],
    summary="Start traffic generation",
    description="Loads the given YAML profile, applies phase 1 to every generator, "
                 "starts them, and launches the background phase runner (and Adaptive "
                 "Control, if the phase defines it).",
)
async def start(profile: str = Query("balanced", description="Name of the YAML profile in config/ to run")):
    if state["running"]:
        return {"ok": True, "note": "already running"}

    try:
        profile_data = _load_yaml(profile)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    state["running"]        = True
    state["active_profile"] = profile

    # Send first phase config to all generators, then start them
    phases = profile_data.get("phases", [])
    if phases:
        configs = _phase_to_gen_configs(phases[0])
        warmup = profile_data.get("global", {}).get("warmup", 0)
        start_configs = configs
        if warmup > 0:
            # Start at rate 0; _run_phases() ramps rates up to their configured
            # targets over `warmup` seconds (see _ramp_rates).
            start_configs = {
                name: {**cfg, **{k: 0 for k in RATE_FIELDS.get(name, []) if k in cfg}}
                for name, cfg in configs.items()
            }
        for name, cfg in start_configs.items():
            if cfg:
                await _call("post", f"{GENERATORS[name]}/start", json=cfg)
    else:
        for url in GENERATORS.values():
            await _call("post", f"{url}/start", json={})

    # Start phase runner in background
    loop = asyncio.get_event_loop()
    state["phase_task"] = loop.create_task(_run_phases(profile_data))

    _log(f"Started with profile '{profile}'", "success")
    return {"ok": True, "profile": profile}


@app.post(
    "/stop",
    response_model=OkResponse,
    tags=["Control"],
    summary="Stop traffic generation",
    description="Stops the phase runner, disables Adaptive Control, and tells every "
                 "generator to stop sending traffic.",
)
async def stop():
    state["running"] = False
    if state["phase_task"]:
        state["phase_task"].cancel()
        state["phase_task"] = None
    _stop_adaptive()

    for name, url in GENERATORS.items():
        result = await _call("post", f"{url}/stop")
        _log(f"Stopped {name}: {result.get('ok', result.get('error', '?'))}")

    state["active_phase"] = None
    _log("System stopped", "info")
    return {"ok": True}


@app.post(
    "/config/load",
    response_model=StartStopResponse,
    tags=["Configuration"],
    summary="Load a YAML traffic profile",
    description="Sets the active profile. If the system is currently running, phase 1 "
                 "of the new profile is applied to all generators immediately.",
)
async def load_config(body: ConfigLoadRequest = Body(...)):
    profile = body.profile
    try:
        profile_data = _load_yaml(profile)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    state["active_profile"] = profile
    _log(f"Loaded profile '{profile}'")

    if state["running"]:
        phases = profile_data.get("phases", [])
        if phases:
            configs = _phase_to_gen_configs(phases[0])
            for name, cfg in configs.items():
                if cfg:
                    await _call("patch", f"{GENERATORS[name]}/config", json=cfg)

    return {"ok": True, "profile": profile}


@app.patch(
    "/generator/{name}",
    response_model=OkResponse,
    tags=["Configuration"],
    summary="Patch a single generator's configuration",
    description="Forwards arbitrary key/value overrides to one generator's `/config` "
                 "endpoint, e.g. `{\"rate\": 80}`. Useful for live demo tweaks during the presentation.",
)
async def patch_generator(
    name: str,
    body: GeneratorPatch = Body(..., description="Fields to override, forwarded as-is"),
):
    if name not in GENERATORS:
        raise HTTPException(404, f"Unknown generator '{name}'. Valid: {list(GENERATORS)}")

    url = GENERATORS[name]
    result = await _call("patch", f"{url}/config", json=body.model_dump(exclude_none=True))
    _log(f"Patched {name}: {body.model_dump(exclude_none=True)}")
    return {"ok": True, "note": str(result)}


@app.post(
    "/generator/{name}/start",
    response_model=OkResponse,
    tags=["Configuration"],
    summary="Start a single generator",
    description="Forwards to the generator's own `/start` endpoint. Used by the "
                 "dashboard's per-protocol on/off switches; does not affect the others.",
)
async def start_generator(name: str):
    if name not in GENERATORS:
        raise HTTPException(404, f"Unknown generator '{name}'. Valid: {list(GENERATORS)}")
    result = await _call("post", f"{GENERATORS[name]}/start", json={})
    _log(f"Started {name}")
    return {"ok": True, "note": str(result)}


@app.post(
    "/generator/{name}/stop",
    response_model=OkResponse,
    tags=["Configuration"],
    summary="Stop a single generator",
    description="Forwards to the generator's own `/stop` endpoint. Used by the "
                 "dashboard's per-protocol on/off switches; does not affect the others.",
)
async def stop_generator(name: str):
    if name not in GENERATORS:
        raise HTTPException(404, f"Unknown generator '{name}'. Valid: {list(GENERATORS)}")
    result = await _call("post", f"{GENERATORS[name]}/stop")
    _log(f"Stopped {name}")
    return {"ok": True, "note": str(result)}


@app.get(
    "/status",
    response_model=StatusResponse,
    tags=["Monitoring"],
    summary="Get full system status",
    description="Returns whether the system is running, the active profile/phase, "
                 "Adaptive Control state, live per-generator status, aggregated metrics, "
                 "and the most recent log entries.",
)
async def status():
    gen_statuses = {}
    for name, url in GENERATORS.items():
        gen_statuses[name] = await _call("get", f"{url}/status")

    metrics  = await _call("get", f"{METRICS_URL}/metrics")
    analysis = await _call("get", f"{METRICS_URL}/analysis")

    return {
        "running":          state["running"],
        "active_profile":   state["active_profile"],
        "active_phase":     state["active_phase"],
        "adaptive_enabled": state["adaptive_enabled"],
        "adaptive_status":  state["adaptive_status"],
        "ramp_status":      state["ramp_status"],
        "generators":       gen_statuses,
        "metrics":          metrics,
        "analysis":         analysis if "error" not in analysis else {},
        "log":              log_entries[-50:],
    }


@app.get(
    "/profiles",
    response_model=ProfilesResponse,
    tags=["Configuration"],
    summary="List available YAML profiles",
)
async def list_profiles():
    files = glob.glob(os.path.join(CONFIG_DIR, "*.yaml"))
    names = [os.path.splitext(os.path.basename(f))[0] for f in files
             if "mosquitto" not in f]
    return {"profiles": sorted(names)}


@app.get(
    "/log",
    response_model=LogResponse,
    tags=["Monitoring"],
    summary="Get the full configuration / adaptive-control log",
)
async def get_log():
    return {"log": log_entries}


@app.get(
    "/adaptive/status",
    response_model=AdaptiveStatusResponse,
    tags=["Adaptive Control"],
    summary="Get the current Adaptive Control state",
    description="Shows whether Adaptive Control is active and, per generator, the most "
                 "recent autonomous scaling decision (error rate observed, action taken, "
                 "and the rate value(s) that were applied).",
)
async def adaptive_status():
    return {
        "enabled":    state["adaptive_enabled"],
        "generators": state["adaptive_status"],
    }


@app.post(
    "/adaptive/toggle",
    response_model=AdaptiveToggleResponse,
    tags=["Adaptive Control"],
    summary="Manually enable or disable Adaptive Control",
    description="Lets you switch Adaptive Control on or off independently of the active "
                 "profile's phase definitions; handy for demonstrating the feature live. "
                 "When enabled without a phase-specific config, demo-tuned defaults are used "
                 "(check every 5s, scale up at <=0% errors and <=50ms latency, scale down at "
                 ">=5% errors or >=150ms latency, x1.2 up / x0.5 down), fast and dramatic "
                 "enough to react visibly to the dashboard's Fault Injection sliders "
                 "(fault_rate / extra_latency_ms) within 5-10 seconds.",
)
async def adaptive_toggle(enabled: bool = Query(..., description="true to enable, false to disable")):
    if enabled and not state["adaptive_enabled"]:
        _start_adaptive({
            "check_interval": 5,
            "scale_up_threshold":   {"error_rate_max": 0.0, "latency_max_ms": 50},
            "scale_down_threshold": {"error_rate_min": 0.05, "latency_min_ms": 150},
            "scale_up_factor":   1.2,
            "scale_down_factor": 0.5,
            "min_multiplier":    0.1,
            "max_multiplier":    5.0,
        })
        _log("Adaptive Control manually enabled", "adaptive")
    elif not enabled and state["adaptive_enabled"]:
        _stop_adaptive()
        _log("Adaptive Control manually disabled", "adaptive")

    return {"ok": True, "enabled": state["adaptive_enabled"]}


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Monitoring"],
    summary="Liveness check",
)
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
