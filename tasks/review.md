# Repository review: easy setup and fast stream startup

Reviewed revision: `9c2d646`. Application changes: none. Skills used: Caveman review/exploration guidance, repository context, streaming integrations, Docker optimization, security and hardening, planning, and browser-testing guidance. No subagent tools are available in this session; the review was performed directly, not delegated.

## Verdict

Keep the existing Python 3.14, TwitchIO, Starlette, Supertonic, and JavaScript stack. The audio pipeline already has useful resource bounds and cleanup. The largest benefit is a better setup/startup lifecycle, not a rewrite.

Browser OAuth and persistent, automatically refreshed tokens **already exist**. What is missing is asking for configuration, opening the **host** browser from a Docker-first workflow, and reliably reporting when streaming features are ready.

## Scope and verification

Inspected all first-party Python modules, both overlay pages and their JavaScript/CSS, configuration, Docker packaging, CI, README, and asset/vendor manifests. Reviewed authentication/startup tests and test coverage boundaries; executed the entire existing Python suite. Vendored rendering libraries and binary assets were checked through their manifests and runtime loading, not audited line by line. Selected locked TwitchIO 3.2.2 and Supertonic 1.2.3 internals were inspected to verify lifecycle and model-cache assumptions.

Results:

- `uv sync --locked --dev`: successful; created an ignored local `.venv`.
- `uv run --frozen pytest -q`: **625 passed**, two dependency deprecation warnings.
- `node tests/test_overlay.cjs`: **9 passed**.
- `uv run --frozen --with playwright python tests/browser_overlay.py`: **four combinations passed**: full/simple overlays with ordinary/reduced motion. Checks include autoplay recovery, layout, MP3 playback, owned acknowledgments, and console errors. Screenshots: `/tmp/voxer-overlay-browser-hr9lhpna`.
- `docker build -t twitch-voxer-review:9c2d646 .`: successful on **linux/amd64**. The initial attempt timed out during export; a cached retry completed. No running application container was left behind.
- Docker reports **1,132,395,504 bytes** for the image. `docker history` attributes approximately **466 MB** to the ffmpeg installation layer and **217 MB** to the virtual environment. This is not a measured registry transfer size.
- Network-disabled, read-only container import smoke: successful; application import approximately **1.23 seconds** in that one run. It is not an end-to-end startup benchmark. The image has no `voxer-fetch-emotes` executable and no `/data/messages.json`.
- `docker compose --env-file .env.example config --quiet`: stops at the required, empty `VOXER_OVERLAY_TOKEN`. This confirms a fresh example cannot start without manual secret preparation; it is intentional validation, not a YAML syntax defect.
- Offline reproductions confirmed that a temporary token validation timeout falls through to browser authorization, and a subscription result containing HTTP 503 returns normally after one attempt.

Not verified: live Twitch authorization/refresh/EventSub delivery, real model download and inference, OBS's embedded browser, ARM64 builds, dependency/image vulnerability scanning, or cold/warm stream-ready timings. No real credentials were requested, no Twitch browser authorization was opened, and no chat was sent. No linters or type checker were run during this review.

## Findings — Caveman format

P1 = fix before relying on quick stream startup. P2 = usability, distribution, or resilience improvement. These are whole-codebase observations, not regressions attributed to a new diff.

- `voxer/app.py:L142`, `voxer/config.py:L399-L431`, `docker-compose.yml:L56-L64`: **P1 goal gap:** first start rejects missing settings; no configuration questions exist. Add a setup-only mode that asks for Client ID/Secret and generates the overlay credential automatically.
- `voxer/bot.py:L451-L465`: **P1 goal gap:** `webbrowser.open()` executes inside the container, not on the desktop. Open the setup/authentication page from a host launcher; retain a manual URL fallback.
- `voxer/app.py:L167-L169`, `voxer/app.py:L225-L231`, `voxer/app.py:L307`: **P1 startup bottleneck:** synchronous model loading/download precedes HTTP and OAuth startup. Start the lightweight control server first; prepare TTS off the event loop and expose progress.
- `voxer/bot.py:L347-L353`, `voxer/bot.py:L411-L465`: **P1 bug:** all stored-token admission errors become an authorization problem, including temporary provider/network failures. Preserve the grant and retry transient failures; request consent only for rejected/revoked/under-scoped grants.
- `voxer/bot.py:L702-L713`, `voxer/app.py:L125-L128`: **P1 bug:** non-409 subscription errors only log warnings; startup then starts the scheduler without ensuring chat reception works. Return per-subscription status, retry transient failures, and withhold readiness if mandatory chat subscription fails.
- `voxer/app.py:L155`, `voxer/bot.py:L330-L333`: **P1 risk:** boot deletes shared audio before acquiring token ownership; a second process can interrupt a running stream before it is rejected. Acquire exclusive runtime ownership before cleanup or model initialization.
- `voxer/oauth.py:L182-L188`, `voxer/bot.py:L633-L656`: **P2 risk:** callback reports success before asynchronous token admission/persistence, and the authorized event is set before saving. Report durable completion separately from provider consent; do not promise saved tokens after a write failure.
- `voxer/bot.py:L656`, `voxer/app.py:L128`: **P2 risk:** first authorization registers subscriptions from both the callback listener and startup, creating duplicate work. Give one lifecycle coordinator ownership of subscription setup.
- `voxer/server.py:L299-L300`, `Dockerfile:L73-L74`: **P1 operational gap:** health only means HTTP responds, not that Twitch chat can be heard. Keep `/healthz` for liveness; add secret-free readiness and component status for the launcher.
- `docker-compose.yml:L72`, `Dockerfile:L41-L42`: **P1 setup friction:** bind-mounted state requires host permissions compatible with UID 1000. Default to an initialized named state volume; retain bind mounts as an advanced override with permission checks.
- `docker-compose.yml:L30-L35`, `.github/workflows/ci.yml:L21-L58`: **P2 distribution gap:** the supplied image name is local and CI neither builds nor publishes containers. Add tested release images and make routine starts independent of builds or registry updates.
- `Dockerfile:L35-L37`: **P2 measured packaging cost:** general-purpose ffmpeg adds approximately 466 MB in this build. Evaluate a pinned audio-only ffmpeg build or another measured compatible encoder; verify WAV-to-MP3 output before switching.
- `voxer/tts.py:L64`, `docker-compose.yml:L82-L84`: **P2 performance gap:** model preparation is implicit and ONNX thread counts are left at upstream defaults. Add an explicit model-preparation command and benchmark a stream-friendly CPU preset against real inference and the PID limit.
- `voxer/fetch_emotes.py:L64`, `voxer/fetch_emotes.py:L413-L422`, `docker-compose.yml:L75-L78`: **P2 configuration split:** emote recovery uses another callback on port 1337 and the documented Docker workflow needs a host Python installation. Reuse the primary authorization flow and run cache refresh through the same image.
- `Dockerfile:L29`, `pyproject.toml:L31-L33`: **P2 packaging gap:** `--no-install-project` omits declared console scripts from the image. Install the project non-editably or intentionally support all operations through `python -m` commands.
- `voxer/app.py:L233-L239`, `data/messages.json:L3-L19`: **P2 unsafe default for a general install:** scheduled community messages start automatically and include a repository-specific Discord invitation. Seed an empty message list and make posting opt-in; never replace existing operator content.
- `voxer/sounds/README.md:L42-L48`: **P2 distribution risk:** 32 bundled user-added clips have no supplied source/license information. Confirm redistribution rights before publishing a public image, or provide those clips through private custom mounts.
- `tests/test_app.py:L1-L6`: **P1 verification gap:** application tests cover directory creation only; auth, readiness, warm boot, and Docker shutdown orchestration lack lifecycle tests. Inject component factories and test the startup state machine with fake providers.

