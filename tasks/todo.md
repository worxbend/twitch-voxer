# Implementation checklist

Core implementation is now present; checked items have offline evidence. Unchecked items remain open, including live/provider, performance, migration and release acceptance. Read [implementation.md](implementation.md) for the current report, [plan.md](plan.md) for design, and [review.md](review.md) for the original evidence. Do not use real Twitch authorization or chat in offline verification.

## Task 1: acquire runtime ownership before cleanup

Prevent another process sharing state from deleting live audio or loading a second model before it discovers the existing owner. Preserve token refresh ownership across the complete application lifetime.

**Acceptance:**
- [x] Acquire the runtime/token lease before boot audio cleanup and model initialization; reject a second owner with an actionable error.
- [x] An attempted second start cannot remove an audio file belonging to the active instance; orderly shutdown releases ownership.
- [x] Preserve the existing bot/emote-fetcher token-lock contract without reacquiring a conflicting lock in the same process.

**Verify:** `uv run --frozen pytest -q tests/test_app.py tests/test_token_store.py tests/test_fetch_emotes.py`; add a separate-process lock/cleanup regression.

**Dependencies:** none. **Scope:** medium. **Likely files:** `voxer/app.py`, `voxer/bot.py`, `voxer/token_store.py`, `tests/test_app.py`, `tests/test_token_store.py`.

## Task 2: load explicit runtime settings

Move configuration construction out of module import side effects and pass operational settings into the composition root and Twitch boundary. Keep existing variable names, validation, and deployment overrides compatible.

**Acceptance:**
- [x] Explicit environment > saved settings > defaults; invalid explicit values remain visible errors, and existing `.env` installs still work.
- [x] Missing Twitch credentials are a typed setup-required outcome; importing configuration does not start providers or initialize TTS.
- [x] Identity and independent overlay credentials can come from private saved configuration without stale module-level/default-argument values.

**Verify:** `uv run --frozen pytest -q tests/test_config.py tests/test_bot.py tests/test_log.py`; add source-precedence and fresh-process import tests. Run the full suite before proceeding.

**Dependencies:** 1. **Scope:** medium. **Likely files:** `voxer/config.py`, `voxer/app.py`, `voxer/bot.py`, `voxer/log.py`, `tests/test_config.py`.

## Task 3: introduce lightweight bootstrap state and status

Start a control listener before expensive work. Keep status/lifecycle state independent of the speech engine and make startup testable with injected components.

**Acceptance:**
- [x] The control listener responds in setup-required and model-preparing states without importing/initializing the model.
- [x] Status distinguishes configuration, consent, model preparation, Twitch connection, and errors without revealing credentials.
- [x] Signals and runtime ownership cover preparation; fake-provider startup and shutdown tests finish without network access or model downloads.

**Verify:** add `tests/test_lifecycle.py`; `uv run --frozen pytest -q tests/test_app.py tests/test_lifecycle.py`; test a blocked fake model while status remains responsive.

**Dependencies:** 2. **Scope:** medium. **Likely files:** `voxer/app.py`, new `voxer/lifecycle.py`, new `voxer/control.py`, `tests/test_app.py`, new `tests/test_lifecycle.py`.

### Checkpoint: foundation

- [x] Existing 625-test behavior stays covered; no removed/skipped tests or relaxed checks. Cancellation coverage now additionally requires native work to finish before release.
- [x] Full Python suite, overlay runtime tests, and existing lint/format/type gates pass.
- [x] Review settings compatibility and early-lock ownership before adding credential-writing endpoints; final review also covered persistence races and provider HTTP transport.

## Task 4: ask for credentials through protected browser setup

Add a minimal setup page and credential-saving endpoints. Generate the persistent overlay credential automatically and make advanced settings optional.

**Acceptance:**
- [x] Ask for Client ID/Secret, show the registered callback, save atomically with mode `0600`, and generate the overlay credential only if absent.
- [x] Require a short-lived setup capability/session plus CSRF and host/origin checks; reject oversized input and arbitrary redirects.
- [x] Twitch secrets never appear in credential-bearing URLs, browser storage, routine logs, GET/status responses, or returned form values. Public Client ID and ephemeral state/code still follow the OAuth protocol; provider secret parameters use form bodies. Credential editing closes after enrollment; reopening setup allows reauthorization, not active-runtime credential replacement. The protected setup page intentionally supplies the separate OBS URL.

**Verify:** new setup boundary tests including malicious origin/host, expired capability, repeated POST, malformed input, permission failure, and log redaction; `uv run --frozen pytest -q tests/test_setup.py tests/test_token_store.py`.

**Dependencies:** 3. **Scope:** medium. **Likely files:** new `voxer/setup.py`, `voxer/control.py`, `.gitignore` (exclude private setup state), new `voxer/static/setup.html`, new `tests/test_setup.py`.

## Task 5: coordinate durable OAuth and subscription readiness

Complete first enrollment using the validated Twitch identity, then use one coordinator for token persistence and required subscriptions. Separate transient network failures from rejected credentials.

