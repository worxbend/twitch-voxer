# Implementation plan: configure once, start quickly for each stream

Status: **core Docker/browser workflow implemented; release and live/performance acceptance remain open**. See [implementation.md](implementation.md) for changes, verification and remaining boundaries.

Baseline and findings: [review.md](review.md). Ordered implementation checklist: [todo.md](todo.md).

## Goal

You run one command when you want to stream. On first start, a browser setup page asks for your Twitch Client ID and Client Secret, guides you through Twitch consent, saves the account and tokens, prepares the speech model, and gives you a ready-to-copy OBS URL. Later starts reuse that configuration, token store, model cache, and OBS URL without another setup flow.

This plan assumes Docker and OBS run on the same desktop. The initial launcher targets Linux, matching this checkout's environment; macOS browser opening can be supported cheaply. Remote Docker and Windows launchers are separate extensions, not requirements silently added to the first release.

## Proposed user experience

The launcher now provides these commands (see implementation notes for limitations):

```sh
./voxer.sh start       # first run opens setup; later runs start without setup
./voxer.sh status      # Twitch, speech engine, and overlay connection status
./voxer.sh stop        # stop the container, retain configuration and model cache
./voxer.sh setup       # explicitly reopen setup/reauthorization
./voxer.sh prepare     # prepare/check the speech model before stream time
./voxer.sh update      # explicitly download a chosen release; not part of start
```

First run:

1. The launcher checks Docker/Compose and obtains a versioned image if it is not present. It does not require host Python, uv, Node, or ffmpeg.
2. It starts the container in setup mode. Missing credentials are not a restart-loop error.
3. After the control listener responds, the **host launcher** opens a private, short-lived setup URL. Headless use prints a manual fallback URL.
4. The setup page shows the exact callback to register: `http://localhost:4343/oauth/callback`. You still create a Twitch Developer application manually; Voxer cannot invent its Client ID/Secret or register the callback for you.
5. You enter Client ID and Client Secret and choose **Connect Twitch**. No access/refresh token copying and no overlay-secret generation are required.
6. Twitch opens in the same browser. You sign in with your **streaming/channel account**, not a separate bot account. Twitch's validated grant supplies the stable user ID and login; the page asks you to confirm the channel before enabling chat processing.
7. Credentials, confirmed identity, and tokens are saved privately. Model preparation has visible progress and retry controls. Setup completes only after the grant is durable and mandatory chat reception is ready.
8. You copy a generated OBS browser-source URL once. The simple overlay is the recommended default. Scheduled chat posting starts turned off.

Subsequent run:

- Use the already-installed image; no build, automatic image update, or model download when the cache is complete.
- Reuse the saved grant; refresh when needed under the existing exclusive ownership contract.
- Open a browser only for genuinely missing/revoked permissions, never just because Twitch timed out.
- Report `Ready — waiting for OBS overlay` if the backend works but no overlay is connected. Connecting OBS must not be required for backend readiness.
- Preserve the overlay credential/URL across stop/start, recreation, and image updates.

## Architecture decisions

### Keep one application and the existing stack

Retain Python 3.14, uv/uv.lock, TwitchIO Conduits, Starlette overlays, Supertonic CPU synthesis, and both current overlays. Do not add a database server, Redis, a frontend framework, Kubernetes, a GPU runtime, or a second reconnect loop.

A small host launcher handles Docker lifecycle and desktop browser opening. It does not host Twitch secrets and does not mount the Docker socket inside the application.

### Explicit settings, loaded before heavy initialization

Replace import-time operational configuration with an explicit settings object. Defaults remain compatible with `.env`/environment deployment.

Precedence: **explicit environment override > saved setup settings > defaults**. Invalid explicit overrides are errors, not silently replaced by saved values. Track which source supplied a setting for diagnostics without revealing its value.

Store wizard credentials and identity in a versioned `/data/settings.json` with mode `0600`, using the existing atomic-write helper. Keep user grants in `/data/tokens.json`; keep the overlay credential independent of Twitch tokens. Persistent private state is plaintext on disk with restrictive permissions, not magically encrypted; document secure backups.