## What is worth preserving

- `voxer/oauth.py`: fixed callback URI, browser-bound single-use state, bounded pending exchanges, client/account/scope verification.
- `voxer/token_store.py`: private atomic writes, fsync, and OS locking for one refresh owner.
- `voxer/server.py`: protected overlay resources, host/origin checks, independent overlay credentials, concurrent bounded delivery, per-client audio ownership, and TTL cleanup.
- `voxer/handler.py`, `voxer/tts.py`: serialized inference, bounded text/work, cancellation-aware file cleanup, and timed ffmpeg subprocesses.
- `voxer/static/overlay.js`: bounded playback, autoplay recovery, stale-work handling, socket ownership, and reconnect teardown.
- Docker: non-root runtime, read-only root filesystem, dropped capabilities, init process, persistent model cache, and host-loopback port publishing.

Do not remove these protections to make setup easier. A localhost-published Docker port still uses a wildcard bind **inside** the container; automatic credential generation is the solution, not turning off overlay authentication.

## Improvements after the core startup work

- Keep the simple overlay as the recommended low-overhead OBS source; keep full visuals optional. Existing browser checks passed, so a graphics rewrite has no demonstrated value for this goal.
- Replace the broad `"bot" in username` exclusion with configurable exact bot/ignore lists if legitimate viewers are being skipped (`voxer/textnorm.py:L354-L357`). Existing behavior is documented, so treat this as a product choice, not an unannounced fix.
- Consider a bounded priority/reserved-capacity policy for raid/subscription announcements. Today system events are deliberately dropped when the common queue is full; avoid replacing that with unbounded waiting tasks.
- Cache validated account identity to avoid the separate `get_user_id()` bootstrap client on each boot. Confirm cached identity against token validation; do not reuse an identity from different credentials.
- Explicitly version the selected model and resource locations. The locked upstream already pins model revisions and stores the default model under `/home/voxer/.cache/supertonic3`; the existing cache mount covers it. Missing model caching is **not** a current bug.
- Pin Python/uv images by digest with automated update PRs; use `uv sync --locked` to detect stale project metadata. Avoid Alpine until native ONNX/numpy compatibility is demonstrated.
- Separate immutable static-asset caching from secret-bearing page responses if repeat browser-source loads warrant it. Keep auth enforcement and no-store on setup/OAuth/token-exchange responses.
- Preserve current external media fallback behavior; remote avatars/emotes are different from executable CDN dependencies, which are already vendored.
- Keep the audio pipeline sequential until measurements show a bottleneck. Multiple model workers increase CPU competition with games and OBS, and require independent engine ownership.

## Reference boundaries for implementation

Recheck provider requirements when changing OAuth scopes or subscription behavior:

- Twitch authorization code grant: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#authorization-code-grant-flow
- Token validation: https://dev.twitch.tv/docs/authentication/validate-tokens/
- Token refresh: https://dev.twitch.tv/docs/authentication/refresh-tokens/
- EventSub Conduits: https://dev.twitch.tv/docs/eventsub/handling-conduit-events/
- Subscription types: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/

The current `AutoBot` uses **Conduits**, not a plain direct EventSub WebSocket subscription flow. Its locked `multi_subscribe` implementation uses app access tokens plus user grants. Keep TwitchIO's transport ownership; do not add a second competing reconnect/refresh loop. These links are implementation references, not a claim that live provider behavior was exercised during this review.
