"""
Rehearse the activity without 30 people: spawn bot nodes that join the
network, propose transactions and vote (sometimes staying silent, so you can
watch the slashing rule in action).

Usage (with the server already running):

    python tools/simulate.py                       # 30 bots on localhost:8000
    python tools/simulate.py --nodes 12 --silence 0.3 --url ws://192.168.0.10:8000

Then open /dashboard and press START.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import uuid

import websockets  # installed with uvicorn[standard]

WORDS = ["thesis", "dataset", "article", "photo", "map", "record", "archive", "letter", "video", "report"]


def random_ark() -> str:
    if random.random() < 0.5:
        return random.choice(WORDS)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"ark:/{random.randint(10000, 99999)}/{suffix}"


async def bot(index: int, args: argparse.Namespace) -> None:
    sid = f"bot-{index}-{uuid.uuid4().hex[:6]}"
    url = f"{args.url}/ws/node?sid={sid}&name=Bot-{index:02d}"
    # Each bot has a personality: how often it stays silent / approves.
    silence = min(1.0, max(0.0, random.gauss(args.silence, 0.15)))
    async with websockets.connect(url) as ws:
        voted_rounds: set[int] = set()
        latest: dict = {}
        writer = asyncio.create_task(writer_loop(ws, latest))
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
        if you.get("role") == "WRITER" and latest.get("accepting") and not you.get("wait_turn"):
            try:
                await ws.send(json.dumps({"type": "propose", "tx": random_ark()}))
            except websockets.ConnectionClosed:
                return


async def delayed_vote(ws, round_id: int, approve_rate: float, timeout: float) -> None:
    await asyncio.sleep(random.uniform(0.3, 0.7 * timeout))  # humans take a moment
    approve = random.random() < approve_rate
    try:
        await ws.send(json.dumps({"type": "vote", "round": round_id, "approve": approve}))
    except websockets.ConnectionClosed:
        pass


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:8000", help="server base URL (ws://host:port)")
    parser.add_argument("--nodes", type=int, default=30, help="number of bot nodes")
    parser.add_argument("--silence", type=float, default=0.2, help="average chance a validator skips a vote")
    parser.add_argument("--approve", type=float, default=0.75, help="chance a cast vote is an approval")
    args = parser.parse_args()

    tasks = []
    for i in range(1, args.nodes + 1):
        tasks.append(asyncio.create_task(bot(i, args)))
        await asyncio.sleep(0.05)  # join in order, like people scanning a QR code
    print(f"{args.nodes} bots connected to {args.url} — press START on the dashboard. Ctrl+C to stop.")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
