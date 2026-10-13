"""TwitchIO 3.2.2 OAuth transport contracts; retain its token/refresh manager.

Twitch documents form bodies, not credential-bearing request URLs:
https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#authorization-code-grant-flow
https://dev.twitch.tv/docs/authentication/refresh-tokens/
"""

from typing import Any, cast
from urllib.parse import unquote

import aiohttp
from twitchio.authentication.oauth import OAuth
from twitchio.authentication.tokens import ManagedHTTPClient
from twitchio.http import HTTPClient, Route

_ISOLATED = "_ManagedHTTPClient__isolated"


class _FormSession:
    """Delegate session ownership; change only identity POST serialization."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)

    def request(self, method, url, **kwargs):
        if str(url).startswith(Route.ID_BASE):
            kwargs["allow_redirects"] = False
            if method == "POST":
                if "json" in kwargs:
                    kwargs["data"] = kwargs.pop("json")
                headers = dict(kwargs.get("headers", {}))
                headers.pop(
                    "Authorization", None
                )  # Authenticate with the form, not an app bearer.
                kwargs["headers"] = headers
        return self.session.request(method, url, **kwargs)


class _FormHTTP(HTTPClient):
    async def _init_session(self) -> None:
        await super()._init_session()
        # The pinned SDK uses request/close/closed, all delegated above. Keep
        # the underlying pooled session and its existing ownership semantics.
        if not isinstance(self._session, _FormSession):
            self._session = cast(aiohttp.ClientSession, _FormSession(self._session))

    async def request(self, route: Route):
        if route.method == "POST" and route.use_id and route.params:
            params = dict(route.params)
            if "refresh_token" in params:
                # OAuth.refresh_token already percent-encodes this one field.
                # Form encoding must encode the original opaque token once.
                params["refresh_token"] = unquote(str(params["refresh_token"]))
            route = Route(
                route.method,
                route.path,
                use_id=True,
                headers=dict(route.headers),
                token_for=route.token_for,
                json=params,
            )
        return await super().request(route)


class _FormOAuth(_FormHTTP, OAuth):
    pass


class _FormManagedHTTP(_FormHTTP, ManagedHTTPClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        setattr(
            self,
            _ISOLATED,
            _FormOAuth(
                client_id=self.client_id,
                client_secret=self.client_secret,
                redirect_uri=self.redirect_uri,
                scopes=self.scopes,
                session=self._session,
            ),
        )


def install_form_oauth(client) -> None:
    """Install before login; never replace an active manager or its token store."""
    previous = client._http
    # Protocol-only test doubles do not have a TwitchIO transport.
    if not isinstance(previous, ManagedHTTPClient) or isinstance(
        previous, _FormManagedHTTP
    ):
        return
    if previous._session_set or previous._tokens or previous._app_token:
        raise RuntimeError(
            "Configure OAuth form transport before using the Twitch client"
        )
    client._http = _FormManagedHTTP(
        client_id=previous.client_id,
        client_secret=previous.client_secret,
        redirect_uri=previous.redirect_uri,
        scopes=previous.scopes,
        session=previous._session,
        client=client,
    )


async def validate_oauth_token(client, token: str):
    """Avoid ManagedHTTPClient replacing user validation with its app bearer."""
    http = client._http
    if isinstance(http, ManagedHTTPClient):
        http = getattr(http, _ISOLATED)
    return await http.validate_token(token)
