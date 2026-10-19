"""
The consensus engine: the single source of truth for the whole activity.

Everything runs inside one asyncio event loop, so state mutations that happen
between two `await`s are atomic. That keeps the engine free of locks except
for the one that serialises broadcasts.

Lifecycle
---------
    IDLE --[START]--> RUNNING --(GAME_DURATION elapses or [STOP])--> ENDED
      ^                                                      |
      +---------------------------[RESET]--------------------+

Roles
-----
There is no limit on participants. Each newcomer draws a random role among
the roles that still have a free seat (writers: unlimited by default;
validators: MAX_VALIDATORS in total, bots included; observers: unlimited).
Two rules keep the network playable:
  * if there is no writer, the newcomer becomes a writer;
  * if there is no validator (only possible without bots), the newcomer
    becomes one.
Optional built-in bot validators (BOT_VALIDATORS_*) always approve, so with
them a single writer is enough to see blocks being created.

Consensus round
---------------
1. A WRITER proposes a transaction (only one may sit in the mempool).
   Writers may propose as many times as they like: as soon as the mempool
   reopens, any writer (including the last proposer) can try again.
2. The set of *eligible* validators is frozen: every active, connected
   validator at that moment. The quorum is  floor(N / 2) + 1  (> 50 %).
3. Validators have VOTE_TIMEOUT seconds to approve or reject.
   The round closes early only when every eligible validator has voted.
4. Approvals >= quorum  -> block is sealed on the ledger.
   Anything else        -> rejected (default deny), including timeouts.
5. Slashing: an eligible validator that did not vote gets a strike; a vote
   clears the strikes. MAX_MISSED_VOTES consecutive strikes demote the
   validator to OBSERVER for the rest of the activity.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from fastapi import WebSocket

from . import config
from .ledger import Ledger

log = logging.getLogger("nodeplay")

# While the activity is running, a disconnected node keeps its role (and slot)
# for this many seconds, so a phone whose screen locked for a moment gets its
# role back on reconnect. Before START, only connected nodes hold slots.
RECONNECT_GRACE = 60.0

# Maximum number of entries kept in the activity feed.
MAX_EVENTS = 40

# Every participant must choose a nickname to join (length after cleaning).
NICKNAME_MIN = 2
NICKNAME_MAX = 20


class Role(str, Enum):
    WRITER = "WRITER"
    VALIDATOR = "VALIDATOR"
    OBSERVER = "OBSERVER"


class Phase(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    ENDED = "ENDED"


class RoundStatus(str, Enum):
    VOTING = "VOTING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class Node:
    """A participant (one phone / browser tab)."""

    sid: str  # session id generated and persisted by the browser
    name: str
    seq: int  # join order (used for default names like Node-07)
    role: Role
    ws: WebSocket | None = None
    disconnected_at: float = 0.0
    missed: int = 0  # consecutive missed votes
    eliminated: bool = False
    bot: bool = False  # built-in validator living inside the server

    @property
    def connected(self) -> bool:
        return self.bot or self.ws is not None


@dataclass
class Round:
    """One proposal travelling through the mempool."""

    id: int
    tx: str
    proposer: str
    proposer_sid: str
    started_at: float
    deadline: float
    eligible: set[str]  # sids of validators allowed to vote
    votes: dict[str, bool] = field(default_factory=dict)  # sid -> approve?
    status: RoundStatus = RoundStatus.VOTING
    reason: str = ""
    closed_at: float = 0.0
    block_index: int | None = None
    all_voted: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def required(self) -> int:
        """Strict majority (> 50 %) of the eligible validators."""
        return len(self.eligible) // 2 + 1

    @property
    def approvals(self) -> int:
        return sum(1 for v in self.votes.values() if v)

    @property
    def rejections(self) -> int:
        return sum(1 for v in self.votes.values() if not v)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tx": self.tx,
            "proposer": self.proposer,
            "started_at": self.started_at,
            "deadline": self.deadline,
            "status": self.status.value,
            "reason": self.reason,
            "eligible": len(self.eligible),
            "required": self.required,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "pending": len(self.eligible) - len(self.votes),
            "block_index": self.block_index,
        }


class ActionError(Exception):
    """A client request that breaks a rule; the message is shown to the user."""


_WHITESPACE = re.compile(r"\s+")


def clean_text(value: object, max_len: int) -> str:
    """Normalise user input: printable chars only, single spaces, bounded length."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    return _WHITESPACE.sub(" ", text).strip()[:max_len]


