"""Configuration for HACKFORGE.

All configuration is environment-driven with safe defaults, so the system can
run (degraded but honest) on a fresh machine. Nothing here reaches the network
or the filesystem beyond reading config files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

UNKNOWN = "UNKNOWN"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name)
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class DeadlineThresholds:
    """Urgency bands from specification section 19. All values are configurable.

    Bands (remaining time to the verified submission deadline):
        < final_window_hours        FINAL_WINDOW
        < critical_hours            CRITICAL
        <= active_days (7)         URGENT
        <= planning_days (14)      ACTIVE
        <= long_range_days (30)    PLANNING
        > long_range_days          LONG_RANGE
    """

    long_range_days: int = 30
    planning_days: int = 14
    active_days: int = 7
    critical_hours: int = 72
    final_window_hours: int = 24


@dataclass(frozen=True)
class ResearchConfig:
    """Limits for the research engine so it cannot run away."""

    max_results_per_source: int = 6
    request_timeout_seconds: int = 20
    max_pages_per_opportunity: int = 25
    obey_robots: bool = True
    user_agent: str = "hackforge-research/0.1 (+personal opportunity agent)"


@dataclass(frozen=True)
class GmailConfig:
    """OAuth credentials for read-only Gmail access."""

    credentials_path: Path | None = None
    token_path: Path | None = None
    query: str = "newer_than:90d"
    max_messages: int = 200


@dataclass(frozen=True)
class Config:
    autonomy_level: int = 0
    data_dir: Path = field(default_factory=lambda: Path.home() / ".hackforge")
    workspace_root: Path = field(default_factory=lambda: Path.home() / "Hackathons")
    timezone: str = "UTC"
    database_url: str = ""
    gmail: GmailConfig = field(default_factory=GmailConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    deadlines: DeadlineThresholds = field(default_factory=DeadlineThresholds)
    max_autonomy_level: int = 3

    def __post_init__(self) -> None:
        if self.autonomy_level < 0 or self.autonomy_level > self.max_autonomy_level:
            raise ValueError(
                f"autonomy_level must be between 0 and {self.max_autonomy_level}, "
                f"got {self.autonomy_level}"
            )

    @property
    def state_path(self) -> Path:
        return self.data_dir / "opportunities.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "activity.log"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def with_autonomy(self, level: int) -> "Config":
        return replace(self, autonomy_level=level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "autonomy_level": self.autonomy_level,
            "data_dir": str(self.data_dir),
            "workspace_root": str(self.workspace_root),
            "timezone": self.timezone,
            "database_url": self.database_url,
        }


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. Never overrides real environment variables."""

    candidate = path or (Path.cwd() / ".env")
    if not candidate.is_file():
        return
    for raw_line in candidate.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _thresholds_from(raw: Mapping[str, Any]) -> DeadlineThresholds:
    base = DeadlineThresholds()
    return DeadlineThresholds(
        long_range_days=int(raw.get("long_range_days", base.long_range_days)),
        planning_days=int(raw.get("planning_days", base.planning_days)),
        active_days=int(raw.get("active_days", base.active_days)),
        critical_hours=int(raw.get("critical_hours", base.critical_hours)),
        final_window_hours=int(raw.get("final_window_hours", base.final_window_hours)),
    )


def _research_from(raw: Mapping[str, Any]) -> ResearchConfig:
    base = ResearchConfig()
    return ResearchConfig(
        max_results_per_source=int(
            raw.get("max_results_per_source", base.max_results_per_source)
        ),
        request_timeout_seconds=int(
            raw.get("request_timeout_seconds", base.request_timeout_seconds)
        ),
        max_pages_per_opportunity=int(
            raw.get("max_pages_per_opportunity", base.max_pages_per_opportunity)
        ),
        obey_robots=bool(raw.get("obey_robots", base.obey_robots)),
        user_agent=str(raw.get("user_agent", base.user_agent)),
    )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from environment variables and optional config.json."""

    source = env if env is not None else os.environ

    data_dir = _env_path("HACKFORGE_DATA_DIR", Path.home() / ".hackforge")
    workspace_root = _env_path("HACKFORGE_WORKSPACE_ROOT", Path.home() / "Hackathons")

    file_overrides: dict[str, Any] = {}
    config_file = source.get("HACKFORGE_CONFIG_FILE")
    if config_file:
        path = Path(config_file).expanduser()
        if path.is_file():
            try:
                file_overrides = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid HACKFORGE_CONFIG_FILE {path}: {exc}") from exc

    credentials = source.get("GMAIL_CREDENTIALS_PATH")
    token = source.get("GMAIL_TOKEN_PATH")

    return Config(
        autonomy_level=int(source.get("HACKFORGE_AUTONOMY_LEVEL", "0") or 0),
        data_dir=data_dir,
        workspace_root=workspace_root,
        timezone=source.get("HACKFORGE_TIMEZONE", "UTC") or "UTC",
        database_url=source.get("HACKFORGE_DATABASE_URL", "") or "",
        gmail=GmailConfig(
            credentials_path=Path(credentials).expanduser() if credentials else None,
            token_path=Path(token).expanduser() if token else None,
            query=source.get("GMAIL_QUERY", "newer_than:90d") or "newer_than:90d",
            max_messages=int(source.get("GMAIL_MAX_MESSAGES", "200") or 200),
        ),
        research=_research_from(file_overrides.get("research", {})),
        deadlines=_thresholds_from(file_overrides.get("deadlines", {})),
    )