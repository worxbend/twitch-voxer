"""Local browser enrollment on the existing OAuth port.

The launcher obtains a one-use capability through a private container command.
A browser session and CSRF token protect credential and channel mutations.
TwitchIO still owns runtime token refresh and Conduit transport.
"""

import asyncio
import datetime
import html
import re
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from twitchio import Client
from twitchio.web.utils import FetchTokenPayload

from .config import ConfigError, RuntimeSettings, read_settings, save_settings
from .lifecycle import StartupStatus
from .oauth import SecureOAuthAdapter
from .token_store import read_tokens, run_durable, write_json_atomic
from .twitch_transport import install_form_oauth, validate_oauth_token

SESSION_COOKIE = "voxer_setup"
SESSION_TTL = 900


@dataclass(repr=False)
class Grant:
    user_id: str
    login: str
    token: str
    refresh: str


class SetupAdapter(SecureOAuthAdapter):
    """One listener across setup, native model loading, and Twitch operation."""

    requires_setup_session = True

    def __init__(
        self,
        settings: RuntimeSettings,
        scopes,
        status: StartupStatus,
        *,
        owner_task: asyncio.Task | None = None,
    ):
        super().__init__(
            redirect_url=settings.OAUTH_REDIRECT_URL,
            expected_user_id=settings.BOT_ID,
            client_id=settings.CLIENT_ID,
            scopes=scopes,
            host=settings.OAUTH_HOST,
            port=settings.OAUTH_PORT,
        )
        self.settings = settings
        self.status = status
        self._owner_task = owner_task
        self.enrolled = asyncio.Event()
        self._bot_attached = False
        self._control_secret = secrets.token_urlsafe(32)
        self._capability: tuple[str, float] | None = None
        self._session: tuple[str, str, float] | None = None
        self._grant: tuple[Grant, float] | None = None
        self._mutation_lock = asyncio.Lock()
        self._client_max_size = 4096
        self.middlewares.append(self._boundary)
        self.router.add_get("/healthz", self.health)
        self.router.add_get("/readyz", self.ready)
        self.router.add_get("/status", self.detail)
        self.router.add_post("/control/open", self.open_setup)
        self.router.add_get("/setup", self.page)
        self.router.add_post("/credentials", self.credentials)
        self.router.add_post("/confirm", self.confirm)

    @property
    def origin(self) -> str:
        parsed = urlsplit(self.redirect_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def write_control_file(self) -> None:
        write_json_atomic(
            Path(self.settings.SETTINGS_FILE).with_name("control.json"),
            {"secret": self._control_secret, "origin": self.origin},
        )

    def _local_control(self, request: web.Request) -> bool:
        value = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return value.isascii() and secrets.compare_digest(value, self._control_secret)

    def _session_valid(self, request: web.Request) -> bool:
        value = request.cookies.get(SESSION_COOKIE, "")
        return bool(
            self._session
            and self._session[2] > time.monotonic()
            and value.isascii()
            and secrets.compare_digest(value, self._session[0])
        )

    def _mutation_valid(self, request: web.Request, data) -> bool:
        csrf = data.get("csrf", "")
        return bool(
            self._session_valid(request)
            and self._session
            and request.headers.get("Origin") == self.origin
            and isinstance(csrf, str)
            and csrf.isascii()
            and secrets.compare_digest(csrf, self._session[1])
        )

    @web.middleware
    async def _boundary(self, request: web.Request, handler):
        if self._owner_task is not None and self._owner_task.cancelling():
            self.status.stage = "stopping"
        if self.status.stage == "stopping" and request.path not in {
            "/healthz",
            "/readyz",
            "/status",
            "/setup",
        }:
            return web.Response(status=503, text="Voxer is stopping.")
        if self.status.stage == "error" and request.path in {
            "/oauth",
            self._redirect_path,
            "/credentials",
            "/confirm",
        }:
            return web.Response(
                status=503, text="Stop/start Voxer before retrying authorization."
            )
        if request.path not in ("/healthz", "/readyz") and not self._valid_host(
            request
        ):
            return web.Response(status=400, text="Invalid setup host.")
        if request.path == self._eventsub_path:
            return web.Response(status=404)  # Conduits do not use webhook ingress.
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        if isinstance(response, web.HTTPException):
            plain = web.Response(
                status=response.status, headers=response.headers, body=response.body
            )
            plain.cookies.update(response.cookies)
            response = plain
        response.headers.update(
            {
                "Cache-Control": "no-store",
                # Chromium sends Origin: null on form POSTs under no-referrer.
                # Keep same-origin form provenance, suppress all cross-site
                # referrers, and retain no-referrer on OAuth/capability responses.
                "Referrer-Policy": "same-origin"
                if request.path == "/setup" and not request.query
                else "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://id.twitch.tv; base-uri 'none'; frame-ancestors 'none'",
            }
        )
        return response

    async def health(self, _request):
        return web.Response(text="ok")

    async def ready(self, _request):
        return web.json_response(
            self.status.public(), status=200 if self.status.ready else 503
        )

    async def detail(self, request):
        if not self._local_control(request) and not self._session_valid(request):
            return web.Response(status=403)
        return web.json_response(self.status.detail())

    async def open_setup(self, request):
        if not self._local_control(request):
            return web.Response(status=403)
        key = secrets.token_urlsafe(32)
        self._capability = (key, time.monotonic() + 300)
        return web.json_response({"url": f"{self.origin}/setup?key={key}"})

    def _document(self, body: str, *, refresh: bool = False) -> web.Response:
        refresh_meta = "<meta http-equiv='refresh' content='2'>" if refresh else ""
        return web.Response(
            content_type="text/html",
            text=(
                "<!doctype html><html lang=en><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                + refresh_meta
                + "<title>Voxer setup</title><style>body{font:18px system-ui;max-width:680px;"
                "margin:4rem auto;padding:1rem;background:#15191d;color:#e9edef}"
                "input,button{font:inherit;padding:.6rem;margin:.5rem 0;box-sizing:border-box}"
                "input{width:100%}a{color:#70ddd0}code{overflow-wrap:anywhere}label{display:block}"
                "button{cursor:pointer}</style><h1>Voxer</h1>" + body + "</html>"
            ),
        )

    def _csrf_field(self) -> str:
        return f'<input type="hidden" name="csrf" value="{self._session[1] if self._session else ""}">'

    async def page(self, request):
        keys = request.query.getall("key", [])
        if keys:
            if (
                len(keys) != 1
                or not self._capability
                or self._capability[1] <= time.monotonic()
                or not keys[0].isascii()
                or not secrets.compare_digest(keys[0], self._capability[0])
            ):
                return web.Response(
                    status=403, text="Setup link expired. Run ./voxer.sh setup again."
                )
            self._capability = None
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._session = (session, csrf, time.monotonic() + SESSION_TTL)
            self._grant = None
            response = web.HTTPSeeOther("/setup")
            response.set_cookie(
                SESSION_COOKIE,
                session,
                httponly=True,
                secure=urlsplit(self.redirect_url).scheme == "https",
                samesite="Lax",
                max_age=SESSION_TTL,
                path="/",
            )
            return response
        if not self._session_valid(request):
            return web.Response(
                status=403, text="Run ./voxer.sh setup to open a private setup session."
            )
        csrf = self._csrf_field()
        body = f"<p>Status: <strong>{html.escape(self.status.stage)}</strong></p>"
        if self.status.reason:
            body += f"<p>{html.escape(self.status.reason)}</p>"
        if self._grant and self._grant[1] > time.monotonic():
            body += (
                f"<p>Use channel <strong>{html.escape(self._grant[0].login)}</strong>?</p>"
                f'<form method="post" action="/confirm">{csrf}<button>Confirm channel</button></form>'
            )
        elif not self._bot_attached and (
            not self.settings.BOT_USERNAME
            or not self.settings.CLIENT_ID
            or not self.settings.CLIENT_SECRET
        ):
            body += (
                "<p>Create a Twitch application at <a href='https://dev.twitch.tv/console/apps' rel=noreferrer>"
                "the developer console</a>. Register this exact redirect:</p>"
                f"<p><code>{html.escape(self.redirect_url)}</code></p>"
                f'<form method="post" action="/credentials">{csrf}'
                '<label>Client ID<input name="client_id" required maxlength="128" autocomplete="off"></label>'
                '<label>Client Secret<input name="client_secret" type="password" required maxlength="256" autocomplete="off"></label>'
                "<button>Save and connect Twitch</button></form>"
            )
            if self.settings.CLIENT_ID and self.settings.CLIENT_SECRET:
                body += "<p><a href='/oauth'>Use the existing credentials and connect Twitch</a></p>"
        else:
            body += f"<p>Channel: {html.escape(self.settings.BOT_USERNAME)}</p>"
            if self.status.stage in {
                "ready",
                "authorization_required",
                "confirm_channel",
            }:
                body += "<p><a href='/oauth'>Authorize Twitch again</a></p>"
        if self.status.optional_failures:
            body += f"<p>Optional event subscriptions unavailable: {self.status.optional_failures}</p>"
        if self.status.ready:
            # Overlay secret is shown only to the protected setup browser, never
            # to /status or logs. Keep it stable across restarts.
            overlay = f"http://localhost:{self.settings.SERVER_PORT}/simple?token={self.settings.OVERLAY_TOKEN}"
            body += f"<p>OBS Browser Source URL:</p><code>{html.escape(overlay)}</code>"
            body += (
                "<p>"
                + (
                    "OBS connected."
                    if self.status.overlay_connected()
                    else "Ready — waiting for OBS overlay."
                )
                + "</p>"
            )
        body += "<p><a href='/setup'>Refresh status</a></p>"
        return self._document(
            body,
            refresh=self.status.stage
            in {"preparing_model", "connecting_twitch", "subscribing"},
        )

    async def credentials(self, request):
        data = await request.post()
        if not self._mutation_valid(request, data):
            return web.Response(status=403)
        async with self._mutation_lock:
            if self._bot_attached or (
                self.settings.BOT_USERNAME
                and self.settings.CLIENT_ID
                and self.settings.CLIENT_SECRET
            ):
                return web.Response(
                    status=409,
                    text="Credentials are locked after enrollment. Stop Voxer before changing its application settings.",
                )
            client_id, secret = data.get("client_id", ""), data.get("client_secret", "")
            if (
                not isinstance(client_id, str)
                or not isinstance(secret, str)
                or not re.fullmatch(r"[A-Za-z0-9]{1,128}", client_id)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret)
            ):
                return web.Response(
                    status=422, text="Enter a valid Twitch Client ID and Secret."
                )
            # Never claim to save an override that cannot take effect.
            for key, value in (
                ("TWITCH_CLIENT_ID", client_id),
                ("TWITCH_CLIENT_SECRET", secret),
            ):
                if self.settings.sources.get(key) in {"environment", "dotenv"}:
                    current = (
                        self.settings.CLIENT_ID
                        if key == "TWITCH_CLIENT_ID"
                        else self.settings.CLIENT_SECRET
                    )
                    if value != current:
                        return web.Response(
                            status=409,
                            text="An environment or .env override controls credentials. Change that override first.",
                        )
            try:
                values = read_settings(self.settings.SETTINGS_FILE)
                values.update(
                    {"TWITCH_CLIENT_ID": client_id, "TWITCH_CLIENT_SECRET": secret}
                )
                await run_durable(save_settings, self.settings.SETTINGS_FILE, values)
            except OSError, ValueError, ConfigError:
                return web.Response(
                    status=503,
                    text="Cannot save credentials. Check state files and volume permissions.",
                )
            self.settings = replace(
                self.settings, CLIENT_ID=client_id, CLIENT_SECRET=secret
            )
            self._client_id = client_id
            self._pending_states.clear()
            self._grant = None
            self.status.stage = "authorization_required"
            return web.HTTPSeeOther("/oauth")

    async def oauth_redirect(self, request):
        if not self._session_valid(request):
            return web.Response(status=403, text="Run ./voxer.sh setup first.")
        if not self.settings.CLIENT_ID or not self.settings.CLIENT_SECRET:
            return web.HTTPSeeOther("/setup")
        if not self.status.ready:
            self.status.stage = "authorizing"
        return await super().oauth_redirect(request)

    async def oauth_callback(self, request):
        response = await super().oauth_callback(request)
        if (
            response.status >= 400
            and self._session_valid(request)
            and not self.status.ready
        ):
            self.status.stage = "authorization_required"
        return response

    async def exchange(self, code: str) -> Grant:
        """Isolated enrollment exchange; no token admission/refresh loop yet."""
        client = Client(
            client_id=self.settings.CLIENT_ID, client_secret=self.settings.CLIENT_SECRET
        )
        install_form_oauth(client)
        try:
            async with asyncio.timeout(15):
                payload = await client._http.user_access_token(
                    code, redirect_uri=self.redirect_url
                )
                validated = await validate_oauth_token(client, payload.access_token)
            if (
                validated.client_id != self.settings.CLIENT_ID
                or not isinstance(validated.user_id, str)
                or not re.fullmatch(r"[0-9]{1,32}", validated.user_id)
                or not isinstance(payload.refresh_token, str)
                or not payload.refresh_token
                or not validated.login
                or not re.fullmatch(r"[A-Za-z0-9_]{1,25}", validated.login)
                or not set(self._required_scopes.selected).issubset(validated.scopes)
                or (self.settings.BOT_ID and validated.user_id != self.settings.BOT_ID)
                or (
                    not self.settings.BOT_ID
                    and self.settings.BOT_USERNAME
                    and validated.login.lower() != self.settings.BOT_USERNAME.lower()
                )
            ):
                raise ValueError("Wrong account, application or scopes")
            return Grant(
                validated.user_id,
                validated.login,
                payload.access_token,
                payload.refresh_token,
            )
        finally:
            await client.close(save_tokens=False)

    async def fetch_token(self, request):
        if not self._session_valid(request):
            return FetchTokenPayload(
                status=403,
                response=web.Response(status=403, text="Setup session expired."),
            )
        if self._bot_attached:
            result = await super().fetch_token(request)
            if result.status == 200:
                self.status.stage = (
                    "ready"
                    if self.status.chat and self.status.model
                    else "connecting_twitch"
                )
            return result
        settings = self.settings
        try:
            grant = await self.exchange(request.query["code"])
        except ValueError:
            return FetchTokenPayload(
                status=403,
                response=web.Response(
                    status=403,
                    text="Authorize your configured streaming account with all requested scopes.",
                ),
            )
        except Exception:
            # No provider exception payloads in logs/responses: they may contain
            # credentials or a consumed authorization code.
            self.status.stage = "authorization_required"
            return FetchTokenPayload(
                status=502,
                response=web.Response(
                    status=502,
                    text="Twitch exchange failed. Return to setup and retry.",
                ),
            )
        if self.settings is not settings or not self._session_valid(request):
            return FetchTokenPayload(
                status=409,
                response=web.Response(
                    status=409,
                    text="Setup changed during authorization. Start consent again.",
                ),
            )
        self._grant = (grant, time.monotonic() + 300)
        return FetchTokenPayload(status=200, response=web.HTTPSeeOther("/setup"))

    async def confirm(self, request):
        data = await request.post()
        if not self._mutation_valid(request, data):
            return web.Response(status=403)
        async with self._mutation_lock:
            if (
                not self._grant
                or self._grant[1] <= time.monotonic()
                or self._bot_attached
            ):
                return web.Response(status=409, text="No pending channel to confirm.")
            grant = self._grant[0]
            try:
                values = read_settings(self.settings.SETTINGS_FILE)
                values.update(
                    {"TWITCH_BOT_USERNAME": grant.login, "VOXER_BOT_ID": grant.user_id}
                )
                tokens = read_tokens(self.settings.TOKEN_FILE)
                tokens[grant.user_id] = {
                    "user_id": grant.user_id,
                    "token": grant.token,
                    "refresh": grant.refresh,
                    "last_validated": datetime.datetime.now().isoformat(),
                }
                await run_durable(write_json_atomic, self.settings.TOKEN_FILE, tokens)
                await run_durable(save_settings, self.settings.SETTINGS_FILE, values)
            except OSError, ValueError, ConfigError:
                return web.Response(
                    status=503,
                    text="Could not save authorization. Check state files/volume permissions and confirm again.",
                )
            self.settings = replace(
                self.settings,
                BOT_USERNAME=grant.login,
                BOT_ID=grant.user_id,
                NO_ANNOUNCE_USERS=frozenset({grant.login.lower()})
                if "VOXER_NO_ANNOUNCE_USERS" not in self.settings.sources
                else self.settings.NO_ANNOUNCE_USERS,
            )
            self._expected_user_id = grant.user_id
            self._grant = None
            self.status.authorized = True
            self.status.stage = "preparing_model"
            self.enrolled.set()
            return web.HTTPSeeOther("/setup")

    def attach_bot(self, bot):
        self._bot_attached = True
        self.client = bot
        self._expected_user_id = bot.bot_id
        self._authorize = bot.event_oauth_authorized

    def authorization_required(self):
        self.status.authorized = False
        self.status.stage = "authorization_required"
