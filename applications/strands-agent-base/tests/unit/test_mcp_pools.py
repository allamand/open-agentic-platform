"""Unit tests for per-caller MCP connection pooling.

The behaviour under test is isolation: an MCP connection binds its credential
when it opens, so two callers must never share one.
"""

import threading

import pytest

from app import agent as agent_mod
from app.config import config
from app.identity import WORKLOAD_KEY, inbound_auth, outbound


def _join_warmups(timeout: float = 5.0) -> None:
    """Wait for any background MCP warm-up threads spawned by _open to finish."""
    for t in threading.enumerate():
        if t.name.startswith("mcp-warmup-"):
            t.join(timeout)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHTTPStatus(Exception):
    """Stand-in for an httpx.HTTPStatusError, carrying .response.status_code."""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.response = _FakeResponse(status_code)


@pytest.fixture(autouse=True)
def isolated_agent_state(monkeypatch, tmp_path):
    """Fake MCP transport, one configured server, and empty pools per test."""
    transport_calls: list = []

    def fake_transport(url, http_client=None):
        raw = dict(getattr(http_client, "headers", None) or {})
        # httpx2.Headers lowercases keys; the app and these tests always refer
        # to "Authorization" (matching identity.py's own header construction).
        headers = {"Authorization": raw["authorization"]} if "authorization" in raw else {}
        transport_calls.append((url, headers))
        return object()

    class FakeClient:
        instances: list = []

        def __init__(self, factory):
            self._factory = factory
            self.starts = 0
            self.stops = 0
            FakeClient.instances.append(self)

        def start(self):
            self.starts += 1
            self._factory()

        def stop(self, *args):
            self.stops += 1

        def list_tools_sync(self):
            return [f"tool-{id(self)}"]

    FakeClient.instances = []
    monkeypatch.setattr(agent_mod, "streamable_http_client", fake_transport)
    monkeypatch.setattr(agent_mod, "MCPClient", FakeClient)
    monkeypatch.setattr(config, "MCP_SERVER_NAMES_RAW", "mcp-time")
    monkeypatch.setattr(agent_mod, "_pools", {})
    # Keep the warm-up retry exercised but instant (cap every sleep at 0).
    monkeypatch.setattr(agent_mod, "_MCP_WARMUP_MAX_WAIT", 0)

    sa = tmp_path / "token"
    sa.write_text("sa-token-v1")
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(sa))

    token = inbound_auth.set(None)
    yield {"calls": transport_calls, "clients": FakeClient, "sa": sa}
    inbound_auth.reset(token)


def _tools_for(header):
    """Resolve credentials as the given caller and fetch tools."""
    inbound_auth.set(header)
    provider, key = outbound()
    return key, agent_mod._get_mcp_tools(key, provider)


def test_two_callers_do_not_share_a_connection(isolated_agent_state):
    alice_key, alice_tools = _tools_for("Bearer alice.jwt")
    bob_key, bob_tools = _tools_for("Bearer bob.jwt")

    assert alice_key != bob_key
    assert alice_tools != bob_tools
    assert len(agent_mod._pools) == 2

    sent = [headers.get("Authorization") for _, headers in isolated_agent_state["calls"]]
    assert sent == ["Bearer alice.jwt", "Bearer bob.jwt"]


def test_same_caller_reuses_its_pool(isolated_agent_state):
    _, first = _tools_for("Bearer alice.jwt")
    _, second = _tools_for("Bearer alice.jwt")

    assert first == second
    assert len(agent_mod._pools) == 1
    assert len(isolated_agent_state["calls"]) == 1, "should not reconnect"


def test_caller_token_is_what_reaches_mcp_not_the_service_account(isolated_agent_state):
    _tools_for("Bearer alice.jwt")
    _, headers = isolated_agent_state["calls"][0]
    assert headers["Authorization"] == "Bearer alice.jwt"
    assert "sa-token" not in headers["Authorization"]


def test_absent_caller_token_uses_the_service_account(isolated_agent_state):
    key, _ = _tools_for(None)
    assert key == WORKLOAD_KEY
    _, headers = isolated_agent_state["calls"][0]
    assert headers["Authorization"] == "Bearer sa-token-v1"


def test_workload_pool_recycles_and_rereads_the_rotated_token(isolated_agent_state):
    key, _ = _tools_for(None)
    client = isolated_agent_state["clients"].instances[0]
    assert client.starts == 1

    # Force the pool past its max age, then rotate the token on disk.
    agent_mod._pools[key].connected_at = 0.0
    isolated_agent_state["sa"].write_text("sa-token-v2")

    _tools_for(None)

    instances = isolated_agent_state["clients"].instances
    # Recycle restarts the SAME client in place (MCPClient is restartable):
    # stop() then start() on the same instance, which re-invokes the transport
    # and re-reads the rotated token. Object identity is preserved so no new
    # client is created — tool objects held by callers stay valid.
    assert client.stops == 1 and client.starts == 2, "same client restarted in place"
    assert len(instances) == 1 and instances[0] is client, "no new client object"
    _, headers = isolated_agent_state["calls"][-1]
    assert headers["Authorization"] == "Bearer sa-token-v2"


