"""Strands agent initialization — per-session agents with AgentCore memory."""

import hashlib
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from typing import Optional

try:
    from opentelemetry import context as _otel_context
except Exception:  # opentelemetry not installed → context isolation is a no-op
    _otel_context = None

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from botocore.exceptions import ClientError
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from strands import Agent
from strands.models.openai import OpenAIModel
from strands.tools.mcp.mcp_client import MCPClient
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential, before_sleep_log, Retrying

try:
    from strands.multiagent.a2a.server import _AGENT_CARD_CONTEXT_ID
except ImportError:
    # Fallback if the SDK renames/removes this internal constant; matches
    # the value as of strands-agents 1.48.0.
    _AGENT_CARD_CONTEXT_ID = "__agent_card__"

from .config import config
from .identity import WORKLOAD_KEY, HeadersProvider, outbound

logger = logging.getLogger(__name__)

# ── shared resources (created once) ──────────────────────────────────────

_model: Optional[OpenAIModel] = None

# An MCP connection binds its credential when it opens, so connections cannot be
# shared between callers: reusing one would run a caller's tool calls under
# whoever's token opened the connection, and serve them that caller's tool list.
# Pools are therefore keyed by credential (see identity.outbound).
_pools: dict = {}

# The projected ServiceAccount token has a fixed TTL (expirationSeconds,
# currently 1h). The kubelet rewrites the file before it expires, but an open
# connection does not re-read it, so a long-lived workload pool would eventually
# call tools with an expired credential. Recycle the pool well inside that
# lifetime by rebuilding it: close the old MCPClients and open fresh ones, which
# re-invokes the transport callable and therefore the headers provider so the new
# connections carry the rotated token.
#
# We do NOT stop()+start() the same MCPClient in place: strands' MCPClient is not
# restartable — stop() tears down its background event loop, so a subsequent
# start() on the same instance fails with "Connection to the MCP server was
# closed" (and orphans the close-event coroutine), leaving a dead connection that
# every bound tool then calls into. Rebuilding sidesteps that entirely; cached
# agents that bound to the old clients are invalidated on recycle (see
# _invalidate_agents), and their conversation state is unaffected because it lives
# in AgentCore Memory keyed by session id.
#
# Caller pools need no equivalent: their key is derived from the credential, so a
# refreshed caller token yields a new pool instead of a stale one.
_MCP_CONNECTION_MAX_AGE_SECONDS = 45 * 60

# Cap on pools held open at once; the least recently used is closed past this.
# Each pool costs one connection per configured MCP server.
_MAX_MCP_POOLS = int(os.getenv("MCP_MAX_POOLS", "16"))


class _McpPool:
    """MCP clients and tools for one caller credential.

    `headers` is a provider invoked at connect time, not a fixed dict, so a
    recycled connection re-reads a rotated token.
    """

    def __init__(self, headers: HeadersProvider):
        self.headers = headers
        self.clients: list = []
        self.tools: list = []
        self.connected_at: float = 0.0


def _is_access_denied(exc: BaseException) -> bool:
    """True if *exc* is a botocore AccessDeniedException (any service)."""
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "AccessDeniedException"


def _get_model() -> OpenAIModel:
    global _model
    if _model is None:
        # Bifrost is the LLM gateway, exposed as an OpenAI-compatible endpoint
        # at <gateway>/v1. Authentication uses a Bifrost virtual key presented
        # via the `x-bf-vk` header (Bifrost governance). The OpenAI client also
        # requires a non-empty api_key, so we pass the same value there.
        vk = config.LLM_GATEWAY_API_KEY
        _model = OpenAIModel(
            client_args={
                "api_key": vk or "not-used",
                "base_url": config.LLM_GATEWAY_URL,
                "default_headers": {"x-bf-vk": vk},
            },
            model_id=config.MODEL_ID,
            params={"max_tokens": 1000, "temperature": 0.7, "stream": True},
        )
    return _model


# ── OTEL context isolation for pooled MCP connections ─────────────────────
# MCPClient.start() spawns a background event loop; anyio copies the *current*
# OTEL context (a ContextVar) into that task and it persists for the whole
# lifetime of the (pooled, cross-request) connection. Every HTTP op the
# background loop later makes — notably the session-teardown DELETE at
# recycle/LRU-close, potentially hours later — is then parented to whatever
# request span happened to be active at start(). Langfuse derives trace
# duration as max(end)-min(start) across observations, so a single stray late
# DELETE glued to the original trace inflates that request's trace to hours.
# Opening and closing the connection under a detached (empty root) context
# keeps those transport spans off the request trace (they become their own
# short root traces), so per-request traces reflect real agent latency.
@contextmanager
def _detached_otel_context():
    if _otel_context is None:  # opentelemetry unavailable → no-op
        yield
        return
    token = _otel_context.attach(_otel_context.Context())
    try:
        yield
    finally:
        _otel_context.detach(token)


