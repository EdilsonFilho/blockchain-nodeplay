"""
Runtime configuration.

Every value can be overridden with an environment variable, which makes it
easy to rehearse the activity with shorter timers, e.g.:

    GAME_DURATION=60 VOTE_TIMEOUT=5 uvicorn main:app --host 0.0.0.0
"""

import os


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

# Optional public base URL shown on the dashboard / QR code
# (e.g. "http://192.168.0.10:8000"). Auto-detected when empty.
PUBLIC_URL: str = os.environ.get("PUBLIC_URL", "").rstrip("/")