class Engine:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.dashboards: dict[WebSocket, bool] = {}  # ws -> is_admin
        self.ledger = Ledger()
        self._seq = 0
        self._round_seq = 0
        self._broadcast_lock = asyncio.Lock()
        self._game_task: asyncio.Task | None = None
        self._round_task: asyncio.Task | None = None
        self._bot_tasks: set[asyncio.Task] = set()
        self._clear_game_state()
        self._spawn_bot_validators()

    def _spawn_bot_validators(self) -> None:
        """Create the built-in validators that always vote APPROVE."""
        for i in range(1, config.BOT_VALIDATORS_COUNT + 1):
            sid = f"bot-validator-{i}"
            self.nodes[sid] = Node(sid=sid, name=f"Bot-V{i}", seq=0, role=Role.VALIDATOR, bot=True)
        log.info("%d bot validators enabled (always approve)", config.BOT_VALIDATORS_COUNT)

    @property
    def _bots(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.bot]

    def _clear_game_state(self) -> None:
        self.phase = Phase.IDLE
        self.started_at = 0.0
        self.ends_at = 0.0
        self.round: Round | None = None  # current round, or the last closed one
        self.mempool_open_at = 0.0
        self.rejected_count = 0
        self.timeout_count = 0
        self.eliminated_log: list[dict] = []
        self.events: list[dict] = []

    # ------------------------------------------------------------------ roles

    def _holds_slot(self, node: Node, now: float) -> bool:
        """True while `node` still occupies its role quota."""
        if node.connected:
            return True
        return self.phase is Phase.RUNNING and (now - node.disconnected_at) < RECONNECT_GRACE

    def _slots_taken(self, role: Role, now: float, exclude: Node | None = None) -> int:
        """
        Count how many quota slots of `role` are in use.

        Slashed validators keep consuming a validator slot: the network
        permanently loses that validator, so the quorum really shrinks.
        """
        count = 0
        for node in self.nodes.values():
            if node is exclude or not self._holds_slot(node, now):
                continue
            if role is Role.VALIDATOR and node.eliminated:
                count += 1
            elif node.role is role:
                count += 1
        return count

    def _pick_role(self, now: float, exclude: Node | None = None) -> Role:
        writers = self._slots_taken(Role.WRITER, now, exclude)
        validators = self._slots_taken(Role.VALIDATOR, now, exclude)

        # Keep the network playable: someone must propose and someone must
        # validate (bots, when enabled, already cover the second rule).
        if writers == 0:
            return Role.WRITER
        if validators == 0:
            return Role.VALIDATOR

        # Otherwise every role with a free seat is equally likely
        # (observers are unlimited, so they are always an option).
        free = [Role.OBSERVER]
        if not config.MAX_WRITERS or writers < config.MAX_WRITERS:
            free.append(Role.WRITER)
        if validators < config.MAX_VALIDATORS:
            free.append(Role.VALIDATOR)
        return random.choice(free)

    def _active(self, role: Role) -> list[Node]:
        return [n for n in self.nodes.values() if n.connected and n.role is role]

    # ------------------------------------------------------------ connections

    async def connect_node(self, ws: WebSocket, sid: str, name: str) -> Node:
        now = time.time()
        sid = clean_text(sid, 64)
        name = clean_text(name, NICKNAME_MAX)
        node = self.nodes.get(sid) if sid else None

        if node is None:
            self._seq += 1
            sid = sid or f"anon-{self._seq}"
            node = Node(
                sid=sid,
                name=name or f"Node-{self._seq:02d}",
                seq=self._seq,
                role=self._pick_role(now),
            )
            self.nodes[sid] = node
            self._event(f"{node.name} joined as {node.role.value}", "join")
        else:
            # Same browser coming back (reload, screen lock, Wi-Fi hiccup).
            if node.ws is not None:
                with contextlib.suppress(Exception):
                    await node.ws.close(code=4000, reason="Opened in another tab")
            elif not self._holds_slot(node, now) and not node.eliminated:
                # Grace period expired: the slot may have been given away.
                node.role = self._pick_role(now, exclude=node)
            if name:
                node.name = name
            self._event(f"{node.name} reconnected as {node.role.value}", "join")

        node.ws = ws
        log.info("node %s (%s) connected as %s", node.name, node.sid, node.role.value)
        await self.broadcast()
        return node

    async def leave(self, node: Node) -> None:
        """
        The participant pressed "Leave network": forget the node right away
        (no reconnect grace), so its seat is free and a later join draws a
        brand-new random role.
        """
        if self.nodes.get(node.sid) is node:
            del self.nodes[node.sid]
        node.ws = None
        log.info("node %s left the network", node.name)
        self._event(f"{node.name} left the network", "leave")
        await self.broadcast()

    async def disconnect_node(self, node: Node, ws: WebSocket) -> None:
        if self.nodes.get(node.sid) is not node:  # already left via leave()
            return
        if node.ws is not ws:  # already replaced by a newer connection
            return
        node.ws = None
        node.disconnected_at = time.time()
        log.info("node %s disconnected", node.name)
        self._event(f"{node.name} left the network", "leave")
        await self.broadcast()

    async def connect_dashboard(self, ws: WebSocket, is_admin: bool) -> None:
        self.dashboards[ws] = is_admin
        await self._send(ws, self._message_for_dashboard(self.public_state(), is_admin))

    def disconnect_dashboard(self, ws: WebSocket) -> None:
        self.dashboards.pop(ws, None)

    # ------------------------------------------------------------- game flow

    async def start(self) -> None:
        if self.phase is Phase.RUNNING:
            raise ActionError("The activity is already running.")
        if self.phase is Phase.ENDED:
            await self.reset(broadcast=False)

        now = time.time()
        self.phase = Phase.RUNNING
        self.started_at = now
        self.ends_at = now + config.GAME_DURATION
        self._game_task = asyncio.create_task(self._game_timer())
        self._event("Activity started — the network is live!", "start")
        if self._bots:
            self._event(f"{len(self._bots)} bot validators online — they always approve", "start")
        log.info("activity started (%ss)", config.GAME_DURATION)
        await self.broadcast()

    async def reset(self, broadcast: bool = True) -> None:
        for task in (self._game_task, self._round_task, *self._bot_tasks):
            if task and not task.done():
                task.cancel()
        self._game_task = self._round_task = None
        self._bot_tasks.clear()

        self._clear_game_state()
        self.ledger.reset()

        # Forget nodes that are gone, then draw every role again in a random
        # order (slashed validators come back).
        now = time.time()
        self.nodes = {sid: n for sid, n in self.nodes.items() if n.connected}
        humans = [n for n in self.nodes.values() if not n.bot]
        for node in self.nodes.values():
            node.missed = 0
            node.eliminated = False
        for node in humans:
            node.role = Role.OBSERVER  # free every seat before re-assigning
        random.shuffle(humans)
        for node in humans:
            node.role = self._pick_role(now, exclude=node)

        self._event("Network reset — new genesis block sealed", "reset")
        log.info("activity reset")
        if broadcast:
            await self.broadcast()

    async def stop(self) -> None:
        """The facilitator ends the activity early (STOP button)."""
        if self.phase is not Phase.RUNNING:
            raise ActionError("The activity is not running.")
        for task in (self._game_task, self._round_task, *self._bot_tasks):
            if task and not task.done():
                task.cancel()
        self._game_task = self._round_task = None
        self._bot_tasks.clear()

        now = time.time()
        rnd = self.round
        if rnd and rnd.status is RoundStatus.VOTING:
            # Cancelled, not judged: it is not counted as a rejected block and
            # nobody gets a strike for it.
            rnd.status = RoundStatus.REJECTED
            rnd.closed_at = now
            rnd.reason = "Vote cancelled — the activity was stopped"
        self.phase = Phase.ENDED
        self.ends_at = now
        self._event("Activity stopped by the facilitator.", "end")
        log.info("activity stopped")
        await self.broadcast()

    async def _game_timer(self) -> None:
        await asyncio.sleep(max(0.0, self.ends_at - time.time()))
        self.phase = Phase.ENDED
        self._event("Time is up! The mempool is closed.", "end")
        log.info("activity ended")
        await self.broadcast()

    def _accepting_proposals(self, now: float) -> bool:
        return (
            self.phase is Phase.RUNNING
            and now < self.ends_at
            and not (self.round and self.round.status is RoundStatus.VOTING)
            and now >= self.mempool_open_at
        )

    # ------------------------------------------------------------- consensus

    async def propose(self, node: Node, raw_tx: object) -> None:
        now = time.time()
        if node.role is not Role.WRITER:
            raise ActionError("Only WRITER nodes can propose transactions.")
        if self.phase is not Phase.RUNNING or now >= self.ends_at:
            raise ActionError("The activity is not running.")
        if self.round and self.round.status is RoundStatus.VOTING:
            raise ActionError("Another proposal is already being voted on.")
        if now < self.mempool_open_at:
            raise ActionError("The mempool reopens in a moment.")
        tx = clean_text(raw_tx, config.MAX_TX_LENGTH)
        if not tx:
            raise ActionError("The transaction cannot be empty.")

        self._round_seq += 1
        eligible = {n.sid for n in self._active(Role.VALIDATOR)}
        self.round = Round(
            id=self._round_seq,
            tx=tx,
            proposer=node.name,
            proposer_sid=node.sid,
            started_at=now,
            deadline=now + config.VOTE_TIMEOUT,
            eligible=eligible,
        )
        self._event(f'{node.name} proposed "{tx}" (needs {self.round.required}/{len(eligible)})', "propose")
        self._round_task = asyncio.create_task(self._run_round(self.round))
        for bot in self._bots:
            if bot.sid in eligible:
                task = asyncio.create_task(self._bot_vote(bot, self.round.id))
                self._bot_tasks.add(task)
                task.add_done_callback(self._bot_tasks.discard)
        await self.broadcast()

    async def _bot_vote(self, bot: Node, round_id: int) -> None:
        """Bots "think" for a moment (well within the deadline), then approve."""
        await asyncio.sleep(random.uniform(0.3, min(3.0, config.VOTE_TIMEOUT * 0.5)))
        with contextlib.suppress(ActionError):  # round already closed / reset
            await self.vote(bot, round_id, True)

    async def vote(self, node: Node, round_id: object, approve: object) -> None:
        rnd = self.round
        if node.role is not Role.VALIDATOR:
            raise ActionError("Only active VALIDATOR nodes can vote.")
        if rnd is None or rnd.status is not RoundStatus.VOTING or rnd.id != round_id:
            raise ActionError("This voting round is already closed.")
        if node.sid not in rnd.eligible:
            raise ActionError("You joined after this round started; wait for the next one.")
        if node.sid in rnd.votes:
            raise ActionError("You already voted in this round.")

        rnd.votes[node.sid] = bool(approve)
        node.missed = 0
        if len(rnd.votes) >= len(rnd.eligible):
            rnd.all_voted.set()
        await self.broadcast()

    async def _run_round(self, rnd: Round) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(rnd.all_voted.wait(), timeout=max(0.0, rnd.deadline - time.time()))
        await self._close_round(rnd)

        # Keep the result on screen briefly, then reopen the mempool.
        await asyncio.sleep(config.RESULT_PAUSE)
        await self.broadcast()

    async def _close_round(self, rnd: Round) -> None:
        now = time.time()
        rnd.closed_at = now
        approvals, rejections = rnd.approvals, rnd.rejections
        timed_out = len(rnd.votes) < len(rnd.eligible)

        if approvals >= rnd.required:
            block = self.ledger.append(rnd.tx, rnd.proposer, approvals, rejections)
            rnd.status = RoundStatus.APPROVED
            rnd.block_index = block.index
            rnd.reason = f"Quorum reached ({approvals}/{len(rnd.eligible)}) — block #{block.index} sealed"
            self._event(f'Block #{block.index} sealed: "{rnd.tx}" ({approvals} approvals)', "block")
        else:
            rnd.status = RoundStatus.REJECTED
            self.rejected_count += 1
            if not rnd.eligible:
                rnd.reason = "No active validators — default deny"
            elif timed_out and approvals + rejections == 0:
                rnd.reason = "Timeout with no votes — default deny"
            elif timed_out:
                rnd.reason = f"Timeout: quorum not reached ({approvals}/{rnd.required} approvals) — default deny"
            else:
                rnd.reason = f"Rejected by validators ({approvals}/{rnd.required} approvals)"
            if timed_out:
                self.timeout_count += 1
            self._event(f'Block not created: "{rnd.tx}" — {rnd.reason}', "reject")

        # Slashing: strikes for every eligible validator that stayed silent.
        for sid in rnd.eligible:
            node = self.nodes.get(sid)
            if node is None or sid in rnd.votes or node.role is not Role.VALIDATOR:
                continue
            node.missed += 1
            if node.missed >= config.MAX_MISSED_VOTES:
                await self._slash(node, rnd)

        self.mempool_open_at = now + config.RESULT_PAUSE
        await self.broadcast()

    async def _slash(self, node: Node, rnd: Round) -> None:
        node.role = Role.OBSERVER
        node.eliminated = True
        self.eliminated_log.append({"name": node.name, "round": rnd.id, "at": time.time()})
        self._event(f"{node.name} was SLASHED ({node.missed} missed votes in a row)", "slash")
        log.info("validator %s slashed", node.name)
        if node.ws is not None:
            await self._send(node.ws, {
                "type": "notice",
                "level": "danger",
                "text": f"You were demoted to Observer for inactivity ({node.missed} missed votes).",
            })

    # ------------------------------------------------------------- snapshots

    def _event(self, text: str, kind: str) -> None:
        self.events.append({"at": time.time(), "text": text, "kind": kind})
        del self.events[:-MAX_EVENTS]

    def public_state(self) -> dict:
        now = time.time()
        validators = len(self._active(Role.VALIDATOR))
        return {
            "type": "state",
            "server_time": now,
            "phase": self.phase.value,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "duration": config.GAME_DURATION,
            "vote_timeout": config.VOTE_TIMEOUT,
            "max_tx_length": config.MAX_TX_LENGTH,
            "accepting": self._accepting_proposals(now),
            "metrics": {
                "writers": len(self._active(Role.WRITER)),
                "validators": validators,
                "observers": len(self._active(Role.OBSERVER)),
                "bots": sum(1 for n in self._bots if n.role is Role.VALIDATOR),
                "online": sum(1 for n in self.nodes.values() if n.connected and not n.bot),
                "quorum": validators // 2 + 1,
            },
            "stats": {
                "blocks_created": len(self.ledger.blocks) - 1,  # genesis excluded
                "blocks_rejected": self.rejected_count,
                "timeouts": self.timeout_count,
                "eliminated": len(self.eliminated_log),
            },
            "round": self.round.to_dict() if self.round else None,
            "ledger": self.ledger.to_list(),
            "chain_valid": self.ledger.is_valid(),
            "eliminated_log": self.eliminated_log,
            "events": self.events,
        }

    def _message_for_node(self, state: dict, node: Node) -> dict:
        rnd = self.round
        vote = None
        if rnd and node.sid in rnd.votes:
            vote = "approve" if rnd.votes[node.sid] else "reject"
        return {
            **state,
            "you": {
                "name": node.name,
                "role": node.role.value,
                "eliminated": node.eliminated,
                "missed": node.missed,
                "max_missed": config.MAX_MISSED_VOTES,
                "eligible": bool(rnd and node.sid in rnd.eligible),
                "vote": vote,
            },
        }

    @staticmethod
    def _message_for_dashboard(state: dict, is_admin: bool) -> dict:
        return {**state, "admin": is_admin}

    # ------------------------------------------------------------- transport

    @staticmethod
    async def _send(ws: WebSocket, message: dict) -> None:
        # A slow or dead client must never stall the whole network.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.send_json(message), timeout=2.0)

    async def broadcast(self) -> None:
        """Push a fresh snapshot to every node (personalised) and dashboard."""
        async with self._broadcast_lock:
            state = self.public_state()
            sends = [
                self._send(node.ws, self._message_for_node(state, node))
                for node in self.nodes.values()
                if node.ws is not None
            ]
            sends += [
                self._send(ws, self._message_for_dashboard(state, is_admin))
                for ws, is_admin in list(self.dashboards.items())
            ]
            await asyncio.gather(*sends)