# On a fresh cluster the agent pod can become Ready before an MCP backend (or
# its agentgateway route) is — the connect then fails one-shot (typically a
# transient HTTP 500 from the gateway before the backend Deployment is Ready).
# The original single-attempt connect dropped that server's tools *permanently*
# and the partial pool was cached, so the agent stayed half-blind (e.g. only
# skills-mcp, missing eks-read-mcp/gitlab-mcp) until a manual pod restart.
# Retry each server with bounded exponential backoff so a first-boot ordering
# race self-resolves without any manual intervention. Defaults (~12 attempts,
# 30s cap ≈ 3.5 min total) comfortably exceed the observed warm-up gap; both are
# env-tunable. A genuinely-down server still fails after the budget and is
# logged, without blocking the other servers.
_MCP_CONNECT_MAX_ATTEMPTS = int(os.getenv("MCP_CONNECT_MAX_ATTEMPTS", "12"))
_MCP_CONNECT_MAX_WAIT = int(os.getenv("MCP_CONNECT_MAX_WAIT", "30"))


def _connect_one(pool: _McpPool, url: str) -> MCPClient:
    """Open one MCP connection and load its tools, retrying through warm-up.

    Retries any failure (gateway 500, connection refused, list_tools error)
    with bounded exponential backoff. On a failed attempt the half-started
    client is closed before the next try so retries do not leak connections.
    Tools are appended to the pool only once, on the successful attempt. The
    connect runs under a detached OTEL context so the transport's background
    loop does not capture the active request span (see _detached_otel_context).

    The retry controller is built per call so the ``_MCP_CONNECT_*`` budget
    stays monkeypatchable (tests set wait=0 / attempts=1).
    """

    def _attempt() -> MCPClient:
        # headers travel on a pre-built httpx client (mcp SDK via strands 1.57.0)
        client = MCPClient(
            lambda u=url, p=pool: streamable_http_client(
                u, http_client=create_mcp_http_client(headers=p.headers())
            )
        )
        try:
            with _detached_otel_context():
                client.start()
                server_tools = client.list_tools_sync()
        except Exception:
            try:
                with _detached_otel_context():
                    client.stop(None, None, None)
            except Exception:
                pass
            raise
        logger.info(f"  Loaded {len(server_tools)} tools from {url}")
        pool.tools.extend(server_tools)
        return client

    retryer = Retrying(
        wait=wait_exponential(multiplier=1, max=_MCP_CONNECT_MAX_WAIT),
        stop=stop_after_attempt(_MCP_CONNECT_MAX_ATTEMPTS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    return retryer(_attempt)


def _open(pool: _McpPool, urls: list) -> None:
    for url in urls:
        logger.info(f"Connecting to MCP server: {url}")
        try:
            client = _connect_one(pool, url)
            pool.clients.append(client)
        except Exception as exc:
            logger.warning(
                f"  Failed to connect to MCP server {url} after "
                f"{_MCP_CONNECT_MAX_ATTEMPTS} attempts: {exc}"
            )
    pool.connected_at = time.monotonic()


def _close(pool: _McpPool) -> None:
    for client in pool.clients:
        try:
            # Detach so the session-teardown DELETE is not parented to the
            # caller's request trace (covers transports that issue it inline).
            with _detached_otel_context():
                client.stop(None, None, None)
        except Exception as exc:
            logger.warning(f"  Failed to close MCP connection: {exc}")
    pool.clients = []


def _get_mcp_tools(key: str, headers: HeadersProvider) -> list:
    """Tools from the MCP pool for *key*, connecting or recycling as needed."""
    urls = config.MCP_SERVER_URLS
    if not urls:
        return []

    pool = _pools.pop(key, None)
    if pool is None:
        pool = _McpPool(headers)
        _open(pool, urls)
    elif (
        key == WORKLOAD_KEY
        and time.monotonic() - pool.connected_at >= _MCP_CONNECTION_MAX_AGE_SECONDS
    ):
        logger.info(
            "Recycling %d workload MCP connection(s) to pick up the rotated token",
            len(pool.clients),
        )
        # Rebuild rather than restart in place: MCPClient is not restartable
        # (stop() kills its event loop; a same-instance start() then fails and
        # leaves a dead connection). _close + _open yields fresh clients whose
        # headers provider re-reads the rotated token.
        _close(pool)
        pool.tools = []
        _open(pool, urls)
        # Cached agents hold tool objects bound to the just-closed clients, so
        # drop them: get_or_create_agent will rebuild against the fresh pool.
        _invalidate_agents()

    # Re-insert last so dict insertion order doubles as the LRU order.
    _pools[key] = pool
    while len(_pools) > _MAX_MCP_POOLS:
        logger.info("Closing least recently used MCP pool (max %d)", _MAX_MCP_POOLS)
        _close(_pools.pop(next(iter(_pools))))

    return pool.tools


# ── per-session agent creation ───────────────────────────────────────────

# AgentCore Memory constrains sessionId / actorId to
# ``[a-zA-Z0-9][a-zA-Z0-9-_]*`` with a maximum length of 100. The contextId we
# receive from the request body is caller-supplied and, for autonomous
# incidents, is derived from an alert fingerprint (e.g.
# "PodOOMKilled|spoke-dev|ns|pod|hog") — it contains '|' and can exceed 100
# characters, so passing it verbatim makes every ListEvents/CreateEvent call
# fail with a ValidationException and the agent never completes the RCA.
_AGENTCORE_ID_MAX_LEN = 100
_AGENTCORE_ID_INVALID = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_agentcore_id(value: str) -> str:
    """Coerce an arbitrary id into a valid AgentCore sessionId / actorId.

    Invalid characters become '-'; the result is guaranteed to start with an
    alphanumeric character and to be at most ``_AGENTCORE_ID_MAX_LEN`` chars.
    When truncation is required a short deterministic hash of the original is
    appended so distinct inputs keep distinct ids (no memory cross-talk).
    """
    cleaned = _AGENTCORE_ID_INVALID.sub("-", value or "")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "s-" + cleaned.lstrip("-_")
    if len(cleaned) > _AGENTCORE_ID_MAX_LEN:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
        cleaned = cleaned[: _AGENTCORE_ID_MAX_LEN - 1 - len(digest)] + "-" + digest
    return cleaned


def _build_session_manager(session_id: str, actor_id: str):
    """Build an AgentCoreMemorySessionManager for a specific session."""
    if config.MEMORY_PROVIDER != "agentcore":
        return None

    # The A2AServer agent_factory is invoked once at construction with a
    # placeholder context id ("__agent_card__") solely to derive agent-card
    # metadata; that agent is never used for request handling. Skip memory
    # attachment for it — AgentCore session ids must start with an
    # alphanumeric character, which the placeholder does not satisfy.
    if session_id == _AGENT_CARD_CONTEXT_ID:
        return None

    mem_config = config.MEMORY_CONFIG
    memory_id = mem_config.get("memoryId")
    region = mem_config.get("region", config.AWS_REGION)

    if not memory_id:
        logger.warning("MEMORY_PROVIDER=agentcore but no memoryId in MEMORY_CONFIG")
        return None

    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    # Sanitize before handing the ids to AgentCore: the raw contextId may carry
    # '|' or exceed 100 chars (autonomous-incident fingerprints), which
    # AgentCore rejects. The raw session_id is still what we cache and echo back
    # as contextId to the caller; only the memory-backend id is normalized.
    safe_session_id = _sanitize_agentcore_id(session_id)
    safe_actor_id = _sanitize_agentcore_id(actor_id)

    agentcore_config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=safe_session_id,
        actor_id=safe_actor_id,
    )
    sm = AgentCoreMemorySessionManager(
        agentcore_memory_config=agentcore_config,
        region_name=region,
    )
    logger.info(
        f"AgentCore session manager created (memory={memory_id}, "
        f"session={safe_session_id}, actor={safe_actor_id}, raw_context={session_id!r})"
    )
    return sm


# ── Langfuse/OTEL trace attributes ───────────────────────────────────────
# Strands applies these to the agent's trace span; Langfuse lifts the
# well-known keys to trace level: ``session.id`` -> Session view (groups every
# turn of one conversation / one incident), ``user.id`` -> User, ``tags`` ->
# filterable tags. We derive the source from the session id rather than a
# separate flag: the incident-bridge sets the A2A contextId to
# "incident-<fingerprint>" for autonomous RCA and leaves it caller-supplied
# (or a fresh UUID) for interactive chat — so a "incident-" prefix is a
# reliable, transport-agnostic discriminator. This lets Langfuse filter
# tags=source:rca vs source:chat in one click, which neither session_id nor
# name filtering could do before (session_id was empty and untagged).
def _trace_attributes(session_id: str, actor_id: str) -> dict:
    source = "rca" if (session_id or "").startswith("incident-") else "chat"
    tags = [f"source:{source}", config.AGENT_NAME]
    return {
        # Raw session id (what we echo back as contextId) so the Langfuse
        # Session groups by conversation/incident. Langfuse has no AgentCore
        # charset constraint, so the raw value is fine here.
        "session.id": session_id or "",
        "user.id": actor_id or "user",
        # Langfuse lifts trace tags ONLY from "langfuse.trace.tags" (verified
        # empirically: a bare "tags" attribute is ignored, while session.id /
        # user.id ARE accepted as fallbacks). Keep "tags" too — inert on this
        # Langfuse version but forward-compatible and harmless.
        "langfuse.trace.tags": tags,
        "tags": tags,
    }


@retry(
    retry=retry_if_exception(_is_access_denied),
    wait=wait_exponential(multiplier=1, max=16),
    stop=stop_after_attempt(6),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _construct_agent(session_id: str, actor_id: str) -> Agent:
    """Build the session manager + Agent.

    Retries on AccessDeniedException (first-boot IAM propagation race
    between Pod Identity association and the AgentCore access policy)
    with exponential backoff instead of crashing the process.
    """
    session_manager = _build_session_manager(session_id, actor_id)
    headers, key = outbound(config.PROPAGATE_CALLER_TOKEN)
    tools = _get_mcp_tools(key, headers) or None
    return Agent(
        model=_get_model(),
        system_prompt=config.SYSTEM_PROMPT,
        tools=tools,
        agent_id=config.AGENT_NAME,
        name=config.AGENT_NAME,
        description=config.AGENT_DESCRIPTION,
        session_manager=session_manager,
        trace_attributes=_trace_attributes(session_id, actor_id),
    )


def create_agent(session_id: Optional[str] = None, actor_id: str = "user") -> Agent:
    """Create a Strands agent for a given session.

    Args:
        session_id: Conversation session id. A new UUID is generated when None.
        actor_id: Identity of the caller (default "user").
    """
    session_id = session_id or str(uuid.uuid4())
    agent = _construct_agent(session_id, actor_id)
    logger.info(f"Agent created: {config.AGENT_NAME} session={session_id}")
    return agent


# ── session cache ────────────────────────────────────────────────────────

_agents: dict[tuple, Agent] = {}


def _invalidate_agents() -> None:
    """Drop all cached agents after a workload MCP pool rebuild.

    Cached agents bind to the tool objects of MCPClient instances that a recycle
    has just closed, so calling them would hit dead connections. Clearing the
    cache makes get_or_create_agent reconstruct them against the fresh pool on
    next use. Conversation state is preserved: it lives in AgentCore Memory keyed
    by session id, which the rebuilt agent's session manager reloads.
    """
    if _agents:
        logger.info("Invalidating %d cached agent(s) after workload MCP recycle", len(_agents))
        _agents.clear()


def get_or_create_agent(session_id: Optional[str] = None, actor_id: str = "user") -> tuple[Agent, str]:
    """Return a cached agent for *session_id*, creating one if needed.

    Cached per (caller, session) rather than per session alone. `session_id`
    arrives from the request body as `contextId`, so keying on it alone would let
    one caller retrieve another caller's agent, whose MCP connections carry that
    caller's credential, by supplying a known context id.

    Returns (agent, session_id).
    """
    _, caller = outbound(config.PROPAGATE_CALLER_TOKEN)

    if session_id and (caller, session_id) in _agents:
        return _agents[(caller, session_id)], session_id

    sid = session_id or str(uuid.uuid4())
    agent = create_agent(session_id=sid, actor_id=actor_id)
    _agents[(caller, sid)] = agent
    return agent, sid


# ── cleanup ──────────────────────────────────────────────────────────────

def shutdown_mcp() -> None:
    if _pools:
        logger.info("Closing MCP client connections for %d pool(s)", len(_pools))
        while _pools:
            _close(_pools.pop(next(iter(_pools))))
