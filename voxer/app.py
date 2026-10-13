"""Runtime composition root; bootstrap owns setup before this module is loaded."""

import asyncio
import logging
from pathlib import Path
from collections.abc import Callable

from twitchio.web import AiohttpAdapter

from .bot import VoxBot, get_user_id
from .config import RuntimeSettings, load_settings, validate_config
from .handler import MessageHandler
from .lifecycle import StartupStatus
from .log import setup_logging
from .models import QueuedMessage
from .scheduler import Scheduler
from .server import AudioServer, reap_audio, sweep_audio_dir
from .soundboard import load_sounds
from .setup import SetupAdapter
from .stores import AnnounceTracker, EmoteStore, VoiceStore
from .token_store import TokenFileLock, run_durable
from .tts import TTSService

LOGGER = logging.getLogger(__name__)


def _prepare_runtime_dirs(audio_dir: Path, token_file: Path) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)
    token_file.parent.mkdir(parents=True, exist_ok=True)


async def _authorize_and_subscribe(
    bot: VoxBot,
    bot_id: str,
    status: StartupStatus | None = None,
    *,
    on_chat_ready: Callable[[], None] | None = None,
) -> None:
    await bot.wait_until_ready()
    await bot.ensure_authorized()
    if status is not None:
        status.authorized = True
    if on_chat_ready is None:
        await bot.subscribe_for(bot_id)
    else:
        await bot.subscribe_for(bot_id, on_chat_ready=on_chat_ready)


async def run(
    settings: RuntimeSettings | None = None,
    *,
    owner: TokenFileLock | None = None,
    adapter: AiohttpAdapter | None = None,
    status: StartupStatus | None = None,
) -> None:
    """Own state before any cleanup; a bootstrap owner may lend its lease."""
    settings = settings or load_settings()
    setup_logging(settings.LOG_LEVEL)
    validate_config(settings)
    if owner is not None:
        if owner.path.resolve() != Path(settings.TOKEN_FILE).resolve():
            raise ValueError("Runtime owner must hold the configured token file")
        owner.acquire()
        await _run_owned(settings, owner, adapter, status or StartupStatus())
    else:
        with TokenFileLock(settings.TOKEN_FILE) as lease:
            await _run_owned(settings, lease, adapter, status or StartupStatus())


async def _run_owned(
    s: RuntimeSettings,
    owner: TokenFileLock,
    adapter: AiohttpAdapter | None,
    status: StartupStatus,
) -> None:
    audio_dir = Path(s.AUDIO_DIR)
    _prepare_runtime_dirs(audio_dir, Path(s.TOKEN_FILE))
    sweep_audio_dir(audio_dir)
    queue: asyncio.Queue[QueuedMessage] = asyncio.Queue(maxsize=s.MESSAGE_QUEUE_MAXSIZE)
    status.stage = "preparing_model"
    # Native model loading must not block the setup/status listener.
    tts = await run_durable(TTSService, voices_dir=Path(s.VOICES_DIR))
    status.model = True
    server = AudioServer(
        audio_dir=audio_dir,
        host=s.SERVER_HOST,
        port=s.SERVER_PORT,
        send_timeout=s.WS_SEND_TIMEOUT,
        overlay_token=s.OVERLAY_TOKEN,
        allowed_hosts=s.ALLOWED_HOSTS,
        max_clients=s.MAX_WS_CLIENTS,
        max_pending_per_client=s.MAX_PENDING_PER_CLIENT,
        audio_max_age=s.AUDIO_MAX_AGE_SECS,
        trusted_proxies=s.TRUSTED_PROXIES,
        readiness=lambda: status.ready,
    )
    status.overlay_connected = lambda: server.has_clients
    voice_store = VoiceStore(s.DB_PATH, tts.voice_names)
    announce_tracker = AnnounceTracker(s.TIMESTAMPS_DB_PATH, s.ANNOUNCE_WINDOW_SECS)
    emote_store = EmoteStore(s.EMOTES_DB_PATH)
    await asyncio.gather(
        emote_store.load(), voice_store.load(), announce_tracker.load()
    )
    handler = MessageHandler(
        tts=tts,
        voice_store=voice_store,
        announce_tracker=announce_tracker,
        emote_store=emote_store,
        audio_dir=audio_dir,
        broadcast=server.broadcast,
        message_queue=queue,
        emote_sound_paths=s.EMOTE_SOUND_PATHS,
        sound_paths=load_sounds(Path(s.SOUNDS_DIR)),
        no_announce_users=s.NO_ANNOUNCE_USERS,
        max_text_chars=s.MAX_MESSAGE_CHARS,
        max_speech_chars=s.MAX_SPEECH_CHARS,
        max_message_age_secs=s.MAX_MESSAGE_AGE_SECS,
        overlay_available=lambda: server.has_clients,
    )
    status.stage = "connecting_twitch"
    bot_id = s.BOT_ID or await get_user_id(s.BOT_USERNAME, s)
    async with VoxBot(
        bot_id=bot_id,
        message_queue=queue,
        token_owner=owner,
        settings=s,
        adapter=adapter,
    ) as bot:
        if adapter is not None:
            adapter.client = bot
            # A bootstrap listener is already running. It stays attached to
            # the bot for callback validation, admission, and durable saves.
            if isinstance(adapter, SetupAdapter):
                adapter.attach_bot(bot)
        scheduler = Scheduler(
            send_chat=bot.send_chat,
            messages_path=Path(s.MESSAGES_PATH),
            empty_retry_delay=s.SCHEDULER_EMPTY_RETRY_DELAY,
            initial_delay=s.SCHEDULER_INITIAL_DELAY,
        )
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(bot.start(with_adapter=adapter is None))
                group.create_task(server.serve())
                group.create_task(
                    reap_audio(
                        audio_dir,
                        s.AUDIO_SWEEP_INTERVAL_SECS,
                        s.AUDIO_MAX_AGE_SECS,
                        server.outstanding_files,
                    )
                )
                activated = False

                def activate_chat() -> None:
                    nonlocal activated
                    if activated:
                        return
                    activated = True
                    group.create_task(handler.process_queue())
                    group.create_task(scheduler.run())
                    status.chat = True
                    status.stage = "ready"
                    LOGGER.info("Ready — connect the OBS browser source to hear chat")

                await _authorize_and_subscribe(
                    bot, bot_id, status, on_chat_ready=activate_chat
                )
                activate_chat()
                status.optional_failures = getattr(
                    bot, "optional_subscription_failures", 0
                )
        finally:
            status.chat = False
            try:
                await asyncio.gather(announce_tracker.flush(), voice_store.flush())
            except OSError:
                LOGGER.error("Could not checkpoint persistent state")


def main() -> None:
    """Compatibility entry point; use lightweight browser setup for all starts."""
    from .bootstrap import main as bootstrap_main

    bootstrap_main()


if __name__ == "__main__":
    main()
