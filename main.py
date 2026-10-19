"""
NodePlay — entry point.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000
or simply:
    python main.py            (same thing, and prints the LAN URLs)

Routes
------
    GET  /             participant client (phones)
    GET  /dashboard    projector dashboard (START / RESET controls)
    GET  /join-info    URL participants should open
    GET  /qr.svg       QR code for that URL
    WS   /ws/node      participant channel  (?sid=<browser id>&name=<nickname>)
    WS   /ws/dashboard dashboard channel    (?token=<ADMIN_TOKEN>)
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from nodeplay import config
from nodeplay.engine import ActionError, Engine
from nodeplay.network import is_loopback, lan_ip, qr_svg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nodeplay")

STATIC_DIR = Path(__file__).parent / "static"

engine = Engine()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The genesis block is sealed when the Engine is created (at import time).
    log.info("Genesis block %s sealed — open /dashboard on the projector", engine.ledger.last.hash[:8])
    yield
    await engine.reset(broadcast=False)  # cancels background timers


app = FastAPI(title="NodePlay", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- HTTP pages

@app.get("/", include_in_schema=False)
async def client_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


def join_url(request: Request) -> str:
    """The address phones should open (works even if the projector uses localhost)."""
    if config.PUBLIC_URL:
        return config.PUBLIC_URL + "/"
    host = request.url.hostname or "localhost"
    if is_loopback(host):
        host = lan_ip()
    port = request.url.port
    default_port = 443 if request.url.scheme == "https" else 80
    return f"{request.url.scheme}://{host}{f':{port}' if port and port != default_port else ''}/"


@app.get("/join-info")
async def join_info(request: Request) -> dict:
    return {"url": join_url(request)}


@app.get("/qr.svg", include_in_schema=False)
async def qr_code(request: Request) -> Response:
    return Response(qr_svg(join_url(request)), media_type="image/svg+xml")


# ---------------------------------------------------------------- WebSockets

async def incoming(ws: WebSocket) -> AsyncIterator[dict]:
    """Yield JSON objects sent by a client, silently skipping malformed frames."""
    while True:
        try:
            msg = await ws.receive_json()
        except (KeyError, ValueError):  # binary frame or invalid JSON
            continue
        if isinstance(msg, dict):
            yield msg


@app.websocket("/ws/node")
async def node_socket(ws: WebSocket) -> None:
    await ws.accept()
    node = await engine.connect_node(ws, ws.query_params.get("sid", ""), ws.query_params.get("name", ""))
    try:
        async for msg in incoming(ws):
            kind = msg.get("type")
            try:
                if kind == "propose":
                    await engine.propose(node, msg.get("tx"))
                elif kind == "vote":
                    await engine.vote(node, msg.get("round"), msg.get("approve"))
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
            except ActionError as exc:
                await ws.send_json({"type": "notice", "level": "warn", "text": str(exc)})
    except (WebSocketDisconnect, RuntimeError):
        pass  # client closed the connection
    finally:
        await engine.disconnect_node(node, ws)


@app.websocket("/ws/dashboard")
async def dashboard_socket(ws: WebSocket) -> None:
    await ws.accept()
    token = ws.query_params.get("token", "")
    is_admin = not config.ADMIN_TOKEN or hmac.compare_digest(token, config.ADMIN_TOKEN)
    await engine.connect_dashboard(ws, is_admin)
    try:
        async for msg in incoming(ws):
            kind = msg.get("type")
            try:
                if kind in {"start", "reset"} and not is_admin:
                    raise ActionError("Read-only dashboard: open it with ?token=<ADMIN_TOKEN>.")
                if kind == "start":
                    await engine.start()
                elif kind == "reset":
                    await engine.reset()
            except ActionError as exc:
                await ws.send_json({"type": "notice", "level": "warn", "text": str(exc)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        engine.disconnect_dashboard(ws)


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    ip = lan_ip()
    print(f"\n  Projector dashboard : http://{ip}:{port}/dashboard")
    print(f"  Participants (QR)   : http://{ip}:{port}/\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
