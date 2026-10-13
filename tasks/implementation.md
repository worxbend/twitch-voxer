# Implementation report

Status: **core workflow implemented and verified offline; live and release acceptance remain open**.

Changes are relative to reviewed revision `9c2d646`. Verification did not publish registry images, authorize real Twitch accounts, post chat, or run a live OBS session. See [todo.md](todo.md) for remaining acceptance checks and [review.md](review.md) for baseline findings.

## What changed

- `voxer/bootstrap.py` starts protected setup/control HTTP before importing the speech runtime. Ownership is acquired before setup writes, audio cleanup, or model loading and retained through shutdown.
- `voxer/config.py` constructs injected runtime settings. Precedence is environment > `.env` > saved settings > defaults. Invalid explicit overrides stay errors; missing application credentials have a typed setup outcome. Legacy configuration attributes remain lazy.
- `voxer/setup.py` asks for Client ID/Secret, shows the exact registered callback, validates the consented account/application/scopes, and asks for channel confirmation. Credentials and grants are atomically persisted with mode `0600` before enrollment succeeds.
- Setup uses a one-use, five-minute capability, a 15-minute private browser session, CSRF, exact Host/Origin checks, bounded forms, and no-store responses. Browser pages preserve same-origin POST provenance without exposing cross-site referrers. Startup pages refresh automatically until the OBS URL becomes available.
- The overlay credential is generated independently and retained in private settings. Its OBS URL appears only in the protected setup page, not status or routine logs.
- OAuth callbacks await admission and persistence; they never register subscriptions. Startup registers mandatory chat first and starts processing without waiting for optional event requests. Provider retries are bounded; outages do not discard recoverable grants or automatically open consent.
- `/healthz` reports liveness; `/readyz` reports startup authorization/model/chat readiness. OBS absence is separate. Detailed startup and optional-subscription status requires the private browser session or control bearer.
- Native model loading runs off the event loop. Cancellation waits for non-abandonable preparation and synthesis/file work before ownership is released; canceled output is cleaned afterward. Stopping/error states reject new authorization, and closing bots reject late grants/refresh writes.
- Token saves and enrollment preserve other persisted grants. Read failures do not silently overwrite the old store, and setup returns actionable errors for corrupt/unsupported state. In-memory token admission alone cannot bypass failed persistence.
- `voxer/twitch_transport.py` keeps TwitchIO's refresh manager but moves OAuth secret/code/refresh parameters from URLs into form bodies, disables identity-request redirects, and keeps Helix JSON unchanged. User-grant validation uses the isolated OAuth client so the manager cannot substitute its cached app bearer. Local HTTP contracts verify refresh serialization, validation identity, and owned/borrowed session cleanup. Sources: [Twitch authorization-code flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#authorization-code-grant-flow) and [refresh tokens](https://dev.twitch.tv/docs/authentication/refresh-tokens/).
- Control requests bypass ambient HTTP proxies and refuse redirects, preventing the administrative bearer from being forwarded. Log redaction covers OAuth codes/state, setup capabilities, application secrets, and token/refresh fields.
- `voxer.sh` provides `start`, `stop`, `status`, `setup`, `prepare`, `emotes`, and `update`. It resolves paths and image selection through Compose, including `.env` interpolation. Browser opening happens on the host; cached starts neither build nor pull. Repeated starts do not reopen setup or recreate the running service.
- Compose no longer requires `.env` or manual UID preparation: named state/cache volumes, loopback ports, non-root/read-only execution, dropped capabilities, init, and bounded temporary audio remain in place. Existing reminder files are preserved; absent files start empty.
- Preparation and emote maintenance require a stopped service and the same exclusive token-file lease. Container emote maintenance uses saved grants and never starts the legacy port-1337 consent flow.
- Base/tool images are pinned by multi-platform index digest. The project is installed non-editably so operational console scripts exist. Docker includes only the 17 sound effects with supplied CC0 attribution; 32 clips without supplied redistribution rights are excluded. Playwright is development-only.
- CI now declares Chromium setup checks and native amd64/ARM64 image build/smoke jobs. Those new remote jobs have not run.

## Final local verification

All commands below passed against the final runtime source:

```sh
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen pyright
uv run --frozen pytest -q
node tests/test_overlay.cjs
uv run --frozen python tests/browser_setup.py
uv run --frozen python tests/browser_overlay.py
bash -n voxer.sh
docker compose config --quiet
git diff --check
docker build -t twitch-voxer:local .
python3 tests/container_smoke.py twitch-voxer:local
```

Results:

