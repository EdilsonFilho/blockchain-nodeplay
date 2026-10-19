"""
A deliberately simple, append-only cryptographic ledger.

Each block links to its predecessor through `prev_hash` (the first 8 hex
characters of the previous block's SHA-256 hash, short enough to read on a
projector). Tampering with any block changes its hash and breaks the link
with the next one, which is exactly the property we want to demonstrate.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field

GENESIS_TX = "GENESIS"
GENESIS_PREV_HASH = "0" * 8


@dataclass
class Block:
    index: int
    timestamp: float
    tx: str
    prev_hash: str
    # Extra (non-required) metadata that makes the demo more readable.
    proposer: str = ""
    approvals: int = 0
    rejections: int = 0
    hash: str = field(default="")

    def compute_hash(self) -> str:
        """SHA-256 over the block contents (everything except the hash itself)."""
        payload = {
            "index": self.index,
            "timestamp": self.timestamp,
            "tx": self.tx,
            "prev_hash": self.prev_hash,
            "proposer": self.proposer,
            "approvals": self.approvals,
            "rejections": self.rejections,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


class Ledger:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.reset()

    def reset(self) -> None:
        """Drop every block and seal a fresh genesis block."""
        genesis = Block(
            index=0,
            timestamp=time.time(),
            tx=GENESIS_TX,
            prev_hash=GENESIS_PREV_HASH,
            proposer="network",
        )
        genesis.hash = genesis.compute_hash()
        self.blocks = [genesis]

    @property
    def last(self) -> Block:
        return self.blocks[-1]

    def append(self, tx: str, proposer: str, approvals: int, rejections: int) -> Block:
        """Seal a new block on top of the chain and return it."""
        block = Block(
            index=self.last.index + 1,
            timestamp=time.time(),
            tx=tx,
            prev_hash=self.last.hash[:8],
            proposer=proposer,
            approvals=approvals,
            rejections=rejections,
        )
        block.hash = block.compute_hash()
        self.blocks.append(block)
        return block

    def is_valid(self) -> bool:
        """Re-verify every hash and every link of the chain."""
        for i, block in enumerate(self.blocks):
            if block.hash != block.compute_hash():
                return False
            if i > 0 and block.prev_hash != self.blocks[i - 1].hash[:8]:
                return False
        return True

    def to_list(self) -> list[dict]:
        return [b.to_dict() for b in self.blocks]
