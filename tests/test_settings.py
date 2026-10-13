"""Private settings precedence and import isolation."""

import os
import subprocess
import sys

import pytest

from voxer.config import (
    ConfigError,
    SetupRequired,
    load_settings,
    save_settings,
    validate_credentials,
    validate_oauth_listener,
)


def test_precedence_and_no_stale_identity_defaults(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(
        path,
        {
            "TWITCH_CLIENT_ID": "saved",
            "TWITCH_BOT_USERNAME": "channel",
            "VOXER_OVERLAY_TOKEN": "a" * 43,
        },
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("TWITCH_CLIENT_ID=dotenv\n")
    s = load_settings(
        {"VOXER_SETTINGS_FILE": str(path), "TWITCH_CLIENT_ID": "environment"},
        dotenv_path=dotenv,
    )
    assert s.CLIENT_ID == "environment"
    assert s.sources["TWITCH_CLIENT_ID"] == "environment"
    assert s.BOT_USERNAME == "channel"
    assert s.NO_ANNOUNCE_USERS == frozenset({"channel"})
    assert s.OVERLAY_TOKEN == "a" * 43
    assert path.stat().st_mode & 0o777 == 0o600
    assert "saved" not in repr(s)


def test_missing_credentials_are_typed_and_explicit_blank_wins(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(path, {"TWITCH_CLIENT_ID": "saved", "TWITCH_CLIENT_SECRET": "saved"})
    s = load_settings(
        {"VOXER_SETTINGS_FILE": str(path), "TWITCH_CLIENT_ID": ""}, dotenv_path=None
    )
    with pytest.raises(SetupRequired, match="TWITCH_CLIENT_ID"):
        validate_credentials(s)


def test_bad_explicit_override_is_not_replaced(tmp_path):
    with pytest.raises(ConfigError, match="VOXER_SERVER_PORT"):
        load_settings(
            {
                "VOXER_SETTINGS_FILE": str(tmp_path / "absent"),
                "VOXER_SERVER_PORT": "bad",
            },
            dotenv_path=None,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"VOXER_OAUTH_REDIRECT_URL": "http://localhost:4344/oauth/callback"},
        {"VOXER_SERVER_PORT": "4343"},
        {"VOXER_OAUTH_REDIRECT_URL": "https://bot.example"},
        {"VOXER_OAUTH_REDIRECT_URL": "http://localhost:4343/setup"},
        {"VOXER_OAUTH_REDIRECT_URL": "http://localhost:4343/healthz/"},
    ],
)
def test_listener_configuration_fails_before_enrollment(overrides):
    from voxer.config import RuntimeSettings

    settings = RuntimeSettings.from_env(overrides)
    with pytest.raises(ConfigError):
        validate_oauth_listener(settings)


def test_import_config_does_not_parse_environment_or_import_model():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import voxer.config; assert 'supertonic' not in sys.modules",
        ],
        env={**os.environ, "VOXER_SERVER_PORT": "not-a-port"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
