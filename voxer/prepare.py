"""Prepare the pinned Supertonic cache before a stream, with Voxer stopped."""

from pathlib import Path

from .config import load_settings
from .token_store import TokenFileBusyError, TokenFileLock


def main() -> None:
    settings = load_settings()
    try:
        with TokenFileLock(settings.TOKEN_FILE):
            from .tts import TTSService

            TTSService(voices_dir=Path(settings.VOICES_DIR))
            print("Speech model prepared in the persistent cache.")
    except TokenFileBusyError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
