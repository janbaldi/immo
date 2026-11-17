"""Settings come from environment variables (or a .env file). Sources and provinces come from YAML."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./immo.db"
    secret_key: str = "change-me"
    base_url: str = "http://localhost:8000"
    province: str = "antwerpen"

    user_agent: str = "ImmoAgentBot/0.1 (+https://github.com/janbaldi/immo; polite crawler)"
    min_request_interval: float = 5.0  # seconds between requests to one domain
    http_timeout: float = 30.0
    enable_playwright: bool = False

    telegram_bot_token: str = ""
    telegram_bot_name: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "alerts@example.com"
    smtp_starttls: bool = True
    admin_email: str = ""

    llm_provider: str = "none"  # none | anthropic
    llm_model: str = "claude-opus-5-5"

    portal_crosscheck_enabled: bool = False  # switch on only after legal review
    outreach_enabled: bool = False
    outreach_mode: str = "approve"  # approve | auto
    default_segment_cap: int = 0  # 0 = unlimited
    min_comparables: int = 30
    model_path: str = "data/model.pkl"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml(name: str) -> Any:
    path = CONFIG_DIR / name
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None


def province_config(name: str | None = None) -> dict[str, Any]:
    provinces = load_yaml("provinces.yaml") or {}
    return provinces[name or get_settings().province]


def in_province(postal_code: str | None, name: str | None = None) -> bool:
    if not postal_code or not postal_code.isdigit():
        return False
    pc = int(postal_code)
    return any(lo <= pc <= hi for lo, hi in province_config(name)["postal_ranges"])