**Acceptance:**
- [x] First enrollment confirms and saves the validated channel without a mandatory username field; subsequent grants must match the configured app/account and needed scopes.
- [x] Denial/wrong identity/read or write failure never reports durable setup success; synthetic stored-grant tests reuse valid grants, and transient validation failures retain them and retry without opening consent. Live refresh/revocation remains separate.
- [x] Register subscriptions once per setup cycle; classify required/optional failures, retry transient errors within a budget, and never mark ready after mandatory chat failure.

**Verify:** add fresh grant, stored grant, expired/refreshable grant, revoked grant, 429/503/timeout, persistence failure, and duplicate-registration cases; `uv run --frozen pytest -q tests/test_oauth.py tests/test_bot.py tests/test_lifecycle.py`. Exercise a real local HTTP adapter with synthetic provider exchanges to verify cookies/port ownership; live Twitch remains separate.

**Dependencies:** 4. **Scope:** medium. **Likely files:** `voxer/oauth.py`, `voxer/bot.py`, `voxer/lifecycle.py`, `tests/test_oauth.py`, `tests/test_lifecycle.py`.

## Task 6: provide a host-side start command

Add the proposed `./voxer.sh` launcher and tests. It owns host browser opening and communicates with the container through a local control command, without reading credentials into logs or requiring a host Python install.

**Acceptance:**
- [x] `start`, `stop`, `status`, and `setup` resolve the checkout from any current directory; repeated unchanged `start` does not force recreation or reopen a browser (fake-command tests).
- [x] Wait for the listener before opening Linux `xdg-open`/macOS `open`; print a usable fallback for headless sessions and actionable Docker/port errors (Linux shell tested; actual macOS remains unverified).
- [x] Cached starts do not build or pull; setup capability retrieval is private, control HTTP timeouts/waits are bounded, and no Docker socket is exposed to the app.

**Verify:** test shell behavior with fake Docker/browser commands, hostile path/argument cases, missing tools, readiness timeout, and repeated invocation. Run the launcher against synthetic container readiness before a live-account test.

**Dependencies:** 5. **Scope:** medium. **Likely files:** new root `voxer.sh` launcher, new `tests/test_launcher.py`, `voxer/control.py`, `README.md`.

## Task 7: make fresh-volume Compose deployment self-contained

Remove manual `.env`, overlay-secret, and UID preparation from the default setup workflow. Keep safe container isolation and an advanced path for existing mounts.

**Acceptance:**
- [x] A fresh named state volume starts in setup mode with no `.env`; runtime user can write state/cache and temporary audio while root remains read-only.
- [x] Disposable-volume recreation retains settings, grants, voices, overlay credentials, cache, and nonempty operator reminders (synthetic state, not a live Twitch restart).
- [ ] Existing bind-mounted data migrates only through an explicit, tested import; no migration command is implemented.
- [x] Seed only absent defaults with empty scheduled messages; maintain loopback ports, dropped capabilities, init, private state, and safe setup stop/recreate behavior.

**Verify:** `docker compose config --quiet` without secrets; fresh-volume/read-only container checks, UID-mismatch refusal for advanced bind mounts, recreate/retention tests, and `docker stop` during fake startup. Never use real state volumes as fixtures.

**Dependencies:** 6. **Scope:** medium. **Likely files:** `docker-compose.yml`, `Dockerfile`, `voxer/control.py`, `voxer/config.py`, new `tests/test_container.py`.

### Checkpoint: first-run workflow

- [x] Native Chromium synthetic enrollment asks for only Client ID/Secret, confirms an account, persists a grant, and supplies an automatically displayed OBS URL.
- [x] Implemented security rejection cases pass; separate fresh-volume Docker checks verify read-only root and writable-mount ownership.
- [x] Fake-launcher repeated starts open no browser; actual disposable Docker recreation retains configuration.
- [ ] Complete the integrated Docker-to-Twitch/OBS workflow with operator participation; the browser and container fixtures above are separate checks.

## Task 8: prepare the model and report stream readiness

Make model preparation an explicit operation with cached normal starts, measured warmup, and a stream-friendly CPU configuration. Distinguish readiness from HTTP liveness.

**Acceptance:**
- [ ] `prepare` downloads/verifies a pinned model into persistent cache; cached starts do not download and status responds throughout preparation/failure.
- [x] `/readyz` reflects startup model, authorization, and mandatory chat subscription readiness; absent OBS is reported separately, not as backend failure. Continuous revocation/reconnect status needs additional live transport evidence.
- [ ] Record cold/warm timing, native threads, memory, first-speech latency, and shutdown; verify CPU presets/PID limits with real English/Ukrainian inference before choosing defaults.

**Verify:** `uv run --frozen pytest -q tests/test_tts.py tests/test_lifecycle.py tests/test_server.py`; add cached/missing/partial model and preparation failure cases. Run real inference and ten warm starts on the agreed reference hardware; report provider timing separately.

**Dependencies:** 7. **Scope:** medium. **Likely files:** `voxer/tts.py`, `voxer/lifecycle.py`, `voxer/server.py`, `tests/test_tts.py`, `tests/test_lifecycle.py`.

