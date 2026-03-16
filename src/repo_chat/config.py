"""Config loader for repo-chat.

Create a .repo-chat.toml file in your working directory to use a custom API:

    api_key = "your-api-key"
    api_url = "https://api.openai.com/v1"
    model   = "gpt-4o"          # optional

If the file is absent or empty, repo-chat falls back to the claude CLI.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

_CONFIG_FILE = Path(".repo-chat.toml")


@dataclass
class Config:
    api_key: str | None = None
    api_url: str | None = None
    model: str | None = None

    @property
    def use_direct_api(self) -> bool:
        return bool(self.api_key and self.api_url)


def load_config() -> Config:
    if not _CONFIG_FILE.exists():
        return Config()
    try:
        with open(_CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        return Config(
            api_key=data.get("api_key") or None,
            api_url=(data.get("api_url") or "").rstrip("/") or None,
            model=data.get("model") or None,
        )
    except Exception:
        return Config()
