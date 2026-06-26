"""
Metrics Collector
-----------------
Receives stats from all generators via POST /update.
Exposes aggregated stats via GET /metrics.
"""

from flask import Flask, request, jsonify
import threading, time

app = Flask(__name__)
lock = threading.Lock()

store = {
    "generators": {},   # name → latest stat snapshot
    "analyzers":  {},   # name → latest network-analyzer snapshot (totals + io_graph)
    "start_time": time.time(),
}


# ── Receive stats from a generator ────────────────────────────────────────────

@app.route("/update", methods=["POST"])
def update():
    payload = request.get_json(silent=True)
    if not payload or "generator" not in payload:
        return jsonify({"error": "missing 'generator' field"}), 400

    name = payload["generator"]
    with lock:
        store["generators"][name] = {**payload, "_ts": time.time()}

    return jsonify({"ok": True})


# ── Aggregated metrics endpoint ────────────────────────────────────────────────

@app.route("/metrics", methods=["GET"])
def metrics():
    with lock:
        gens = dict(store["generators"])

    total_packets = sum(g.get("packets_sent", 0) for g in gens.values())
    total_bytes   = sum(g.get("bytes_sent",   0) for g in gens.values())
    total_errors  = sum(g.get("errors",        0) for g in gens.values())

    # bytes per second over the last 5 seconds (rough estimate)
    rate_bps = sum(g.get("rate_bps", 0) for g in gens.values())

    return jsonify({
        "uptime_seconds": int(time.time() - store["start_time"]),
        "total_packets":  total_packets,
        "total_bytes":    total_bytes,
        "total_errors":   total_errors,
        "rate_bps":       rate_bps,
        "generators":     gens,
    })


# ── Receive a snapshot from a network-analyzer sidecar ─────────────────────────

@app.route("/analysis/update", methods=["POST"])
def analysis_update():
    payload = request.get_json(silent=True)
    if not payload or "analyzer" not in payload:
        return jsonify({"error": "missing 'analyzer' field"}), 400

    name = payload["analyzer"]
    with lock:
        store["analyzers"][name] = {**payload, "_ts": time.time()}

    return jsonify({"ok": True})


# ── Aggregated network-analysis endpoint ────────────────────────────────────────
# Each analyzer sidecar only sees the traffic crossing its own target container's
# network namespace, so simply summing across all of them reconstructs the full
# picture (Wireshark's "Protocol Hierarchy" + "I/O Graph", but live and continuous).

@app.route("/analysis", methods=["GET"])
def analysis():
    with lock:
        analyzers = dict(store["analyzers"])

    protocols = {}
    buckets = {}
    size_hist = {}
    gap_hist = {}
    failure_events = []
    status = {}

    for name, snap in analyzers.items():
        status[name] = {
            "iface":       snap.get("iface"),
            "age_seconds": round(time.time() - snap.get("_ts", time.time()), 1),
        }

        for proto, vals in snap.get("totals", {}).items():
            agg = protocols.setdefault(proto, {"packets": 0, "bytes": 0})
            agg["packets"] += vals.get("packets", 0)
            agg["bytes"]   += vals.get("bytes", 0)

        for point in snap.get("io_graph", []):
            sec = point.get("ts")
            if sec is None:
                continue
            bucket = buckets.setdefault(sec, {})
            for proto, vals in point.items():
                if proto == "ts":
                    continue
                agg = bucket.setdefault(proto, {"packets": 0, "bytes": 0})
                agg["packets"] += vals.get("packets", 0)
                agg["bytes"]   += vals.get("bytes", 0)

        # size_hist/gap_hist: each protocol is only ever observed by the one
        # analyzer sharing its target's namespace, so summing across analyzers
        # is equivalent to a passthrough - but stays correct even if that
        # assumption ever changes (e.g. a future analyzer seeing multiple ports).
        for proto, label_counts in snap.get("size_hist", {}).items():
            agg = size_hist.setdefault(proto, {})
            for label, count in label_counts.items():
                agg[label] = agg.get(label, 0) + count

        for proto, label_counts in snap.get("gap_hist", {}).items():
            agg = gap_hist.setdefault(proto, {})
            for label, count in label_counts.items():
                agg[label] = agg.get(label, 0) + count

        failure_events.extend(snap.get("failure_events", []))

    io_graph = [{"ts": sec, **buckets[sec]} for sec in sorted(buckets)[-120:]]
    failure_events.sort(key=lambda e: e.get("ts", 0))
    failure_events = failure_events[-100:]

    return jsonify({
        "protocols":      protocols,
        "io_graph":       io_graph,
        "size_hist":      size_hist,
        "gap_hist":       gap_hist,
        "failure_events": failure_events,
        "analyzers":      status,
    })


# ── Reset counters ─────────────────────────────────────────────────────────────

@app.route("/reset", methods=["POST"])
def reset():
    with lock:
        for name in store["generators"]:
            store["generators"][name]["packets_sent"] = 0
            store["generators"][name]["bytes_sent"]   = 0
            store["generators"][name]["errors"]        = 0
        store["start_time"] = time.time()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9090, debug=False)