Generate the overlay credential once, not at every start. Importing setup/control/status code must not import or initialize the model engine. Keep a compatibility path for existing `.env`, tokens, voice assignments, and timestamps.

### Bootstrap and runtime are different states

The lightweight control/setup listener must respond before TTS preparation. Retain port 4343 and the existing callback URI; avoid requiring a second registered callback. A coordinator owns listener handoff/lifecycle so bootstrap and TwitchIO never bind that port simultaneously.

States must distinguish:

- `setup_required`
- `authorization_required`
- `authorizing`
- `preparing_model`
- `connecting_twitch`
- `ready`
- `degraded` / `error`, with a component-specific reason
- `stopping`

Authorization and model work may run concurrently after settings are valid, but no speech consumption or scheduled chat posting starts until its prerequisites hold. Bound work admitted before model readiness so startup does not replay old chat after a long download. Install shutdown handling and acquire runtime ownership before cleanup or expensive initialization.

Run synchronous model loading outside the event loop. A thread keeps HTTP responsive but does not make native inference cancellable: shutdown and preparation tests must account for that. Start with a thread and a measured stop deadline; consider a child process only if measurements show unbounded shutdown.

### Safe browser setup

A loopback-published port alone is not sufficient authorization for a credential-writing endpoint. Preserve host/origin checks; require a short-lived setup capability plus a browser-bound session and CSRF protection for mutations. Deliver the capability to the host launcher through a local container control command, not normal application/access logs. Remove bootstrap credentials from the browser URL after session establishment.

Client Secret travels in a bounded POST body, never a query string, command argument, browser storage, status response, or log. Use private cookies, no-store/referrer protections, bounded OAuth state, exact registered redirects, and scope/client verification. Keep setup enrollment distinct from normal reauthorization: first enrollment discovers an identity only within the authorized setup session; later grants must match the stored/explicitly configured account.

Close credential-editing access after setup unless explicitly reopened with a new setup capability. Remote setup requires a deliberate HTTPS/tunnel configuration; it is not publicly exposed by default.

### One owner for auth completion and subscriptions

Retain private atomic token persistence and one refresh owner. Move startup subscription registration into one coordinator, not both the callback and composition root.

Do not confuse consent, token admission, token persistence, and subscription success. Callback/status output must describe the stage that actually completed. If saving fails, report it and do not claim setup survives restart.

Classify provider errors: validation/revocation/scope rejection may require consent; timeouts, connection failures, 429, and 5xx retry with bounded backoff/jitter while retaining recoverable tokens. Use typed outcomes and time budgets at startup rather than a broad `except Exception` fallback.

Treat chat reception as mandatory. Followers/subscriptions/bits/raids have separate status and may degrade independently, including for channels not eligible for paid features. Compute needed scopes from selected features against current Twitch requirements; preserve existing deployments and require consent only when the necessary grants change.

### Container storage and packaging

Default volumes:

- Named state volume at `/data`: settings, tokens, voice assignments, timestamps, emote cache, and operator messages.
- Existing persistent TTS cache, with an explicit model path and revision verification.
- Generated audio and temporary WAVs in bounded temporary storage with permissions for the unprivileged runtime user, not in the durable credential backup.

Initialize empty message configuration only when absent; never copy the repository-specific Discord/community messages into a general install and never overwrite existing settings.

Remove the mandatory `.env` dependency from the default setup path. Keep advanced environment/secrets injection supported. Keep non-root/read-only/capability controls and host-loopback publishing. Check existing bind-mount permissions without broad `chmod 777` or recursive host ownership changes. Preserve restart behavior after unexpected failure, but explicitly stopped services must stay stopped.

Publish versioned amd64/arm64 release images through CI only after platform build and smoke verification. Confirm redistribution rights for model and sound assets first: the sound manifest records unknown licenses for 32 user-added clips. Obtain permission/source evidence or keep those clips outside public artifacts as private custom mounts. A development build override can remain available. Routine `start` uses the cached selected release (`--no-build`, no unconditional pull); `update` is explicit and retains a rollback image/version.

