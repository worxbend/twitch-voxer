"""Actual process signals during setup, without Twitch or speech downloads."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["voxer.bootstrap", "voxer.app"])
def test_sigterm_setup_process_exits_cleanly(tmp_path, module):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1]),
        "VOXER_OAUTH_PORT": str(port),
        "VOXER_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "VOXER_TOKEN_FILE": str(tmp_path / "tokens.json"),
        "TWITCH_BOT_USERNAME": "",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(
                    f"http://localhost:{port}/healthz", timeout=1
                ):
                    break
            except urllib.error.URLError:
                time.sleep(0.02)
        else:
            raise AssertionError("No control listener")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stdout + stderr
        assert "Traceback" not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
