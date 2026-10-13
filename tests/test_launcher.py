"""Launcher contract with fake Docker/browser commands; never touches the daemon."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    shutil.copy2(Path(__file__).parents[1] / "voxer.sh", root / "voxer.sh")
    (root / "docker-compose.yml").write_text("services: {}\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "calls"
    docker = tools / "docker"
    docker.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1" == info && "${BROKEN:-}" == yes ]]; then exit 1; fi
if [[ "$1 $2" == 'image inspect' && "${IMAGE_MISSING:-}" == yes ]]; then exit 1; fi
if [[ "$*" == *'ps --status running'* ]]; then printf '%s' "${RUNNING:-}"; exit 0; fi
if [[ "$*" == *'config --images'* ]]; then printf '%s\\n' "${COMPOSE_IMAGE:-${VOXER_IMAGE:-twitch-voxer:local}}"; exit 0; fi
case "${!#}" in
  stage) [[ "${UNAVAILABLE:-}" != yes ]] || exit 1; printf '%s\\n' "${STAGE:-ready}" ;;
  setup-url) printf 'http://localhost:4343/setup?key=synthetic-private-link\\n' ;;
  status) printf '{"stage":"%s"}\\n' "${STAGE:-ready}" ;;
esac
""")
    docker.chmod(0o755)
    for name in ("xdg-open", "sleep"):
        tool = tools / name
        tool.write_text(
            '#!/usr/bin/env bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$CALLS"\n'
        )
        tool.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CALLS": str(log),
        "DISPLAY": ":synthetic",
    }

    def run(command="start", **overrides):
        result = subprocess.run(
            [str(root / "voxer.sh"), command],
            cwd="/",
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, log.read_text()

    return run


def test_cached_start_from_other_directory_never_pulls_builds_or_opens(launcher):
    result, calls = launcher(RUNNING="container")
    assert result.returncode == 0, result.stderr
    assert "checkout with spaces/docker-compose.yml" in calls
    assert "up -d --no-build --pull never" in calls
    assert "build voxer" not in calls
    assert "pull voxer" not in calls
    assert "xdg-open" not in calls


def test_fresh_start_opens_host_setup_after_listener(launcher):
    result, calls = launcher(STAGE="setup_required", IMAGE_MISSING="yes")
    assert result.returncode == 0, result.stderr
    assert "build voxer" in calls
    assert (
        calls.index("voxer.control stage")
        < calls.index("voxer.control setup-url")
        < calls.index("xdg-open")
    )
    assert "synthetic-private-link" not in result.stdout


def test_repeated_start_never_reopens_browser(launcher):
    result, calls = launcher(STAGE="authorization_required", RUNNING="container")
    assert result.returncode == 0
    assert "xdg-open" not in calls


def test_control_timeout_is_bounded(launcher):
    result, calls = launcher(UNAVAILABLE="yes", RUNNING="container")
    assert result.returncode != 0
    assert "30 seconds" in result.stderr
    assert calls.count("voxer.control stage") == 30
    assert "xdg-open" not in calls


def test_explicit_setup_prints_headless_fallback(launcher):
    result, calls = launcher("setup", DISPLAY="", WAYLAND_DISPLAY="")
    assert result.returncode == 0
    assert "single-use" in result.stdout
    assert "setup?key=synthetic" in result.stdout
    assert "xdg-open" not in calls


def test_registry_image_is_only_acquired_when_missing(launcher):
    result, calls = launcher(
        VOXER_IMAGE="example.invalid/voxer:v1", IMAGE_MISSING="yes"
    )
    assert result.returncode == 0
    assert "pull voxer" in calls
    assert "build voxer" not in calls


def test_compose_dotenv_image_selection_matches_launcher(launcher):
    result, calls = launcher(
        COMPOSE_IMAGE="example.invalid/voxer:v2", IMAGE_MISSING="yes"
    )
    assert result.returncode == 0
    assert "image inspect example.invalid/voxer:v2" in calls
    assert "pull voxer" in calls
    assert "build voxer" not in calls


def test_prepare_uses_stopped_service_command(launcher):
    result, calls = launcher("prepare")
    assert result.returncode == 0
    assert "run --rm --no-deps --entrypoint python voxer -m voxer.prepare" in calls
    assert "xdg-open" not in calls


def test_daemon_failure_is_actionable(launcher):
    result, calls = launcher(BROKEN="yes")
    assert result.returncode != 0
    assert "permissions" in result.stderr
    assert "up -d" not in calls


def test_unknown_command_is_not_evaluated(launcher):
    result, calls = launcher("$(touch /tmp/voxer-invalid-test)")
    assert result.returncode != 0
    assert "Usage:" in result.stderr
    assert "touch" not in calls