The measured ffmpeg layer is the first image-size candidate. Benchmark a minimal, pinned, audio-only build while preserving WAV-to-MP3 compatibility, licensing, and both architectures. Do not claim a smaller image until it is built and measured. Keep glibc-based Python slim unless native dependency compatibility proves another base is worthwhile.

Support maintenance through installed non-editable console scripts or documented `python -m` commands. The current image deliberately skips installing the project and therefore lacks its console scripts; the launcher must not depend on those missing executables.

### Readiness and stream-friendly performance

- `/healthz`: minimal, unauthenticated liveness only; keep existing semantics.
- `/readyz`: secret-free response; ready only when settings/grant/model/mandatory chat subscription are usable. Return 503 otherwise; do not fail solely because OBS is disconnected.
- Detailed status: protected local control command/page; report optional features, overlay count, model stage, errors, and timings without secrets.

Measure image acquisition, control-listener availability, token validation/refresh, model load/warmup, subscription establishment, first speech synthesis, memory, native thread count, and OBS/game CPU competition separately. Keep one engine and serialized inference. Thread-count presets need real inference benchmarks; do not blindly set CPU/memory/PID limits that prevent model load or slow speech.

Provisional acceptance targets, **not measured promises**:

- Cached-image control/setup availability: within **two seconds** on the agreed reference desktop.
- Warm start with complete model cache and valid grants: **p95 within ten seconds** over at least ten runs under healthy Twitch/network conditions.
- Graceful stop: within **20 seconds**, including during preparation and synthesis; retain state and no orphan ffmpeg process.
- Normal restart: no browser opening, no registry/model download, no changed OBS URL.

Record hardware, cached state, provider/network timing, and compressed transfer size separately. Confirm or revise targets explicitly after a baseline; do not hide remote-provider delay or silently lower test gates.

## Implementation order

1. Fix early ownership and prove transient-auth/subscription failure behavior.
2. Introduce explicit settings and runtime configuration injection.
3. Add lightweight bootstrap states/status and shutdown ownership.
4. Add protected credential setup and durable storage.
5. Complete first enrollment and single-owner auth/subscription coordination.
6. Add the host launcher.
7. Make default Compose startup independent of `.env` and host UID assumptions.
8. Add explicit model preparation, readiness, and measured CPU controls.
9. Move emote maintenance into the container workflow with shared authorization.
10. Measure and reduce the ffmpeg/image cost.
11. Test and publish versioned container releases.
12. Verify the complete first-run and recurring-stream workflow; simplify documentation.

Each task's files, dependencies, and acceptance checks are in [todo.md](todo.md). Keep changes in small reviewed slices; do not merge setup UI and secret handling without the security checks.

## Risks and decisions to confirm

- **Platform:** Linux host first; if Docker runs on another machine, browser/callback reachability needs a tunnel or deliberately configured HTTPS URL.
- **Account:** retain the current one-account/one-channel design and infer identity during enrollment. Separate bot and broadcaster accounts require a different permission/identity contract and are out of this plan's core scope.
- **Model:** cache preparation adds one-time download/disk cost; a bundled-model image is an optional distribution variant, not the default size optimization.
- **Token-store migration:** never discard usable existing grants just because settings move. Show explicit recovery for mismatched app/account/scopes and persistence failure.
- **OAuth library coupling:** the current adapter and admission checks use TwitchIO private APIs. Pin and contract-test them before dependency upgrades; do not replace transport behavior casually.
- **Setup listener:** verify callback/browser-cookie continuity and single-owner port handoff with the pinned adapter, not just mocked route handlers.
- **Image-size trade-off:** a bespoke encoder reduces dependencies but creates maintenance and license obligations; keep the current encoder until output/build checks justify the replacement.

## Completion gate

The change is complete only when a fresh-volume Docker setup asks for Client ID/Secret, opens host-side Twitch consent, safely persists the confirmed grant, supplies a working OBS URL, and a stop/start/recreate/update retains configuration and authorization. Offline lifecycle/container tests must pass; live Twitch and OBS checks require the operator's explicit participation and must be reported separately.
