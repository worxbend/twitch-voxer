"""Unit tests for runtime-directory preparation in voxer.app.

Only ``_prepare_runtime_dirs`` is covered here.  The rest of ``app.run()``
opens a Twitch WebSocket and downloads a speech model, so it cannot be
exercised from a test; the directory work was extracted into its own function
precisely so that this part can be.
"""

from pathlib import Path

import asyncio
import os
import subprocess
import sys

import pytest

from voxer import app
from voxer.app import _prepare_runtime_dirs
from voxer.config import RuntimeSettings
from voxer.token_store import TokenFileBusyError, TokenFileLock


@pytest.mark.asyncio
async def test_second_process_cannot_sweep_or_load_model(tmp_path, monkeypatch):
    token_file = tmp_path / "tokens.json"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    clip = audio_dir / "playing.mp3"
    clip.write_bytes(b"playing")
    settings = RuntimeSettings.from_env(
        {"VOXER_TOKEN_FILE": str(token_file), "VOXER_AUDIO_DIR": str(audio_dir)}
    )
    monkeypatch.setattr(app, "validate_config", lambda _: None)
    monkeypatch.setattr(app, "setup_logging", lambda _: None)
    monkeypatch.setattr(app, "TTSService", lambda **_: pytest.fail("loaded model"))
    code = (
        "import sys; from voxer.token_store import TokenFileLock; "
        "lock=TokenFileLock(sys.argv[1]); lock.acquire(); "
        "print('locked', flush=True); sys.stdin.read()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(token_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )
    try:
        assert await asyncio.to_thread(process.stdout.readline) == "locked\n"
        with pytest.raises(TokenFileBusyError, match="another process"):
            await app.run(settings)
        assert clip.read_bytes() == b"playing"
    finally:
        process.communicate(timeout=5)
    with TokenFileLock(token_file):
        pass


class TestPrepareRuntimeDirs:
    def test_creates_nested_audio_dir(self, tmp_path: Path) -> None:
        """A multi-level VOXER_AUDIO_DIR (e.g. /data/voxer/audio) must work.

        Before this fix the audio directory was created without
        ``parents=True``, so a configured path whose parent did not exist yet
        raised FileNotFoundError at startup, before a single log line.
        """
        audio_dir = tmp_path / "data" / "voxer" / "audio"
        _prepare_runtime_dirs(audio_dir, tmp_path / "data" / "tokens.json")
        assert audio_dir.is_dir()

    def test_creates_nested_token_file_parent(self, tmp_path: Path) -> None:
        """The token file itself is written later by twitchio; only its
        directory is created here, parents included."""
        token_file = tmp_path / "state" / "voxer" / "tokens.json"
        _prepare_runtime_dirs(tmp_path / "audio", token_file)
        assert token_file.parent.is_dir()
        assert not token_file.exists()

    def test_existing_dirs_are_not_an_error(self, tmp_path: Path) -> None:
        """Every run after the first finds both directories already there."""
        audio_dir = tmp_path / "audio"
        token_file = tmp_path / "data" / "tokens.json"
        _prepare_runtime_dirs(audio_dir, token_file)
        _prepare_runtime_dirs(audio_dir, token_file)
        assert audio_dir.is_dir()
        assert token_file.parent.is_dir()

    def test_existing_audio_contents_are_kept(self, tmp_path: Path) -> None:
        """Preparing the directories must never wipe what is already inside
        them — the boot-time sweep of stale MP3s is a separate, deliberate
        step in run()."""
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        leftover = audio_dir / "keep.mp3"
        leftover.write_bytes(b"")
        _prepare_runtime_dirs(audio_dir, tmp_path / "data" / "tokens.json")
        assert leftover.exists()
