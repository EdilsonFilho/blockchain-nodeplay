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
    GET  /join-info    URL participants should open + Wi-Fi credentials
    GET  /qr.svg       QR code for that URL
    GET  /wifi-qr.svg  "join Wi-Fi" QR code (when WIFI_SSID is configured)
    WS   /ws/node      participant channel  (?sid=<browser id>&name=<nickname>, nickname required)
    WS   /ws/dashboard dashboard channel    (?token=<ADMIN_TOKEN>): start / stop / reset
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from nodeplay import config
from nodeplay.engine import NICKNAME_MAX, NICKNAME_MIN, ActionError, Engine, clean_text
from nodeplay.network import lan_ip, qr_svg, wifi_qr_payload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nodeplay")

STATIC_DIR = Path(__file__).parent / "static"

engine = Engine()


def resolve_server_ip() -> str:
    """HOST_IP from .env if set, otherwise the LAN address detected right now."""
    return config.HOST_IP or lan_ip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture the LAN IP once, when the service comes up. Restart the server
    # after switching networks (or set HOST_IP / PUBLIC_URL in .env).
    app.state.server_ip = resolve_server_ip()
    source = "from HOST_IP" if config.HOST_IP else "auto-detected"
    log.info("Server IP %s (%s)", app.state.server_ip, source)
    if config.WIFI_SSID:
        log.info("Participants' Wi-Fi: %s", config.WIFI_SSID)
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
    """
    The address phones should open: PUBLIC_URL if configured, otherwise the IP
    captured at startup plus the port this request came in on (so it works
    even when the projector opened the dashboard via localhost).
    """
    if config.PUBLIC_URL:
        return config.PUBLIC_URL + "/"
    host = getattr(request.app.state, "server_ip", "") or resolve_server_ip()
    port = request.url.port
    default_port = 443 if request.url.scheme == "https" else 80
    return f"{request.url.scheme}://{host}{f':{port}' if port and port != default_port else ''}/"


@app.get("/join-info")
async def join_info(request: Request) -> dict:
    wifi = None
    if config.WIFI_SSID:
        wifi = {
            "ssid": config.WIFI_SSID,
            "password": config.WIFI_PASSWORD,
            "security": config.WIFI_SECURITY,
        }
    return {"url": join_url(request), "wifi": wifi}


@app.get("/qr.svg", include_in_schema=False)
async def qr_code(request: Request) -> Response:
    return Response(qr_svg(join_url(request)), media_type="image/svg+xml")


@app.get("/wifi-qr.svg", include_in_schema=False)
async def wifi_qr_code() -> Response:
    if not config.WIFI_SSID:
        raise HTTPException(status_code=404, detail="WIFI_SSID is not configured")
    payload = wifi_qr_payload(config.WIFI_SSID, config.WIFI_PASSWORD, config.WIFI_SECURITY)
    return Response(qr_svg(payload), media_type="image/svg+xml")


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
    # A nickname is mandatory: refuse the join (code 4002) so the client
    # goes back to the join screen instead of retrying.
    name = clean_text(ws.query_params.get("name", ""), NICKNAME_MAX)
    if len(name) < NICKNAME_MIN:
        await ws.close(code=4002, reason="Nickname required")
        return
    node = await engine.connect_node(ws, ws.query_params.get("sid", ""), name)
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
                elif kind == "leave":
                    await engine.leave(node)
                    await ws.close(code=4001, reason="Left the network")
                    return
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
                if kind in {"start", "stop", "reset"} and not is_admin:
                    raise ActionError("Read-only dashboard: open it with ?token=<ADMIN_TOKEN>.")
                if kind == "start":
                    await engine.start()
                elif kind == "stop":
                    await engine.stop()
                elif kind == "reset":
                    await engine.reset()
            except ActionError as exc:
                await ws.send_json({"type": "notice", "level": "warn", "text": str(exc)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        engine.disconnect_dashboard(ws)


if __name__ == "__main__":
    import uvicorn

    ip = resolve_server_ip()
    base = config.PUBLIC_URL or f"http://{ip}:{config.PORT}"
    print(f"\n  Projector dashboard : {base}/dashboard")
    print(f"  Participants (QR)   : {base}/")
    if config.WIFI_SSID:
        print(f"  Wi-Fi network       : {config.WIFI_SSID}  (password: {config.WIFI_PASSWORD or '—'})")
    print()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
