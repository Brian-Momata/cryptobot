"""Settings from environment variables, optionally loaded from a .env file.

Secrets (API keys, Telegram token) are never printed: `Settings.__repr__` hides them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_FIELDS = ("api_key", "api_secret", "telegram_token", "healthcheck_url")
LIMIT_NAMES = ("soft_daily_loss", "hard_daily_loss", "max_drawdown", "max_price_gap", "max_reconcile_gap")


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=value lines, # comments, optional quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


@dataclass
class Settings:
    api_key: str = ""
    api_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    healthcheck_url: str = ""
    symbol: str = "BTC/USDT"
    allocated_usdt: float = 1_000.0
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "live.sqlite")
    kill_path: Path = field(default_factory=lambda: ROOT / "data" / "KILL")
    # Optional risk-limit overrides, e.g. HARD_DAILY_LOSS=-0.001 for a safety drill.
    limit_overrides: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, env: dict[str, str] | None = None, dotenv: Path | None = None) -> "Settings":
        """Environment variables win over the .env file."""
        merged = {**read_dotenv(dotenv or ROOT / ".env"), **(os.environ if env is None else env)}
        s = cls(
            api_key=merged.get("BINANCE_TESTNET_API_KEY", ""),
            api_secret=merged.get("BINANCE_TESTNET_API_SECRET", ""),
            telegram_token=merged.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=merged.get("TELEGRAM_CHAT_ID", ""),
            healthcheck_url=merged.get("HEALTHCHECK_URL", ""),
            symbol=merged.get("SYMBOL", "BTC/USDT"),
            allocated_usdt=float(merged.get("ALLOCATED_USDT", 1_000.0)),
        )
        if merged.get("DB_PATH"):
            s.db_path = Path(merged["DB_PATH"])
        if merged.get("KILL_PATH"):
            s.kill_path = Path(merged["KILL_PATH"])
        for name in LIMIT_NAMES:
            if merged.get(name.upper()):
                s.limit_overrides[name] = float(merged[name.upper()])
        return s

    def __repr__(self) -> str:
        shown = {k: ("***" if k in SECRET_FIELDS and v else v) for k, v in self.__dict__.items()}
        return "Settings(" + ", ".join(f"{k}={v!r}" for k, v in shown.items()) + ")"
