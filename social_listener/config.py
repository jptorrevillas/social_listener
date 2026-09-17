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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Load from the project root explicitly, not via a bare load_dotenv().
#
# A bare call resolves relative to the CURRENT WORKING DIRECTORY, so the key is
# found when you happen to run from the repo root and silently is not when you
# do not -- which presents as "no API key found" with no clue why. Anchoring to
# __file__ makes it work from anywhere. The cwd is then tried as a fallback, so
# a .env sitting beside wherever you launched from still works.
# A malformed .env must not take the application down with an unreadable
# traceback. Windows PowerShell 5.1 writes UTF-16 when you use '>' redirection,
# which python-dotenv cannot decode; `doctor` reports that in plain words.
ENV_LOAD_ERROR = None
try:
    load_dotenv(ENV_PATH)
    load_dotenv()
except (UnicodeDecodeError, OSError) as exc:  # pragma: no cover - see doctor
    ENV_LOAD_ERROR = f"{type(exc).__name__}: {exc}"


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


def diagnose_env() -> dict:
    """Why is the key not being seen? Answers without printing the key.

    Every check here corresponds to a failure mode that presents identically
    -- the app quietly runs in demo mode -- and is otherwise invisible.
    """
    report = {
        "env_path": str(ENV_PATH),
        "env_exists": ENV_PATH.exists(),
        "cwd": os.getcwd(),
        "mode": settings.mode,
        "key_seen": bool(settings.youtube_api_key),
        "problems": [],
        "notes": [],
    }

    key = settings.youtube_api_key
    if key:
        report["key_fingerprint"] = f"{key[:6]}...{key[-4:]} ({len(key)} chars)"
        if key.strip() != key:
            report["problems"].append(
                "The key has leading or trailing whitespace; it will be sent as-is."
            )
        if len(key) < 30:
            report["problems"].append(
                f"The key is only {len(key)} characters. Google keys are ~39."
            )

    # A .env named .env.txt: Windows Explorer hides known extensions, so a file
    # that looks like ".env" is often ".env.txt" on disk.
    for stray in (".env.txt", ".env.example.txt", "env"):
        candidate = PROJECT_ROOT / stray
        if candidate.exists():
            report["problems"].append(
                f"Found '{stray}' next to the project. If that is meant to be "
                f"your .env, rename it -- Windows hides known extensions, so a "
                f"file shown as '.env' can really be '.env.txt'."
            )

    if ENV_PATH.exists():
        raw = ENV_PATH.read_bytes()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            report["problems"].append(
                "The .env is UTF-16 encoded, which cannot be parsed. Windows "
                "PowerShell 5.1 writes UTF-16 when you use '>' redirection. "
                "Re-save it as UTF-8."
            )
        else:
            text = raw.decode("utf-8", errors="replace")
            occurrences = [
                line for line in text.splitlines()
                if line.strip().startswith("YOUTUBE_API_KEY")
            ]
            if len(occurrences) > 1:
                report["problems"].append(
                    f"YOUTUBE_API_KEY appears {len(occurrences)} times in .env. "
                    f"The LAST one wins, and .env.example ships with an empty "
                    f"one -- delete the blank line."
                )
            elif occurrences and occurrences[0].split("=", 1)[-1].strip() == "":
                report["problems"].append(
                    "YOUTUBE_API_KEY is present in .env but empty."
                )
            if not occurrences:
                report["problems"].append(
                    "No YOUTUBE_API_KEY line found in .env at all."
                )
    else:
        report["problems"].append(
            f"No .env at {ENV_PATH}. Create it there, or set the environment "
            f"variable directly: $env:YOUTUBE_API_KEY=\"...\" (PowerShell)."
        )

    if ENV_LOAD_ERROR:
        report["problems"].append(
            f"The .env file could not be read ({ENV_LOAD_ERROR}). It was "
            f"ignored rather than crashing the app."
        )

    if os.getenv("YOUTUBE_API_KEY"):
        report["notes"].append(
            "YOUTUBE_API_KEY is set in the process environment, which takes "
            "precedence over .env."
        )
    return report
