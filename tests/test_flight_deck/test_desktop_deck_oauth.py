"""Google and Microsoft on the desktop app's own deck — through the real routes.

The desktop app runs Flight Deck with sign-in off, started by its Electron
shell (``FD_DESKTOP=1``) on loopback. That deck — and only that one among the
sign-in-off decks — connects Google and Microsoft for its one local user:

* ``auth._fd_desktop_deck`` needs all three: sign-in off, the shell's marker,
  a loopback bind; any other sign-in-off deck still refuses every route;
* the app mints the connect ticket and hands ``/login`` to the system browser,
  which never saw the ticket's cookie — the desktop deck doesn't ask for it,
  every other deck still does;
* ``/login`` first moves to ``http://localhost:<port>`` (the loopback name a
  Microsoft registration can list, port ignored), so the state cookie and the
  provider's callback share a host, and the redirect URI uses it;
* the callback stores the tokens for the local user, per user, and its page
  sends the user back to the app instead of opening the deck in the browser;
* agents this deck spawned get the local user's token; browsers don't;
* a Microsoft registration without a secret (public client, "Mobile and
  desktop applications") counts as configured, and the token calls send none.

Neither provider is contacted: exchange / userinfo are mocked.
"""

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.google_oauth_routes as gr
import captain_claw.flight_deck.microsoft_oauth_routes as mr
import captain_claw.microsoft_oauth as mo
from captain_claw.flight_deck import agent_secret, auth
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.google_oauth import GoogleOAuthTokens
from captain_claw.microsoft_oauth import MicrosoftOAuthTokens

PORT = "25093"
APP = f"http://127.0.0.1:{PORT}"        # where the app's window runs
SIGN_IN = f"http://localhost:{PORT}"    # where the system browser signs in
LOCAL = auth._LOCAL_USER["id"]


