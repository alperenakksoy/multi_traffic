"""
HTTP/2 Target Server
---------------------
Receives HTTP/2 requests from gen-http2.
Served by Hypercorn which supports HTTP/2 natively.
"""

import os, time, threading
import requests as req_sync
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

METRICS_URL = os.getenv("METRICS_URL", "http://metrics:9090")

counter = {"requests": 0, "bytes_in": 0}
_lock   = threading.Lock()


@app.get("/")
async def index():
    with _lock:
        counter["requests"] += 1
    return JSONResponse({"status": "ok", "server": "HTTP/2 target"})


@app.post("/data")
async def data(request: Request):
    body = await request.body()
    with _lock:
        counter["requests"] += 1
        counter["bytes_in"] += len(body)
    return JSONResponse({"received": len(body)})


@app.get("/health")
async def health():
    return {"ok": True}


# Catch-all routes so gen-http2 can be configured with arbitrary extra
# get_paths / post_paths (e.g. "/api/items") without producing 404s. The
# specific routes above (/, /data, /health) still take precedence.
@app.get("/{path:path}")
async def catch_all_get(path: str):
    with _lock:
        counter["requests"] += 1
    return JSONResponse({"status": "ok", "server": "HTTP/2 target", "path": f"/{path}"})


@app.post("/{path:path}")
async def catch_all_post(path: str, request: Request):
    body = await request.body()
    with _lock:
        counter["requests"] += 1
        counter["bytes_in"] += len(body)
    return JSONResponse({"received": len(body), "path": f"/{path}"})


def _report():
    while True:
        time.sleep(10)
        with _lock:
            payload = {
                "generator":    "target-http2",
                "running":      True,
                "packets_sent": counter["requests"],
                "bytes_sent":   counter["bytes_in"],
                "errors":       0,
            }
        try:
            req_sync.post(f"{METRICS_URL}/update", json=payload, timeout=2)
        except Exception:
            pass


threading.Thread(target=_report, daemon=True).start()
