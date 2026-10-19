"""
qoregeo.mcp_server
==================
A Model Context Protocol server exposing QOREgeo's tools to AI assistants
(Claude Desktop, Claude Code, OpenAI Codex, Cursor, VS Code, Gemini CLI, …).

Zero dependencies: JSON-RPC 2.0 over stdio, one message per line. Start it
with ``qoregeo mcp`` (or ``python -m qoregeo mcp``); see ``qoregeo mcp --help``.

Both protocol eras are spoken:

* handshake era (2024-11-05 … 2025-11-25): ``initialize`` → requests;
* stateless era (2026-07-28): ``server/discover`` and every request carrying
  its protocol version in ``_meta``.

stdout carries protocol messages only — diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from typing import IO, Any

from ._version import __version__
from .tools import SERVER_INSTRUCTIONS, TOOLS, ToolContext, _text_safe, call_tool, tool_definitions

# Newest first. A client asking ``initialize`` for a version we don't know is
# offered the first entry (the client then decides whether it can continue).
HANDSHAKE_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)
SUPPORTED_PROTOCOL_VERSIONS = MODERN_PROTOCOL_VERSIONS + HANDSHAKE_PROTOCOL_VERSIONS

# Reserved _meta keys of the stateless era.
_META_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
_META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

SERVER_INFO = {
    "name": "qoregeo",
    "title": "QOREgeo",
    "version": __version__,
    "description": "Offline geospatial tools: distances, zones, filters, maps and geocoding.",
    "websiteUrl": "https://github.com/bosekarmegam/qoregeo",
}

Json = dict[str, Any]


class MCPServer:
    """
    Handles MCP messages. Transport-independent: ``handle(message)`` takes a
    decoded JSON-RPC message and returns the response dict (or None for
    notifications); ``serve_stdio()`` runs it over stdin/stdout.
    """

    def __init__(self, context: ToolContext | None = None, include_network: bool | None = None) -> None:
        self.context = context if context is not None else ToolContext()
        # Geocoding tools are listed only when the network is allowed.
        self.include_network = self.context.allow_network if include_network is None else include_network
        self.protocol_version: str | None = None   # negotiated by initialize (handshake era)
        self.client_info: Json = {}
        self._write_lock = threading.Lock()

    # ── message dispatch ────────────────────────────────────────────────────

    def handle(self, message: Any) -> Json | list[Json] | None:
        if isinstance(message, list):
            # JSON-RPC batches (only in protocol 2025-03-26) — answer each request.
            if not message:
                return _error(None, INVALID_REQUEST, "Empty batch")
            replies = [r for r in (self._handle_one(m) for m in message) if r is not None]
            return replies or None
        return self._handle_one(message)

    def _handle_one(self, message: Any) -> Json | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(_id_or_none(message.get("id") if isinstance(message, dict) else None),
                          INVALID_REQUEST, "Expected a JSON-RPC 2.0 message")
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str):
            # A response to a request we never sent, or garbage: nothing to answer.
            return None if "result" in message or "error" in message else \
                _error(_id_or_none(msg_id), INVALID_REQUEST, "Missing 'method'")
        if "id" not in message:
            return None   # notifications/initialized, notifications/cancelled, … need no reply
        if not _valid_id(msg_id):
            # MCP: "Requests MUST include a string or integer ID … MUST NOT be null."
            return _error(None, INVALID_REQUEST, "'id' must be a string or an integer, "
                                                 f"e.g. \"id\": 1 (got {msg_id!r:.40})")
        params = message.get("params")
        params = {} if params is None else params
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "'params' must be an object")

        # The stateless era is chosen by the envelope key's presence (a malformed
        # value is then rejected, not served as legacy). initialize always means
        # the handshake era, even when a client stamps the envelope on it.
        meta = params.get("_meta")
        if method != "initialize" and (
                (isinstance(meta, dict) and _META_VERSION in meta) or method == "server/discover"):
            return self._handle_modern(msg_id, method, params)

        handler = _HANDSHAKE_METHODS.get(method)
        if handler is None:
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        return self._run(msg_id, method, lambda: handler(self, params, self.protocol_version))

    def _handle_modern(self, msg_id: Any, method: str, params: Json) -> Json:
        """A stateless-era request: the protocol version travels with every request."""
        meta = params.get("_meta")
        if not isinstance(meta, dict) or _META_VERSION not in meta or \
                not isinstance(meta.get(_META_CAPABILITIES), dict):
            return _error(msg_id, INVALID_PARAMS,
                          f"Requests must carry _meta['{_META_VERSION}'] and _meta['{_META_CAPABILITIES}']")
        requested = meta[_META_VERSION]
        if not isinstance(requested, str):
            return _error(msg_id, INVALID_PARAMS,
                          f"_meta['{_META_VERSION}'] must be a string, e.g. "
                          f"\"{MODERN_PROTOCOL_VERSIONS[0]}\"")
        if requested not in MODERN_PROTOCOL_VERSIONS:
            return _error(msg_id, UNSUPPORTED_PROTOCOL_VERSION,
                          f"Unsupported protocol version: {requested}",
                          {"supported": list(MODERN_PROTOCOL_VERSIONS), "requested": str(requested)})
        handler = _MODERN_METHODS.get(method)
        if handler is None:
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")

        def run() -> Json:
            result = handler(self, params, requested)
            result["resultType"] = "complete"
            result["_meta"] = {**result.get("_meta", {}), _META_SERVER_INFO: SERVER_INFO}
            return result

        return self._run(msg_id, method, run)

    def _run(self, msg_id: Any, method: str, call: Callable[[], Json]) -> Json:
        try:
            result = call()
        except _RpcError as exc:
            return _error(msg_id, exc.code, exc.message)
        except Exception as exc:   # noqa: BLE001 — never let one request kill the server
            _log(f"internal error in {method}: {type(exc).__name__}: {exc}")
            return _error(msg_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # ── methods ─────────────────────────────────────────────────────────────

    def _instructions(self) -> str:
        return SERVER_INSTRUCTIONS + f" Files are confined to: {self.context.root}"

    def _capabilities(self) -> Json:
        return {"tools": {"listChanged": False}}

    def _initialize(self, params: Json, version: str | None) -> Json:
        requested = params.get("protocolVersion")
        chosen = requested if requested in HANDSHAKE_PROTOCOL_VERSIONS else HANDSHAKE_PROTOCOL_VERSIONS[0]
        self.protocol_version = chosen
        info = params.get("clientInfo")
        self.client_info = info if isinstance(info, dict) else {}
        return {
            "protocolVersion": chosen,
            "capabilities": self._capabilities(),
            "serverInfo": SERVER_INFO,
            "instructions": self._instructions(),
        }

    def _discover(self, params: Json, version: str | None) -> Json:
        return {
            "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
            "capabilities": self._capabilities(),
            "instructions": self._instructions(),
            "ttlMs": 0,
            "cacheScope": "private",
        }

    def _ping(self, params: Json, version: str | None) -> Json:
        return {}

    def _visible(self, name: Any) -> bool:
        """Tools this server offers: geocoding only with network, writers only with write access."""
        tool = TOOLS.get(name) if isinstance(name, str) else None
        return tool is not None and (self.include_network or not tool.open_world) and \
            (self.context.allow_write or tool.read_only)

    def _tools_list(self, params: Json, version: str | None) -> Json:
        tools = [t for t in tool_definitions("mcp") if self._visible(t["name"])]
        if version in ("2024-11-05", "2025-03-26"):
            # 'title' arrived in 2025-06-18; older clients may reject unknown fields.
            tools = [{k: v for k, v in t.items() if k != "title"} for t in tools]
        result: Json = {"tools": tools}
        if version in MODERN_PROTOCOL_VERSIONS:
            result.update(ttlMs=0, cacheScope="private")
        return result

    def _tools_call(self, params: Json, version: str | None) -> Json:
        name = params.get("name")
        if not isinstance(name, str) or not self._visible(name):
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "'arguments' must be an object")
        result = call_tool(name, arguments, self.context).to_mcp()
        if version in ("2024-11-05", "2025-03-26"):
            result.pop("structuredContent", None)   # added in 2025-06-18
        return result

    # ── stdio transport ─────────────────────────────────────────────────────

    def serve_stdio(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        """Read newline-delimited JSON-RPC from stdin until EOF; write replies to stdout."""
        src = stdin if stdin is not None else _utf8_stdin()
        dst = stdout if stdout is not None else _utf8_stdout()
        _log(f"QOREgeo MCP server {__version__} ready (root: {self.context.root}, "
             f"write: {'on' if self.context.allow_write else 'off'}, "
             f"network: {'on' if self.include_network else 'off'})")
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line, parse_constant=_reject_constant)
            except (ValueError, RecursionError) as exc:   # bad JSON, NaN, huge ints, deep nesting
                reason = exc.msg if isinstance(exc, json.JSONDecodeError) else type(exc).__name__
                self._send(dst, _error(None, PARSE_ERROR, f"Parse error: {reason}"))
                continue
            try:
                reply = self.handle(message)
            except RecursionError:
                reply = _error(None, INVALID_REQUEST, "Request nested too deeply")
            if reply is not None:
                self._send(dst, reply)

    def _send(self, dst: IO[str], reply: Json | list[Json]) -> None:
        # Batch replies are encoded one by one, so one bad reply can't sink the rest.
        text = "[" + ",".join(_encode(r) for r in reply) + "]" if isinstance(reply, list) \
            else _encode(reply)
        with self._write_lock:
            dst.write(text + "\n")
            dst.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _encode(reply: Json) -> str:
    """
    One reply as a line of JSON that strict parsers accept: ASCII only (so it
    stays on one line whatever the payload), no NaN, no lone surrogates.
    """
    try:
        return json.dumps(_text_safe(reply), ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        _log(f"could not encode a reply: {type(exc).__name__}: {exc}")
        msg_id = _id_or_none(reply.get("id")) if isinstance(reply, dict) else None
        return json.dumps(_text_safe(_error(msg_id, INTERNAL_ERROR,
                                            "Internal error: unencodable result")),
                          ensure_ascii=True, separators=(",", ":"))


def _valid_id(msg_id: Any) -> bool:
    """A request id MCP allows: a string or an integer (not null, a float or a bool)."""
    return isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool)


def _id_or_none(msg_id: Any) -> Any:
    return msg_id if _valid_id(msg_id) else None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> Json:
    error: Json = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _log(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except (OSError, ValueError, UnicodeError):
        pass


def _utf8_stdin() -> IO[str]:
    stream = sys.stdin
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
    return stream


def _utf8_stdout() -> IO[str]:
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", newline="\n")
    return stream


_Method = Callable[[MCPServer, Json, "str | None"], Json]


def _no_items(key: str) -> _Method:
    # Advertised capabilities are tools only, but answer these listings politely.
    def listing(server: MCPServer, params: Json, version: str | None) -> Json:
        result: Json = {key: []}
        if version in MODERN_PROTOCOL_VERSIONS:
            # 2026-07-28: list results MUST carry caching hints.
            result.update(ttlMs=0, cacheScope="private")
        return result

    return listing


_HANDSHAKE_METHODS: dict[str, _Method] = {
    "initialize": MCPServer._initialize,
    "ping": MCPServer._ping,
    "tools/list": MCPServer._tools_list,
    "tools/call": MCPServer._tools_call,
    "resources/list": _no_items("resources"),
    "resources/templates/list": _no_items("resourceTemplates"),
    "prompts/list": _no_items("prompts"),
}

# initialize and ping no longer exist in the stateless era.
_MODERN_METHODS: dict[str, _Method] = {
    "server/discover": MCPServer._discover,
    **{k: v for k, v in _HANDSHAKE_METHODS.items() if k not in ("initialize", "ping")},
}


def serve(
    root: str | None = None,
    allow_write: bool = True,
    allow_network: bool = False,
    max_features: int = 200,
) -> None:
    """Run the MCP server on stdin/stdout (blocks until stdin closes)."""
    context = ToolContext(root=root if root is not None else os.getcwd(), allow_write=allow_write,
                          allow_network=allow_network, max_features=max_features)
    MCPServer(context).serve_stdio()
