"""
Runtime configuration.

Values are read from environment variables. A `.env` file in the project root
is loaded automatically (see `.env.example`); variables already set in the
shell take precedence over it. Example of a quick rehearsal:

    GAME_DURATION=60 VOTE_TIMEOUT=5 uvicorn main:app --host 0.0.0.0
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Total length of the activity, in seconds (default: 5 minutes).
GAME_DURATION: int = _int_env("GAME_DURATION", 300)

# Time validators have to vote on each proposal, in seconds.
VOTE_TIMEOUT: int = _int_env("VOTE_TIMEOUT", 10)

# Pause after a round closes so everybody can see the result before the
# mempool accepts the next proposal, in seconds.
RESULT_PAUSE: int = _int_env("RESULT_PAUSE", 2)

# Role quotas. Nodes that connect after these quotas are filled become observers.
MAX_WRITERS: int = _int_env("MAX_WRITERS", 5)
MAX_VALIDATORS: int = _int_env("MAX_VALIDATORS", 7)

# Consecutive missed votes (timeouts) before a validator is slashed.
MAX_MISSED_VOTES: int = _int_env("MAX_MISSED_VOTES", 2)

# Maximum length of a proposed transaction (word / ARK identifier).
MAX_TX_LENGTH: int = _int_env("MAX_TX_LENGTH", 48)

# Optional shared secret for the dashboard controls (START / RESET).
# When empty, anyone who opens /dashboard can control the activity.
ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "")

# ------------------------------------------------------------ network

# Port used by `python main.py` (with uvicorn, pass --port instead).
PORT: int = _int_env("PORT", 8000)

# IP address phones should connect to. Leave empty to detect it automatically
# when the server starts; set it when detection picks the wrong interface
# (VPN, Docker, several network cards).
HOST_IP: str = os.environ.get("HOST_IP", "").strip()

# Optional full public base URL (e.g. "http://192.168.0.10:8000"). When set,
# it wins over HOST_IP and auto-detection.
PUBLIC_URL: str = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

# Wi-Fi network every participant must join. When WIFI_SSID is set, the
# dashboard shows the network name, the password and a "join Wi-Fi" QR code.
WIFI_SSID: str = os.environ.get("WIFI_SSID", "").strip()
WIFI_PASSWORD: str = os.environ.get("WIFI_PASSWORD", "")
# WPA (also covers WPA2/WPA3), WEP or nopass. Defaults to nopass without a password.
WIFI_SECURITY: str = (os.environ.get("WIFI_SECURITY", "").strip().upper()
                      or ("WPA" if WIFI_PASSWORD else "nopass"))
