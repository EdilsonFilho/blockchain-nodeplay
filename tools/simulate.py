"""
Rehearse the activity without 30 people: spawn bot nodes that join the
network, propose transactions and vote (sometimes staying silent, so you can
watch the slashing rule in action).

The server must already be running in another terminal:

    terminal 1:  python main.py
    terminal 2:  python tools/simulate.py                 # 30 bots, port from .env
                 python tools/simulate.py --nodes 12 --silence 0.3 --url ws://192.168.0.10:8000

Then open /dashboard and press START.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import websockets  # installed with uvicorn[standard]
from dotenv import load_dotenv

# Use the same PORT as the server (from .env) unless --url is given.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DEFAULT_URL = f"ws://127.0.0.1:{os.environ.get('PORT', '8000')}"

WORDS = ["thesis", "dataset", "article", "photo", "map", "record", "archive", "letter", "video", "report"]


def random_record() -> str:
    """A random word or record identifier, e.g. "dataset" or "REC-4821"."""
    if random.random() < 0.5:
        return random.choice(WORDS)
    return f"REC-{random.randint(1000, 9999)}"


async def bot(index: int, args: argparse.Namespace, connected: asyncio.Event) -> None:
    sid = f"bot-{index}-{uuid.uuid4().hex[:6]}"
    url = f"{args.url}/ws/node?sid={sid}&name=Bot-{index:02d}"
    # Each bot has a personality: how often it stays silent / approves.
    silence = min(1.0, max(0.0, random.gauss(args.silence, 0.15)))
    async with websockets.connect(url) as ws:
        connected.set()
        voted_rounds: set[int] = set()
        latest: dict = {}
        writer = asyncio.create_task(writer_loop(ws, latest))  # noqa: F841 (kept alive)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") != "state":
                continue
            latest.update(msg)
            you, rnd = msg["you"], msg["round"]

            if (
                you["role"] == "VALIDATOR"
                and rnd and rnd["status"] == "VOTING"
                and you["eligible"] and not you["vote"]
                and rnd["id"] not in voted_rounds
            ):
                voted_rounds.add(rnd["id"])
                if random.random() >= silence:
                    asyncio.create_task(delayed_vote(ws, rnd["id"], args.approve, msg["vote_timeout"]))


async def writer_loop(ws, latest: dict) -> None:
    """Writers keep trying to get their records into the mempool."""
    while True:
        await asyncio.sleep(random.uniform(0.5, 3.0))
        you = latest.get("you", {})
        if you.get("role") == "WRITER" and latest.get("accepting"):
            try:
                await ws.send(json.dumps({"type": "propose", "tx": random_record()}))
            except websockets.ConnectionClosed:
                return


async def delayed_vote(ws, round_id: int, approve_rate: float, timeout: float) -> None:
    await asyncio.sleep(random.uniform(0.3, 0.7 * timeout))  # humans take a moment
    approve = random.random() < approve_rate
    try:
        await ws.send(json.dumps({"type": "vote", "round": round_id, "approve": approve}))
    except websockets.ConnectionClosed:
        pass


async def server_is_up(url: str) -> bool:
    """Quick TCP check, so we can explain the problem instead of crashing."""
    parsed = urlparse(url)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(parsed.hostname, parsed.port or 80), 3)
        writer.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"server base URL (default: {DEFAULT_URL})")
    parser.add_argument("--nodes", type=int, default=30, help="number of bot nodes")
    parser.add_argument("--silence", type=float, default=0.2, help="average chance a validator skips a vote")
    parser.add_argument("--approve", type=float, default=0.75, help="chance a cast vote is an approval")
    args = parser.parse_args()
    args.url = args.url.rstrip("/")

    if not await server_is_up(args.url):
        print(
            f"Cannot reach the NodePlay server at {args.url}.\n"
            "Start it first in another terminal (and keep it running):\n\n"
            "    python main.py\n\n"
            "If it runs on another port or machine, pass --url ws://<host>:<port>.",
            file=sys.stderr,
        )
        return 1

    events = [asyncio.Event() for _ in range(args.nodes)]
    tasks = []
    for i, ev in enumerate(events, start=1):
        tasks.append(asyncio.create_task(bot(i, args, ev)))
        await asyncio.sleep(0.05)  # join in order, like people scanning a QR code

    # Report only once the bots are really connected.
    await asyncio.wait([asyncio.create_task(ev.wait()) for ev in events], timeout=10)
    joined = sum(ev.is_set() for ev in events)
    print(f"{joined}/{args.nodes} bots connected to {args.url} — press START on the dashboard. Ctrl+C to stop.")

    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        print(f"Connection to the server lost ({type(failures[0]).__name__}: {failures[0]}). "
              "Was the server stopped?", file=sys.stderr)
        return 1
    print("The server closed the connections.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
