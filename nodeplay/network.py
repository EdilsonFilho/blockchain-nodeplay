"""Helpers to tell participants where to connect (LAN address + QR code)."""

from __future__ import annotations

import io
import socket
from functools import lru_cache

import qrcode
import qrcode.image.svg


def lan_ip() -> str:
    """
    Best-effort detection of this machine's address on the local network.

    Opening a UDP socket towards a public address sends no packet; it only
    asks the OS which interface it would route through.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def is_loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}


@lru_cache(maxsize=16)
def qr_svg(data: str) -> bytes:
    """Render `data` as a crisp, dependency-free SVG QR code."""
    img = qrcode.make(
        data,
        image_factory=qrcode.image.svg.SvgPathFillImage,
        box_size=10,
        border=2,
    )
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()