@pytest.fixture()
def desktop(monkeypatch):
    """The desktop app's deck: what main.js and server.main set."""
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")
    monkeypatch.setenv("FD_DESKTOP", "1")
    monkeypatch.setenv("FD_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("FD_PORT", PORT)


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    for var in ("FD_LOCKDOWN", "FD_PUBLIC_URL", "FD_AGENT_SHARED_SECRET", "FD_COOKIE_SECURE",
                "FD_DESKTOP", "FD_BIND_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev_db = auth._db
    auth.set_auth_db(d)
    await d.ensure_local_user(LOCAL, "h")  # what _init_fd_db does with sign-in off
    await d.set_system_setting(gr._K_CLIENT_ID, "g-cid")
    await d.set_system_setting(gr._K_CLIENT_SECRET, "g-secret")
    await d.set_system_setting(mr._K_CLIENT_ID, "ms-cid")  # public client: no secret
    for mod in (gr, mr):
        mod._pending_oauth.clear()
        mod._connect_tickets.clear()
    gr._primary_owner_cache.update(id=None, at=0.0)
    try:
        yield d
    finally:
        for mod in (gr, mr):
            mod._pending_oauth.clear()
            mod._connect_tickets.clear()
        gr._primary_owner_cache.update(id=None, at=0.0)
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def providers(monkeypatch):
    calls = {"google": [], "microsoft": []}

    async def g_exchange(**kw):
        calls["google"].append(kw)
        return GoogleOAuthTokens(access_token="g-access", refresh_token="g-refresh",
                                 token_type="Bearer", expires_at=time.time() + 3600,
                                 scope="openid email")

    async def g_user(access_token):
        return {"sub": "g-me", "email": "me@gmail.com"}

    async def m_exchange(**kw):
        calls["microsoft"].append(kw)
        return MicrosoftOAuthTokens(access_token="m-access", refresh_token="m-refresh",
                                    expires_at=time.time() + 3600, scope="openid Mail.ReadWrite")

    async def m_user(access_token):
        return {"sub": "m-me", "email": "me@outlook.com", "name": "Me"}

    monkeypatch.setattr(gr, "exchange_code_for_tokens", g_exchange)
    monkeypatch.setattr(gr, "fetch_user_info", g_user)
    monkeypatch.setattr(mr, "exchange_code_for_tokens", m_exchange)
    monkeypatch.setattr(mr, "fetch_user_info", m_user)
    return calls


@pytest.fixture()
def agents(monkeypatch):
    """Agents this deck spawned record the request's user — "local" here."""
    import captain_claw.flight_deck.server as srv
    registry = {"tok-local": LOCAL}
    monkeypatch.setattr(
        srv, "_resolve_agent_identity_by_auth",
        lambda t: (t in registry, registry.get(t, "")) if t else (False, ""),
    )


def _client(base_url):
    app = FastAPI()
    app.include_router(gr.router)
    app.include_router(mr.router)
    return TestClient(app, base_url=base_url, client=("127.0.0.1", 50123), follow_redirects=False)


def _connect(provider):
    """The app mints a ticket (in its own window); the system browser — a
    different cookie jar — opens /login at the app's address."""
    window, browser = _client(APP), _client(APP)
    r = window.post(f"/fd/{provider}/connect-ticket")
    assert r.status_code == 200, r.text
    return browser, r.json()["ticket"]


class TestDesktopDeckPredicate:
    @pytest.mark.parametrize("env, expected", [
        ({}, True),
        ({"FD_BIND_HOST": "::1"}, True),
        ({"FD_BIND_HOST": "localhost"}, True),
        ({"FD_BIND_HOST": "0.0.0.0"}, False),     # reachable from the network
        ({"FD_BIND_HOST": ""}, False),            # unknown bind: fail closed
        ({"FD_DESKTOP": ""}, False),              # not started by the shell
        ({"FD_AUTH_ENABLED": "true"}, False),     # a deck with accounts is no desktop deck
    ])
    def test_needs_sign_in_off_the_shell_and_loopback(self, desktop, monkeypatch, env, expected):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert auth._fd_desktop_deck() is expected


class TestGoogleOnTheDesktopDeck:
    async def test_full_flow_through_the_system_browser(self, db, desktop, providers):
        browser, ticket = _connect("google")
        # Reached at the app's 127.0.0.1: moved to localhost, ticket unspent.
        r = browser.get(f"/fd/google/login?ticket={ticket}")
        assert r.status_code == 302
        assert r.headers["location"] == f"{SIGN_IN}/fd/google/login?ticket={ticket}"
        assert ticket in gr._connect_tickets

        browser.base_url = SIGN_IN
        r = browser.get(f"/fd/google/login?ticket={ticket}")
        assert r.status_code == 302, r.text  # no ticket cookie in this browser, still fine
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert q["redirect_uri"] == [f"{SIGN_IN}/fd/google/callback"]
        assert gr._pending_oauth[q["state"][0]]["owner"] == LOCAL

        r = browser.get(f"/fd/google/callback?code=c0de&state={q['state'][0]}")
        assert r.status_code == 200, r.text
        assert providers["google"][0]["redirect_uri"] == f"{SIGN_IN}/fd/google/callback"
        assert "go back to the app" in r.text
        assert "window.location.href" not in r.text  # never opens the deck in the browser
        # Stored for the local user, per user — not in the deployment-wide keys.
        assert json.loads(await db.get_setting(LOCAL, gr._K_TOKENS))["refresh_token"] == "g-refresh"
        assert not await db.get_system_setting(gr._K_TOKENS)
        status = _client(APP).get("/fd/google/status").json()
        assert status["connected"] and status["user"]["email"] == "me@gmail.com"
        assert status["redirect_uri"] == f"{SIGN_IN}/fd/google/callback"

    async def test_callback_still_needs_the_browser_that_started_it(self, db, desktop, providers):
        browser, ticket = _connect("google")
        browser.base_url = SIGN_IN
        state = parse_qs(urlsplit(browser.get(
            f"/fd/google/login?ticket={ticket}").headers["location"]).query)["state"][0]
        r = _client(SIGN_IN).get(f"/fd/google/callback?code=c0de&state={state}")
        assert r.status_code == 400 and "not started in this browser" in r.text
        assert providers["google"] == []

    async def test_agents_get_the_local_users_token_browsers_dont(self, db, desktop, agents):
        await gr._store_tokens(db, LOCAL, GoogleOAuthTokens(
            access_token="g-access", refresh_token="g-refresh", token_type="Bearer",
            expires_at=time.time() + 3600, scope="openid email"))
        c = _client(APP)
        r = c.get("/fd/google/access_token", headers={"X-Agent-Auth": "tok-local"})
        assert r.status_code == 200 and r.json()["access_token"] == "g-access"
        r = c.get("/fd/google/access_token",
                  headers={"X-Agent-Auth": "tok-local", "Origin": "http://evil.example"})
        assert r.status_code == 403
        assert c.get("/fd/google/access_token").status_code == 403  # no identity
        assert await gr.get_valid_google_access_token(LOCAL) == "g-access"
        assert await gr.is_google_connected(LOCAL) is True


class TestMicrosoftOnTheDesktopDeck:
    async def test_full_flow_with_a_public_client(self, db, desktop, providers):
        status = _client(APP).get("/fd/microsoft/status").json()
        assert status["configured"] is True  # client id only
        browser, ticket = _connect("microsoft")
        r = browser.get(f"/fd/microsoft/login?ticket={ticket}")
        assert r.headers["location"] == f"{SIGN_IN}/fd/microsoft/login?ticket={ticket}"
        browser.base_url = SIGN_IN
        r = browser.get(f"/fd/microsoft/login?ticket={ticket}")
        assert r.status_code == 302, r.text
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert q["redirect_uri"] == [f"{SIGN_IN}/fd/microsoft/callback"]
        r = browser.get(f"/fd/microsoft/callback?code=c0de&state={q['state'][0]}")
        assert r.status_code == 200, r.text
        assert providers["microsoft"][0]["client_secret"] == ""
        assert json.loads(await db.get_setting(LOCAL, mr._K_TOKENS))["refresh_token"] == "m-refresh"
        assert _client(APP).get("/fd/microsoft/status").json()["connected"] is True


class TestEveryOtherDeck:
    @pytest.mark.parametrize("env", [{"FD_DESKTOP": ""}, {"FD_BIND_HOST": "0.0.0.0"}])
    async def test_sign_in_off_without_the_desktop_marks_serves_nothing(self, db, desktop,
                                                                         monkeypatch, env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        c = _client(APP)
        for provider in ("google", "microsoft"):
            assert c.get(f"/fd/{provider}/status").status_code == 503
            assert c.post(f"/fd/{provider}/connect-ticket").status_code == 503
        assert await gr.get_valid_google_access_token(LOCAL) is None
        assert await mr.get_valid_microsoft_access_token(LOCAL) is None

    async def test_a_deck_with_accounts_still_binds_the_ticket_to_its_browser(self, db, monkeypatch):
        # FD_DESKTOP on a deck with sign-in on changes nothing.
        monkeypatch.setenv("FD_AUTH_ENABLED", "true")
        monkeypatch.setenv("FD_DESKTOP", "1")
        monkeypatch.setenv("FD_BIND_HOST", "127.0.0.1")
        headers = {"Authorization": f"Bearer {auth.create_access_token(LOCAL, 'user')}"}
        ticket = _client(APP).post("/fd/google/connect-ticket", headers=headers).json()["ticket"]
        r = _client(APP).get(f"/fd/google/login?ticket={ticket}")  # another browser
        assert r.status_code == 401
        assert gr._pending_oauth == {}


class TestMicrosoftTokenCallsWithoutASecret:
    """A public client sends no client_secret (Microsoft refuses one from it);
    a Web registration still sends its own."""

    @pytest.fixture()
    def posted(self, monkeypatch):
        sent = []

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None):
                sent.append(dict(data))
                return httpx.Response(200, json={
                    "access_token": "a", "refresh_token": "r", "expires_in": 3600,
                    "token_type": "Bearer", "scope": "openid"})

        monkeypatch.setattr(mo.httpx, "AsyncClient", _Client)
        return sent

    async def test_exchange_and_refresh(self, posted):
        await mo.exchange_code_for_tokens(code="c", client_id="id", client_secret="",
                                          redirect_uri=f"{SIGN_IN}/cb", code_verifier="v")
        await mo.refresh_access_token(refresh_token="r", client_id="id", client_secret="")
        assert all("client_secret" not in p for p in posted)
        await mo.exchange_code_for_tokens(code="c", client_id="id", client_secret="s",
                                          redirect_uri=f"{SIGN_IN}/cb")
        await mo.refresh_access_token(refresh_token="r", client_id="id", client_secret="s")
        assert [p["client_secret"] for p in posted[2:]] == ["s", "s"]
