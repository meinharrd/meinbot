"""Configuration: secrets from .env, everything else from config.yaml."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class ModelRole:
    provider: str            # "anthropic" | "openai" (any OpenAI-compatible endpoint)
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    context_tokens: int = 60000   # how much context we assemble, not the model max
    max_output_tokens: int = 8000
    tools: bool = True             # False for models without function calling
    extra: dict = field(default_factory=dict)   # provider-specific knobs


@dataclass
class Config:
    telegram_token: str
    owner_name: str
    owner_id: int
    memory_dir: Path
    state_dir: Path
    timezone: str
    chat: ModelRole
    memory: ModelRole
    recent_turns: int
    summarize_every: int
    push_delay: int
    pull_interval: int
    auto_apply_max: str


def _role(d: dict) -> ModelRole:
    return ModelRole(**d)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("MEINBOT_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text())
    models = raw["models"]
    return Config(
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        owner_name=raw.get("owner_name", "the owner"),
        owner_id=int(os.environ.get("OWNER_ID", "0")),
        memory_dir=Path(raw.get("memory_dir", "~/meinbot-memory")).expanduser(),
        state_dir=Path(raw.get("state_dir", ROOT / "state")).expanduser(),
        timezone=raw.get("timezone", "Europe/Berlin"),
        chat=_role(models["chat"]),
        memory=_role(models.get("memory", models["chat"])),
        recent_turns=raw.get("recent_turns", 30),
        summarize_every=raw.get("summarize_every", 40),
        push_delay=raw.get("push_delay", 30),
        pull_interval=raw.get("pull_interval", 600),
        auto_apply_max=raw.get("auto_apply_max", "private"),
    )
