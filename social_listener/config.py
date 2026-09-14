"""Runtime configuration, read once from the environment.

Everything has a working default so the demo runs with no .env at all. The
Reddit credentials are the exception: without them the app falls back to the
synthetic corpus rather than failing, because obtaining them is a manual
approval process that takes days (see docs/SPECIFICATION.md §3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'social_listener.db'}"
        )
    )

    reddit_client_id: str | None = field(
        default_factory=lambda: os.getenv("REDDIT_CLIENT_ID") or None
    )
    reddit_client_secret: str | None = field(
        default_factory=lambda: os.getenv("REDDIT_CLIENT_SECRET") or None
    )
    reddit_user_agent: str = field(
        default_factory=lambda: os.getenv(
            "REDDIT_USER_AGENT", "server:social-listener:v0.1.0 (by /u/unknown)"
        )
    )

    # The documented free-tier ceiling. The limiter treats this as a fallback
    # only -- live X-Ratelimit-* headers override it (§3.3).
    rate_limit_qpm: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_QPM", 100)
    )
    # Fraction of the budget held back for retries, hydration and the
    # compliance sweep.
    rate_limit_headroom: float = field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_HEADROOM", "0.15"))
    )

    retention_months: int = field(
        default_factory=lambda: _env_int("RETENTION_MONTHS", 18)
    )
    compliance_sweep_hours: int = field(
        default_factory=lambda: _env_int("COMPLIANCE_SWEEP_HOURS", 24)
    )

    @property
    def has_reddit_credentials(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def mode(self) -> str:
        """'live' once credentials exist, 'demo' until then."""
        return "live" if self.has_reddit_credentials else "demo"


settings = Settings()
