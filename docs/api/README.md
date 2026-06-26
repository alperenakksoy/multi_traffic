# API Reference (Swagger / OpenAPI)

This folder contains the exported OpenAPI 3 specifications for every REST API in the
system, plus a self-contained Swagger UI page for presenting them.

## Quick view (no server needed)

Open **`swagger.html`** directly in a browser. Use the dropdown in the header to
switch between the five APIs:

| Service | Port | Purpose |
|---|---|---|
| `controller` | 8000 | Central orchestration API: start/stop, profile/phase management, Adaptive Control |
| `gen-http2` | 7001 | HTTP/2 generator control API |
| `gen-quic` | 7002 | QUIC/HTTP3 generator control API |
| `gen-mqtt` | 7003 | MQTT generator control API |
| `gen-tcpudp` | 7004 | TCP/UDP generator control API |

The page embeds all specs inline, so it works offline, which is perfect for the live
demo or for screenshots in the report.

## Live docs (while the system is running)

Every FastAPI service also serves interactive Swagger UI and the raw spec itself:

```
http://localhost:8000/docs        # Controller: Swagger UI
http://localhost:8000/openapi.json
```

Generator APIs are only reachable from inside the Docker network (`gen-http2:7001`
etc.), but you can `docker exec` into a container or temporarily map a port to view
them the same way.

## Regenerating the specs

The specs in this folder are static exports (generated via `app.openapi()` for each
FastAPI app). If you change any endpoint signature, model, or docstring in
`controller/main.py` or `generators/*/generator.py`, re-export with:

```bash
pip install fastapi uvicorn pydantic httpx paho-mqtt numpy pyyaml requests
python3 export_openapi.py   # see snippet below, or regenerate with the same approach
```

The export simply imports each module, calls `app.openapi()`, and dumps the result
to `openapi_<service>.json`. `swagger.html` then embeds these five JSON files
directly so the page works without a backend.

## Why no authentication?

The controller and generator APIs are intentionally **unauthenticated**. They are
only reachable inside the isolated `mic-net` Docker network (lab/educational
deployment), and adding a Bearer token / API key would add complexity without a
corresponding security benefit in this context. This trade-off is documented in the
technical report under "Implementation Decisions and Trade-offs".
