"""Explicit runtime configuration; environment overrides saved setup and defaults.

Legacy module attributes remain lazily available for maintenance tools and callers.
Application startup constructs and injects RuntimeSettings instead of using them.
"""

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from dotenv import dotenv_values

from .soundboard import DEFAULT_SOUNDS_DIR
from .token_store import read_tokens, write_json_atomic


class ConfigError(RuntimeError):
    """A setting is missing, unparseable, or outside its supported range."""


class SetupRequired(ConfigError):
    """Twitch application credentials have not been enrolled yet."""


def _env_int(
    name: str,
    default: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    raw = (os.environ if env is None else env).get(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}, got {value}")
    return value


def _env_name_in_use(preferred, deprecated, *, env=None) -> str:
    values = os.environ if env is None else env
    return deprecated if preferred not in values and deprecated in values else preferred


def _env_csv(
    name: str, default: str, *, env: Mapping[str, str] | None = None
) -> list[str]:
    return [
        item.strip()
        for item in (os.environ if env is None else env).get(name, default).split(",")
        if item.strip()
    ]


@dataclass(frozen=True, repr=False)
class RuntimeSettings:
    CLIENT_ID: str
    CLIENT_SECRET: str
    BOT_USERNAME: str
    BOT_ID: str
    ACCESS_TOKEN: str
    REFRESH_TOKEN: str
    DB_PATH: str
    AUDIO_DIR: str
    EMOTES_DB_PATH: str
    TIMESTAMPS_DB_PATH: str
    MESSAGES_PATH: str
    VOICES_DIR: str
    SOUNDS_DIR: str
    SERVER_HOST: str
    SERVER_PORT: int
    OVERLAY_TOKEN: str
    ALLOWED_HOSTS: tuple[str, ...]
    TRUSTED_PROXIES: tuple[str, ...]
    MAX_WS_CLIENTS: int
    MAX_PENDING_PER_CLIENT: int
    WS_SEND_TIMEOUT: int
    AUDIO_SWEEP_INTERVAL_SECS: int
    AUDIO_MAX_AGE_SECS: int
    OAUTH_HOST: str
    OAUTH_PORT: int
    OAUTH_REDIRECT_URL: str
    TOKEN_FILE: str
    SETTINGS_FILE: str
    SCHEDULER_INITIAL_DELAY: int
    SCHEDULER_EMPTY_RETRY_DELAY: int
    MESSAGE_QUEUE_MAXSIZE: int
    MAX_MESSAGE_CHARS: int
    MAX_SPEECH_CHARS: int
    MAX_MESSAGE_AGE_SECS: int
    USER_COOLDOWN_SECS: int
    ANNOUNCE_WINDOW_SECS: int
    NO_ANNOUNCE_USERS: frozenset[str]
    EMOTE_SOUND_PATHS: list[str]
    LOG_LEVEL: str
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def _REQUIRED_CREDENTIALS(self) -> tuple[tuple[str, str], ...]:
        return (
            ("TWITCH_CLIENT_ID", self.CLIENT_ID),
            ("TWITCH_CLIENT_SECRET", self.CLIENT_SECRET),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, sources=None) -> "RuntimeSettings":
        def integer(name, default, **limits):
            return _env_int(name, str(default), env=env, **limits)

        def csv(name, default=""):
            return _env_csv(name, default, env=env)

        port = integer("VOXER_OAUTH_PORT", 4343, maximum=65535)
        username = env.get("TWITCH_BOT_USERNAME", "")
        return cls(
            CLIENT_ID=env.get("TWITCH_CLIENT_ID", ""),
            CLIENT_SECRET=env.get("TWITCH_CLIENT_SECRET", ""),
            BOT_USERNAME=username,
            BOT_ID=env.get("VOXER_BOT_ID", ""),
            ACCESS_TOKEN=env.get("TWITCH_ACCESS_TOKEN", ""),
            REFRESH_TOKEN=env.get("TWITCH_REFRESH_TOKEN", ""),
            DB_PATH=env.get("VOXER_DB_PATH", "data/voices.json"),
            AUDIO_DIR=env.get("VOXER_AUDIO_DIR", "audio"),
            EMOTES_DB_PATH=env.get("VOXER_EMOTES_DB_PATH", "emotes/emotes.db"),
            TIMESTAMPS_DB_PATH=env.get(
                "VOXER_TIMESTAMPS_DB_PATH", "data/timestamps.json"
            ),
            MESSAGES_PATH=env.get("VOXER_MESSAGES_PATH", "data/messages.json"),
            VOICES_DIR=env.get("VOXER_VOICES_DIR", "voices"),
            SOUNDS_DIR=env.get("VOXER_SOUNDS_DIR", str(DEFAULT_SOUNDS_DIR)),
            SERVER_HOST=env.get("VOXER_SERVER_HOST", "127.0.0.1").strip(),
            SERVER_PORT=integer("VOXER_SERVER_PORT", 8080, maximum=65535),
            OVERLAY_TOKEN=env.get("VOXER_OVERLAY_TOKEN", ""),
            ALLOWED_HOSTS=tuple(csv("VOXER_ALLOWED_HOSTS")),
            TRUSTED_PROXIES=tuple(csv("VOXER_TRUSTED_PROXIES")),
            MAX_WS_CLIENTS=integer("VOXER_MAX_WS_CLIENTS", 8, maximum=256),
            MAX_PENDING_PER_CLIENT=integer(
                "VOXER_MAX_PENDING_PER_CLIENT", 64, maximum=1024
            ),
            WS_SEND_TIMEOUT=integer("VOXER_WS_SEND_TIMEOUT", 5),
            AUDIO_SWEEP_INTERVAL_SECS=integer("VOXER_AUDIO_SWEEP_INTERVAL_SECS", 300),
            AUDIO_MAX_AGE_SECS=integer("VOXER_AUDIO_MAX_AGE_SECS", 300),
            OAUTH_HOST=env.get("VOXER_OAUTH_HOST", "localhost"),
            OAUTH_PORT=port,
            OAUTH_REDIRECT_URL=env.get("VOXER_OAUTH_REDIRECT_URL", "")
            or f"http://localhost:{port}/oauth/callback",
            TOKEN_FILE=env.get("VOXER_TOKEN_FILE", "data/tokens.json"),
            SETTINGS_FILE=env.get("VOXER_SETTINGS_FILE", "data/settings.json"),
            SCHEDULER_INITIAL_DELAY=integer(
                "VOXER_SCHEDULER_INITIAL_DELAY", 10, minimum=0
            ),
            SCHEDULER_EMPTY_RETRY_DELAY=integer(
                _env_name_in_use(
                    "VOXER_SCHEDULER_EMPTY_RETRY_DELAY",
                    "VOXER_SCHEDULER_INTERVAL",
                    env=env,
                ),
                600,
            ),
            MESSAGE_QUEUE_MAXSIZE=integer("VOXER_MESSAGE_QUEUE_MAXSIZE", 20),
            MAX_MESSAGE_CHARS=integer("VOXER_MAX_MESSAGE_CHARS", 500, maximum=500),
            MAX_SPEECH_CHARS=integer("VOXER_MAX_SPEECH_CHARS", 1000, maximum=2000),
            MAX_MESSAGE_AGE_SECS=integer("VOXER_MAX_MESSAGE_AGE_SECS", 60),
            USER_COOLDOWN_SECS=integer("VOXER_USER_COOLDOWN_SECS", 2, minimum=0),
            ANNOUNCE_WINDOW_SECS=integer("VOXER_ANNOUNCE_WINDOW_SECS", 300),
            NO_ANNOUNCE_USERS=frozenset(
                user.lower() for user in csv("VOXER_NO_ANNOUNCE_USERS", username)
            ),
            EMOTE_SOUND_PATHS=csv(
                "VOXER_EMOTE_SOUND_PATHS", "emotes/slack-message.mp3,emotes/discord.mp3"
            ),
            LOG_LEVEL=env.get("VOXER_LOG_LEVEL", "INFO").upper(),
            sources=sources or {},
        )


SAVED_KEYS = frozenset(
    {
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "TWITCH_BOT_USERNAME",
        "VOXER_BOT_ID",
        "VOXER_OVERLAY_TOKEN",
    }
)


def read_settings(path: str | Path) -> dict[str, str]:
    data = read_tokens(path)
    if not data:
        return {}
    if data.get("version") != 1 or not isinstance(data.get("values"), dict):
        raise ConfigError("Unsupported settings file; restore a version-1 backup")
    values = data["values"]
    if any(
        key not in SAVED_KEYS or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ConfigError("Invalid saved settings")
    return values


def save_settings(path: str | Path, values: Mapping[str, str]) -> None:
    if any(
        key not in SAVED_KEYS or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ConfigError("Invalid saved settings")
    write_json_atomic(path, {"version": 1, "values": dict(values)})


def load_settings(
    env: Mapping[str, str] | None = None, *, dotenv_path: str | Path | None = ".env"
) -> RuntimeSettings:
    explicit = dict(os.environ if env is None else env)
    dotenv = (
        {
            key: value
            for key, value in dotenv_values(dotenv_path).items()
            if value is not None
        }
        if dotenv_path is not None
        else {}
    )
    overrides = {**dotenv, **explicit}
    path = overrides.get("VOXER_SETTINGS_FILE", "data/settings.json")
    saved = read_settings(path)
    values = {**saved, **overrides}
    # A legacy username override must not retain a different saved numeric ID.
    if (
        "TWITCH_BOT_USERNAME" in overrides
        and overrides["TWITCH_BOT_USERNAME"] != saved.get("TWITCH_BOT_USERNAME")
        and "VOXER_BOT_ID" not in overrides
    ):
        values.pop("VOXER_BOT_ID", None)
    sources = {key: "saved" for key in saved}
    sources.update({key: "dotenv" for key in dotenv})
    sources.update({key: "environment" for key in explicit})
    return RuntimeSettings.from_env(values, sources=sources)


# No environment parsing, dotenv mutation, or provider work at module import.
# Compatibility for old imports and monkeypatched module attributes.
def __getattr__(name: str) -> Any:
    if name in RuntimeSettings.__dataclass_fields__ or name == "_REQUIRED_CREDENTIALS":
        return getattr(RuntimeSettings.from_env(os.environ), name)
    raise AttributeError(name)


def parse_redirect_url(url: str) -> tuple[str | None, str]:
    parsed = urlparse(url if "//" in url else f"//{url}")
    path = parsed.path.strip("/") or "oauth/callback"
    return (
        None if parsed.hostname in (None, "localhost", "127.0.0.1") else parsed.netloc,
        path,
    )


def validate_redirect_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        parsed.port
    except ValueError:
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL has an invalid host or port"
        ) from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(
            f"VOXER_OAUTH_REDIRECT_URL must be a full http(s) URL, got: {url!r}"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL cannot contain credentials, a query or a fragment"
        )
    if parsed.hostname not in ("localhost", "127.0.0.1") and parsed.scheme != "https":
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL must use https:// for a non-localhost host"
        )


def validate_credentials(settings=None) -> None:
    settings = settings or sys.modules[__name__]
    missing = [
        name for name, value in settings._REQUIRED_CREDENTIALS if not value.strip()
    ]
    if missing:
        raise SetupRequired(
            "Required environment variables are not set: " + ", ".join(missing)
        )


def validate_overlay_access(host: str, token: str) -> None:
    if not host:
        raise ConfigError("VOXER_SERVER_HOST must not be empty")
    try:
        is_loopback = ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if token and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ConfigError(
            "VOXER_OVERLAY_TOKEN must be 32-256 URL-safe characters; generate it with secrets.token_urlsafe(32)"
        )
    if not is_loopback and not token:
        raise ConfigError(
            "VOXER_OVERLAY_TOKEN is required when VOXER_SERVER_HOST listens beyond localhost"
        )


def validate_trusted_proxies(addresses: tuple[str, ...]) -> None:
    for address in addresses:
        try:
            network = ip_network(address, strict=False)
        except ValueError:
            raise ConfigError(
                "VOXER_TRUSTED_PROXIES must contain IP addresses or CIDR networks"
            ) from None
        if network.prefixlen == 0:
            raise ConfigError("VOXER_TRUSTED_PROXIES cannot trust every address")


def validate_oauth_listener(settings=None) -> None:
    settings = settings or sys.modules[__name__]
    validate_redirect_url(settings.OAUTH_REDIRECT_URL)
    redirect = urlparse(settings.OAUTH_REDIRECT_URL)
    if not redirect.path or redirect.path.rstrip("/") in {
        "/setup",
        "/status",
        "/healthz",
        "/readyz",
        "/oauth",
        "/callback",
        "/credentials",
        "/confirm",
        "/control/open",
    }:
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL must include a callback path outside the reserved control routes"
        )
    if redirect.scheme == "http" and (redirect.port or 80) != settings.OAUTH_PORT:
        raise ConfigError(
            "VOXER_OAUTH_REDIRECT_URL port must match VOXER_OAUTH_PORT for the local authorization server"
        )
    if settings.SERVER_PORT == settings.OAUTH_PORT:
        raise ConfigError("VOXER_SERVER_PORT and VOXER_OAUTH_PORT must differ")


def validate_config(settings=None) -> None:
    settings = settings or sys.modules[__name__]
    validate_credentials(settings)
    if not settings.BOT_USERNAME:
        raise ConfigError(
            "TWITCH_BOT_USERNAME is not set: authorize and confirm your streaming account"
        )
    if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", settings.BOT_USERNAME):
        raise ConfigError("TWITCH_BOT_USERNAME must be a Twitch login name")
    validate_oauth_listener(settings)
    validate_overlay_access(settings.SERVER_HOST, settings.OVERLAY_TOKEN)
    validate_trusted_proxies(settings.TRUSTED_PROXIES)