def test_caller_pool_is_not_recycled_since_its_credential_cannot_refresh(isolated_agent_state):
    key, _ = _tools_for("Bearer alice.jwt")
    client = isolated_agent_state["clients"].instances[0]
    agent_mod._pools[key].connected_at = 0.0

    _tools_for("Bearer alice.jwt")

    assert client.stops == 0 and client.starts == 1


def test_least_recently_used_pool_is_closed_past_the_cap(monkeypatch, isolated_agent_state):
    monkeypatch.setattr(agent_mod, "_MAX_MCP_POOLS", 2)

    _tools_for("Bearer alice.jwt")
    _tools_for("Bearer bob.jwt")
    alice_client = isolated_agent_state["clients"].instances[0]

    # Touch alice again so bob becomes least recently used.
    _tools_for("Bearer alice.jwt")
    bob_client = isolated_agent_state["clients"].instances[1]

    _tools_for("Bearer carol.jwt")

    assert len(agent_mod._pools) == 2
    assert bob_client.stops == 1, "least recently used pool closed"
    assert alice_client.stops == 0, "recently used pool kept"


def test_no_configured_servers_yields_no_tools_and_no_pool(monkeypatch, isolated_agent_state):
    monkeypatch.setattr(config, "MCP_SERVER_NAMES_RAW", None)
    _, tools = _tools_for("Bearer alice.jwt")
    assert tools == []
    assert agent_mod._pools == {}


def test_shutdown_closes_every_pool(isolated_agent_state):
    _tools_for("Bearer alice.jwt")
    _tools_for("Bearer bob.jwt")

    agent_mod.shutdown_mcp()

    assert agent_mod._pools == {}
    assert all(c.stops == 1 for c in isolated_agent_state["clients"].instances)


def test_connect_failure_is_contained(monkeypatch, isolated_agent_state):
    monkeypatch.setattr(agent_mod, "_MCP_WARMUP_MAX_ATTEMPTS", 2)

    class Failing(isolated_agent_state["clients"]):
        def start(self):
            super().start()
            raise RuntimeError("connect refused")

    monkeypatch.setattr(agent_mod, "MCPClient", Failing)
    _, tools = _tools_for("Bearer alice.jwt")
    _join_warmups()

    # The synchronous connect makes exactly ONE fast attempt (never blocking the
    # event loop on a down server), the agent gets no tools from it and does NOT
    # raise; a bounded background warm-up then retries to its budget and gives up.
    assert tools == []
    instances = isolated_agent_state["clients"].instances
    assert len(instances) == 1 + 2, "1 synchronous attempt + 2 background attempts"
    assert all(c.stops == 1 for c in instances), (
        "each failed attempt closes its half-started client"
    )


def test_connect_retries_in_background_through_transient_failure(monkeypatch, isolated_agent_state):
    """A first-boot warm-up race (gateway 500 before the backend is Ready) must
    self-resolve WITHOUT blocking: the synchronous connect makes one fast
    attempt, then a background warm-up retries until the backend is Ready and
    adds its tools to the pool — no cached half-blind pool needing a restart."""
    monkeypatch.setattr(agent_mod, "_MCP_WARMUP_MAX_ATTEMPTS", 5)

    class FlakyThenOk(isolated_agent_state["clients"]):
        attempts = 0

        def start(self):
            super().start()
            FlakyThenOk.attempts += 1
            if FlakyThenOk.attempts < 3:  # fail twice, succeed on the 3rd
                raise RuntimeError("gateway 500: backend warming up")

    monkeypatch.setattr(agent_mod, "MCPClient", FlakyThenOk)
    key, _ = _tools_for("Bearer alice.jwt")
    _join_warmups()

    assert FlakyThenOk.attempts == 3, "one fast sync attempt, then background retries until Ready"
    assert agent_mod._pools[key].tools, "tools added to the pool once the backend came up"
    assert len(agent_mod._pools[key].clients) == 1, "exactly one live client pooled"


def test_a_non_retryable_connect_error_is_not_warmed_up(monkeypatch, isolated_agent_state):
    """401/403/404 will not fix themselves by waiting, so no background warm-up
    is spawned: exactly one synchronous attempt, then the server is dropped."""
    monkeypatch.setattr(agent_mod, "_MCP_WARMUP_MAX_ATTEMPTS", 5)

    class Unauthorized(isolated_agent_state["clients"]):
        def start(self):
            super().start()
            raise RuntimeError("401 Unauthorized") from _FakeHTTPStatus(401)

    monkeypatch.setattr(agent_mod, "MCPClient", Unauthorized)
    _, tools = _tools_for("Bearer alice.jwt")
    _join_warmups()

    assert tools == []
    assert len(isolated_agent_state["clients"].instances) == 1, "no background retry on 401/403/404"