- **705 Python tests passed**, compared with the 625-test baseline. No tests were removed/skipped and no lint/type gates were relaxed. Three dependency deprecation warnings remain: aiohttp application inheritance, Starlette's httpx TestClient path, and the pinned adapter's returned redirect exception in the standalone callback test.
- **9 overlay runtime tests passed**.
- Chromium completed local synthetic enrollment, channel confirmation, automatic startup-page advancement, and protected OBS URL delivery. The final fixture made no Twitch requests. A preliminary redirect-interception experiment had reached Twitch with synthetic invalid-client values (HTTP 400); it was replaced with the local provider before final verification. Browser storage stayed empty and no browser errors were recorded.
- **Four Chromium overlay combinations passed**: full/simple and normal/reduced motion, including real locally generated MP3 playback, keyboard recovery, layout, and ownership acknowledgments. Screenshots: `/tmp/voxer-overlay-browser-q0pn82ic`.
- Separate-process ownership/cleanup refusal, actual subprocess SIGTERM setup shutdown, blocked-model responsiveness, repeated native-work cancellation, persistence failure, grant preservation, mandatory subscription failure, optional readiness independence, and private control proxy/redirect behavior have regressions.
- Native **amd64** Docker smoke passed with networking disabled, disposable volumes, read-only root, and UID 1000. It checked fresh setup, private file modes, empty defaults, retained settings/grants/voices/cache and nonempty operator reminders after recreation, 17 image sound assets, maintenance scripts, absent development tooling, and graceful setup exit code 0.

The browser enrollment and Docker persistence checks are separate fixtures, not a claim of a complete Docker-to-real-Twitch/OBS session.

### Image and setup measurements

Final local image identifier:

`sha256:611c7486eea62043dc28d3b6477d35f92d9043f18f48a0bab9b400e793876f7b`

Docker image size: **1,134,718,103 bytes**. Baseline: **1,132,395,504 bytes**. No image-size reduction is claimed; ffmpeg remains the principal optimization candidate. Registry transfer size was not measured.

Final disposable-container smoke observations:

- First control availability: **2.145 seconds**.
- Control availability after recreation: **8.879 seconds**.
- Graceful setup stop: **4.381 seconds**.

These are single **setup/control** observations, not speech/Twitch-ready benchmarks. Earlier image runs under different host load measured 3.300/2.616/0.494, 1.998/1.175/0.150, and 9.033/2.949/4.240 seconds. The final control observations exceed the provisional two-second target. The provisional two-second control, ten-second warm-ready p95, and 20-second preparation/synthesis stop targets are not established by these samples and have not been lowered.

## Deliberate limits and remaining work

1. **Live operation:** explicitly coordinate a real Twitch account and OBS with the operator. Validate consent/refresh/revocation, Conduit transport and optional eligibility, audible English/Ukrainian output, repeat startup, and shutdown. Current readiness principally describes startup; continuous reconnect/revocation readiness is not implemented or claimed.
2. **Model and performance:** `prepare` is implemented but actual model download/inference was not run. Supertonic 1.2.3's default Supertonic 3 loader pins revision `724fb5abbf5502583fb520898d45929e62f02c0b` unless explicitly overridden. No additional cache-integrity audit, partial-cache acceptance, CPU presets, PID-budget validation, or first-speech/memory benchmark is claimed. Measure at least ten healthy warm starts on agreed hardware before choosing performance defaults.
3. **Native shutdown:** waiting for non-interruptible work protects ownership but cannot guarantee a 20-second graceful stop for a stuck native call. Docker's configured grace can force termination. Real inference/preparation timings must determine whether process isolation is needed.
4. **Credential changes:** `setup` reopens status/reauthorization, not live application-credential replacement. Rotate application credentials through stopped-service environment/saved-file changes. Existing configuration changes require stop/start; there is no live reconfiguration transaction.
5. **Migration and platforms:** existing grants and advanced bind paths remain supported, but no automatic volume import/migration command or tested bind-UID migration is provided. Actual macOS browser launch, remote Docker, and Windows are unverified/outside the initial workflow.
6. **Permissions and status:** the existing scope set is retained, not recomputed from a new feature-selection UI. Optional failures are counted rather than exposed as a full per-feature dashboard. Provider retries are bounded; startup failure recovery currently requires stop/start. Docker build/pull and a hung Docker daemon are not given a new host-command timeout.
7. **Release:** native ARM64 execution, dependency/image vulnerability scanning, full sound/voice/model licensing, SBOM/provenance, versioned registry publication, updater/rollback release acceptance, compressed transfer sizes, and a minimal-encoder comparison remain open. Excluding 32 clips from Docker does not make an unfiltered source wheel publicly redistributable.
8. **Private data:** saved credentials/tokens/control state are plaintext with restrictive filesystem permissions. Protect Docker access, state volumes, and backups. Do not attach real private files to issues or logs, delete state volumes casually, or infer that filesystem permissions encrypt backups.

For a local trial, use `./voxer.sh start`; register the exact callback shown in the setup page, complete consent with the streaming account, confirm the channel, and copy the OBS URL when ready. Live trial results still require operator participation.