## Task 9: keep emote maintenance in the container workflow

Offer cache refresh without installing Python/ffmpeg on the host or registering a second OAuth callback. Preserve exclusive refresh ownership.

**Acceptance:**
- [x] Container maintenance reuses the confirmed account/grants and primary consent flow, never the separate hardcoded port-1337 recovery flow (`--stored-only`).
- [x] Refreshes require an explicit stopped-service maintenance operation; no second refresh owner exists.
- [x] Cache lives in persistent state; missing/outdated emotes do not block stream readiness, and the old cache survives a failed update.

**Verify:** `uv run --frozen pytest -q tests/test_auth.py tests/test_fetch_emotes.py tests/test_token_store.py`; test live-owner conflict and container module-command smoke without Twitch traffic.

**Dependencies:** 7. **Scope:** medium. **Likely files:** `voxer/fetch_emotes.py`, `voxer/control.py`, `voxer/config.py`, `tests/test_fetch_emotes.py`, `tests/test_auth.py`.

## Task 10: reduce measured image cost

Evaluate the large ffmpeg runtime layer and package operational commands coherently. Keep the original encoder until a measured compatible replacement passes.

**Acceptance:**
- [ ] Compare image/layer and registry-transfer sizes to the review baseline; any claimed reduction comes from an actual image build.
- [ ] A pinned minimal encoder, if adopted, preserves valid English/Ukrainian WAV-to-MP3 output, cancellation, timeouts, licensing, and amd64/arm64 compatibility.
- [x] Pin base/tool images by multi-platform index digest and install the project non-editably; native image smoke verifies console scripts. Existing dependency tooling can propose digest updates; Playwright stays development-only.

**Verify:** native architecture image builds, image-content smoke, `uv run --frozen pytest -q tests/test_tts.py`, and offline real MP3 playback in Chromium. Compare rebuild cache behavior after an application-only change.

**Dependencies:** 7. **Scope:** medium. **Likely files:** `Dockerfile`, `pyproject.toml`, `.dockerignore`, new `docker/ffmpeg.Dockerfile` if justified, `tests/test_container.py`.

## Task 11: test and distribute release images

Build the same artifact that streamers use and publish versioned images on approved releases. Keep image acquisition outside the routine stream-start path.

**Acceptance:**
- [ ] CI builds amd64/arm64 artifacts and runs fake-provider fresh-volume, warm-start, shutdown, and asset smoke checks; failures block publication.
- [ ] Release tags/digests, source revision, model version, SBOM/provenance, advisory triage, and sound/model redistribution rights are recorded; assets with unconfirmed rights stay outside public artifacts.
- [ ] Compose/launcher selects an installed version; `update` is explicit, state migration is compatible, and rollback retains the previous image.

**Verify:** existing CI gates plus local image smoke and multi-architecture build verification. Publishing to the registry is a release action, not part of this analysis or an unapproved test run.

**Dependencies:** 8, 10. **Scope:** medium. **Likely files:** `.github/workflows/ci.yml`, new `.github/workflows/release.yml`, `docker-compose.yml`, chosen launcher file, `tests/test_container.py`.

## Task 12: prove the recurring stream workflow and simplify the README

Document the launcher-first path, separately explain advanced deployment, and verify that the normal streaming session needs no repeated configuration.

**Acceptance:**
- [ ] Fresh setup, denial/retry, stop/start, recreation, expired-token refresh, revoked-token recovery, image update, and rollback have reproducible acceptance checks.
- [ ] With operator participation, Twitch messages/events produce audible OBS output and shutdown leaves private persistent state intact; record which checks were live versus synthetic.
- [x] README starts with one command, two credential fields, exact callback guidance, and OBS URL; documented launcher commands exist and no repository-specific chat posting starts by default.

**Verify:** full required CI gates, Chromium checks for both overlays, Docker acceptance suite, ten-run warm-start benchmark, and an explicitly authorized real Twitch/OBS session.

**Dependencies:** 8, 9, 11. **Scope:** medium. **Likely files:** `README.md`, `.env.example`, `tests/test_container.py`, `tests/test_lifecycle.py`, new `docs/startup-benchmarks.md`.

### Checkpoint: completion

- [x] Existing tests, lint, format, type checking, browser checks, and native amd64 container checks pass without weakened gates.
- [ ] First setup and persistent restart behavior meet the agreed contract; the OBS URL remains stable.
- [ ] Provisional timing targets are measured against named hardware, with any revision explicitly agreed rather than silently lowered.
- [ ] No secrets in tracked files, image layers, test output, logs, or browser storage; private volumes/backups are documented.
- [x] Offline versus live/ARM64 boundaries are clearly listed in implementation.md.
- [ ] Obtain live Twitch/OBS and native ARM64 evidence before completion/release claims.

## Parallel work

After Task 7, emote maintenance and image-size evaluation are independent. Model/readiness work can run alongside them only with explicit ownership of shared `voxer/control.py` and configuration files. Release work depends on verified artifacts. There is no reason to parallelize secret handling or modify the same lifecycle files from multiple agents.
