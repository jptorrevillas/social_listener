"""Runtime configuration.

Everything has a working default so the demo runs with no .env at all. The
YouTube API key is the exception: without it the app falls back to the synthetic
corpus rather than failing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'social_listener.db'}"
        )
    )

    youtube_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("YOUTUBE_API_KEY") or None
    )

    # Share of the 10,000-unit day that discovery (search.list, 100 units a
    # call) may consume. The rest goes to harvesting and the retention refresh,
    # which are 1 unit a call and must never be starved -- refreshing is a
    # policy obligation, whereas discovery is merely useful.
    discovery_budget_fraction: float = field(
        default_factory=lambda: _env_float("DISCOVERY_BUDGET_FRACTION", 0.20)
    )
    # Reserve held back so the retention job can always run.
    retention_reserve_units: int = field(
        default_factory=lambda: _env_int("RETENTION_RESERVE_UNITS", 500)
    )

    comment_pages_per_video: int = field(
        default_factory=lambda: _env_int("COMMENT_PAGES_PER_VIDEO", 3)
    )

    @property
    def has_api_key(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def mode(self) -> str:
        return "live" if self.has_api_key else "demo"


settings = Settings()
