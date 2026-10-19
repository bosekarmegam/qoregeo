"""
tests/test_mcp_server.py
========================
qoregeo.mcp_server — the Model Context Protocol server (``qoregeo mcp``).

``MCPServer.handle()`` is tested as a pure function (decoded JSON-RPC in,
response dict out), ``serve_stdio()`` with ``io.StringIO`` streams, and one
end-to-end test runs ``python -m qoregeo mcp`` as a subprocess.

Covers both protocol eras — the handshake era (``initialize``; versions
2024-11-05 … 2025-11-25) and the stateless era (2026-07-28: ``server/discover``
and the protocol version in every request's ``_meta``) — plus JSON-RPC error
handling, batches, notifications, the read-only / offline tool filtering and
the one-line ASCII framing of stdout.

Run with:
    pytest tests/test_mcp_server.py -v
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from qoregeo import __version__
from qoregeo.mcp_server import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    MODERN_PROTOCOL_VERSIONS,
    SERVER_INFO,
    MCPServer,
)
from qoregeo.tools import TOOLS, ToolContext, ToolResult

REPO_ROOT = Path(__file__).resolve().parent.parent

MODERN = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_VERSION = -32022

NETWORK_TOOLS = {"geocode", "reverse_geocode"}
WRITING_TOOLS = {"make_map", "export_features"}

DISTANCE_ARGS = {"from": {"lat": 0, "lng": 0}, "to": {"lat": 0, "lng": 1}}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

def request(method: str, params: Any = None, msg_id: Any = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def notification(method: str, params: Any = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def modern_meta(version: str = MODERN, **extra: Any) -> dict[str, Any]:
    return {META_VERSION: version, META_CAPS: {}, **extra}


def modern(method: str, params: dict[str, Any] | None = None, msg_id: Any = 1,
           version: str = MODERN) -> dict[str, Any]:
    return request(method, {**(params or {}), "_meta": modern_meta(version)}, msg_id)


def result_of(reply: Any, msg_id: Any = 1) -> dict[str, Any]:
    assert isinstance(reply, dict), reply
    assert reply.get("jsonrpc") == "2.0"
    assert reply.get("id") == msg_id
    assert "error" not in reply, reply
    return reply["result"]


def error_of(reply: Any, code: int, msg_id: Any = 1) -> dict[str, Any]:
    assert isinstance(reply, dict), reply
    assert reply.get("jsonrpc") == "2.0"
    assert reply.get("id") == msg_id
    assert "result" not in reply, reply
    assert reply["error"]["code"] == code, reply
    assert isinstance(reply["error"]["message"], str) and reply["error"]["message"]
    return reply["error"]


def tool_names(result: dict[str, Any]) -> set[str]:
    return {t["name"] for t in result["tools"]}


def initialized(server: MCPServer, version: str = "2025-11-25") -> MCPServer:
    result_of(server.handle(request("initialize", {"protocolVersion": version,
                                                   "capabilities": {},
                                                   "clientInfo": {"name": "test", "version": "1"}},
                                    msg_id=0)), 0)
    assert server.handle(notification("notifications/initialized")) is None
    return server


def run_stdio(server: MCPServer, *lines: str) -> tuple[list[str], str]:
    """Feed raw lines to serve_stdio; return (output lines, raw output)."""
    out = io.StringIO()
    server.serve_stdio(io.StringIO("".join(line + "\n" for line in lines)), out)
    raw = out.getvalue()
    return raw.splitlines(), raw


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "cities.csv").write_text(
        "name,lat,lng\nDelhi,28.6139,77.2090\nMumbai,19.0760,72.8777\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def server(root: Path) -> MCPServer:
    return MCPServer(ToolContext(root=str(root)))


# ─────────────────────────────────────────────────────────────────────────────
# Handshake era
# ─────────────────────────────────────────────────────────────────────────────

class TestInitialize:
    @pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
    def test_negotiates_supported_version(self, server, root, version):
        result = result_of(server.handle(request("initialize", {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "2.0"}})))
        assert result["protocolVersion"] == version
        assert server.protocol_version == version
        assert server.client_info == {"name": "claude-code", "version": "2.0"}
        assert result["serverInfo"] == SERVER_INFO
        assert result["serverInfo"]["name"] == "qoregeo"
        assert result["serverInfo"]["version"] == __version__
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert isinstance(result["instructions"], str)
        assert str(root.resolve()) in result["instructions"] or \
            os.path.realpath(root) in result["instructions"]

    @pytest.mark.parametrize("version", ["2099-01-01", "1.0", None, 5, MODERN])
    def test_unknown_version_gets_newest_handshake_version(self, server, version):
        params: dict[str, Any] = {"capabilities": {}}
        if version is not None:
            params["protocolVersion"] = version
        result = result_of(server.handle(request("initialize", params)))
        assert result["protocolVersion"] == "2025-11-25" == HANDSHAKE_PROTOCOL_VERSIONS[0]

    def test_without_params(self, server):
        assert result_of(server.handle(request("initialize")))["protocolVersion"] == "2025-11-25"
        assert server.client_info == {}

    def test_bad_client_info_ignored(self, server):
        result_of(server.handle(request("initialize", {"protocolVersion": "2025-06-18",
                                                       "clientInfo": "nope"})))
        assert server.client_info == {}

    def test_ping(self, server):
        assert result_of(server.handle(request("ping", msg_id=7)), 7) == {}
        assert result_of(initialized(server).handle(request("ping", {}, msg_id=8)), 8) == {}

    def test_resources_and_prompts_are_empty(self, server):
        assert result_of(server.handle(request("resources/list"))) == {"resources": []}
        assert result_of(server.handle(request("resources/templates/list"))) == \
            {"resourceTemplates": []}
        assert result_of(server.handle(request("prompts/list"))) == {"prompts": []}


class TestHandshakeTools:
    @pytest.mark.parametrize("version, has_title", [("2025-11-25", True), ("2025-06-18", True),
                                                    ("2025-03-26", False), ("2024-11-05", False)])
    def test_tools_list_title_by_version(self, server, version, has_title):
        initialized(server, version)
        tools = result_of(server.handle(request("tools/list")))["tools"]
        assert tools
        for t in tools:
            assert ("title" in t) is has_title
            assert {"name", "description", "inputSchema", "annotations"} <= set(t)
            assert t["inputSchema"]["type"] == "object"
        assert "ttlMs" not in result_of(server.handle(request("tools/list")))

    def test_tools_list_before_initialize(self, server):
        tools = result_of(server.handle(request("tools/list")))["tools"]
        assert all("title" in t for t in tools)

    def test_default_lists_all_but_network_tools(self, server):
        names = tool_names(result_of(server.handle(request("tools/list"))))
        assert names == set(TOOLS) - NETWORK_TOOLS

    @pytest.mark.parametrize("version, structured", [("2025-11-25", True), ("2025-06-18", True),
                                                     ("2025-03-26", False),
                                                     ("2024-11-05", False)])
    def test_tools_call_structured_content_by_version(self, server, version, structured):
        initialized(server, version)
        result = result_of(server.handle(request("tools/call", {"name": "distance",
                                                                "arguments": DISTANCE_ARGS})))
        assert result["isError"] is False
        data = json.loads(result["content"][0]["text"])
        assert data["distance"] == pytest.approx(111.195, abs=0.01)
        assert ("structuredContent" in result) is structured
        if structured:
            assert result["structuredContent"] == data

    def test_tools_call_image(self, server):
        result = result_of(server.handle(request("tools/call", {
            "name": "preview_map", "arguments": {"path": "cities.csv", "width": 64,
                                                 "height": 64}})))
        assert result["isError"] is False
        assert result["content"][1]["type"] == "image"
        assert result["content"][1]["mimeType"] == "image/png"

    def test_tools_call_without_arguments(self, server):
        result = result_of(server.handle(request("tools/call", {"name": "list_files"})))
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["files"][0]["path"] == "cities.csv"

    def test_tool_error_is_a_result(self, server):
        result = result_of(server.handle(request("tools/call", {
            "name": "describe_dataset", "arguments": {"path": "../outside.csv"}})))
        assert result["isError"] is True
        assert "structuredContent" not in result
        assert "outside" in json.loads(result["content"][0]["text"])["error"]

    def test_missing_argument_is_a_tool_error(self, server):
        result = result_of(server.handle(request("tools/call", {"name": "distance",
                                                                "arguments": {}})))
        assert result["isError"] is True

    def test_codex_style_meta_is_handshake(self, server):
        initialized(server, "2025-06-18")
        reply = server.handle(request("tools/list", {"_meta": {"progressToken": 0}}, msg_id=2))
        result = result_of(reply, 2)
        assert "resultType" not in result and "_meta" not in result
        assert tool_names(result) == set(TOOLS) - NETWORK_TOOLS
        call = result_of(server.handle(request("tools/call", {
            "name": "distance", "arguments": DISTANCE_ARGS, "_meta": {"progressToken": "p1"}},
            msg_id=3)), 3)
        assert call["isError"] is False

    def test_null_params(self, server):
        assert result_of(server.handle(request("ping", None))) == {}
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": None}
        assert tool_names(result_of(server.handle(msg)))


# ─────────────────────────────────────────────────────────────────────────────
# Stateless era (2026-07-28)
# ─────────────────────────────────────────────────────────────────────────────

class TestStatelessEra:
    def test_server_discover(self, server, root):
        result = result_of(server.handle(modern("server/discover")))
        assert result["supportedVersions"] == ["2026-07-28"] == list(MODERN_PROTOCOL_VERSIONS)
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert os.path.realpath(root) in result["instructions"]
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"
        assert result["_meta"][META_SERVER_INFO] == SERVER_INFO

    def test_discover_needs_no_initialize_and_sets_no_state(self, server):
        result_of(server.handle(modern("server/discover")))
        assert server.protocol_version is None

    def test_tools_list(self, server):
        result = result_of(server.handle(modern("tools/list", msg_id="list-1")), "list-1")
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 0 and result["cacheScope"] == "private"
        assert result["_meta"][META_SERVER_INFO]["name"] == "qoregeo"
        assert tool_names(result) == set(TOOLS) - NETWORK_TOOLS
        assert all("title" in t for t in result["tools"])

    def test_tools_call(self, server):
        result = result_of(server.handle(modern("tools/call", {"name": "distance",
                                                               "arguments": DISTANCE_ARGS})))
        assert result["resultType"] == "complete"
        assert result["_meta"][META_SERVER_INFO] == SERVER_INFO
        assert result["isError"] is False
        assert result["structuredContent"]["unit"] == "km"

    def test_tool_error_is_complete_result(self, server):
        result = result_of(server.handle(modern("tools/call", {"name": "distance",
                                                               "arguments": {}})))
        assert result["isError"] is True and result["resultType"] == "complete"

    def test_other_meta_keys_survive(self, server):
        msg = request("tools/list", {"_meta": modern_meta(progressToken=5)})
        assert result_of(server.handle(msg))["resultType"] == "complete"

    @pytest.mark.parametrize("version", ["2099-01-01", "2025-11-25", "2024-11-05", "x", 1])
    def test_unsupported_version(self, server, version):
        for method in ("tools/list", "server/discover", "ping"):
            error = error_of(server.handle(request(method, {"_meta": modern_meta(version)})),
                             UNSUPPORTED_VERSION)
            assert error["data"]["supported"] == ["2026-07-28"]
            assert error["data"]["requested"] == str(version)

    @pytest.mark.parametrize("method", ["initialize", "ping", "notifications/initialized",
                                        "no/such/method"])
    def test_removed_methods(self, server, method):
        error_of(server.handle(modern(method)), METHOD_NOT_FOUND)

    @pytest.mark.parametrize("meta", [
        {META_VERSION: MODERN},                       # no client capabilities
        {META_VERSION: MODERN, META_CAPS: None},
        {META_VERSION: MODERN, META_CAPS: []},
        {META_CAPS: {}},                              # no version (server/discover)
        None,
    ])
    def test_discover_needs_version_and_capabilities(self, server, meta):
        params = {} if meta is None else {"_meta": meta}
        error = error_of(server.handle(request("server/discover", params)), INVALID_PARAMS)
        assert META_CAPS in error["message"]

    def test_requests_need_capabilities(self, server):
        msg = request("tools/list", {"_meta": {META_VERSION: MODERN}})
        error_of(server.handle(msg), INVALID_PARAMS)
        msg = request("tools/call", {"name": "distance", "arguments": DISTANCE_ARGS,
                                     "_meta": {META_VERSION: MODERN}})
        error_of(server.handle(msg), INVALID_PARAMS)

    def test_string_ids_echoed(self, server):
        reply = server.handle(modern("server/discover", msg_id="server-discover-probe-1"))
        assert result_of(reply, "server-discover-probe-1")["resultType"] == "complete"
        reply = server.handle(request("ping", msg_id="abc"))
        assert result_of(reply, "abc") == {}

    def test_both_eras_on_one_server(self, server):
        initialized(server, "2025-03-26")
        modern_list = result_of(server.handle(modern("tools/list")))
        assert all("title" in t for t in modern_list["tools"])
        legacy_list = result_of(server.handle(request("tools/list")))
        assert all("title" not in t for t in legacy_list["tools"])


# ─────────────────────────────────────────────────────────────────────────────
# Tool visibility (network / write access)
# ─────────────────────────────────────────────────────────────────────────────

class TestVisibility:
    def test_geocode_hidden_when_offline(self, server):
        assert not NETWORK_TOOLS & tool_names(result_of(server.handle(request("tools/list"))))
        for name in NETWORK_TOOLS:
            error = error_of(server.handle(request("tools/call", {
                "name": name, "arguments": {"query": "x", "point": {"lat": 0, "lng": 0}}})),
                INVALID_PARAMS)
            assert name in error["message"]

    def test_geocode_listed_with_network(self, root):
        class Fake:
            def search(self, query: str, limit: int = 5, country_codes: Any = None) -> list[Any]:
                return [{"lat": 1.0, "lng": 2.0, "name": query}]

            def reverse(self, lat: float, lng: float) -> Any:
                return {"lat": lat, "lng": lng, "name": "here"}

        server = MCPServer(ToolContext(root=str(root), allow_network=True, geocoder=Fake()))
        assert NETWORK_TOOLS <= tool_names(result_of(server.handle(request("tools/list"))))
        result = result_of(server.handle(request("tools/call", {
            "name": "geocode", "arguments": {"query": "Pune"}})))
        assert result["isError"] is False
        assert result["structuredContent"]["results"][0]["name"] == "Pune"

    def test_include_network_override(self, root):
        server = MCPServer(ToolContext(root=str(root)), include_network=True)
        assert NETWORK_TOOLS <= tool_names(result_of(server.handle(request("tools/list"))))
        result = result_of(server.handle(request("tools/call", {
            "name": "geocode", "arguments": {"query": "Pune"}})))
        assert result["isError"] is True   # listed, but the context still forbids the network
        assert "disabled" in result["content"][0]["text"].lower()

    def test_writers_hidden_when_read_only(self, root):
        server = MCPServer(ToolContext(root=str(root), allow_write=False))
        for msg in (request("tools/list"), modern("tools/list")):
            names = tool_names(result_of(server.handle(msg)))
            assert not names & WRITING_TOOLS
            assert names == set(TOOLS) - WRITING_TOOLS - NETWORK_TOOLS
        for name, output in (("make_map", "m.html"), ("export_features", "e.csv")):
            error = error_of(server.handle(request("tools/call", {
                "name": name, "arguments": {"path": "cities.csv", "output": output}})),
                INVALID_PARAMS)
            assert "Unknown tool" in error["message"]
            assert not (root / output).exists()

    def test_writers_listed_and_working_by_default(self, server, root):
        assert WRITING_TOOLS <= tool_names(result_of(server.handle(request("tools/list"))))
        result = result_of(server.handle(request("tools/call", {
            "name": "export_features", "arguments": {"path": "cities.csv",
                                                     "output": "out.geojson"}})))
        assert result["isError"] is False
        assert (root / "out.geojson").is_file()


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC errors, notifications, batches
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonRpc:
    @pytest.mark.parametrize("message", [
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"id": 1, "method": "ping"},
        {"jsonrpc": 2.0, "id": 1, "method": "ping"},
    ])
    def test_not_json_rpc_2(self, server, message):
        error_of(server.handle(message), INVALID_REQUEST, 1)

    @pytest.mark.parametrize("message", ["ping", 5, None, True])
    def test_not_an_object(self, server, message):
        error_of(server.handle(message), INVALID_REQUEST, None)

    def test_missing_method(self, server):
        error_of(server.handle({"jsonrpc": "2.0", "id": 4}), INVALID_REQUEST, 4)
        error_of(server.handle({"jsonrpc": "2.0", "id": 5, "method": 42}), INVALID_REQUEST, 5)

    def test_method_not_found(self, server):
        error = error_of(server.handle(request("tools/destroy")), METHOD_NOT_FOUND)
        assert "tools/destroy" in error["message"]

    @pytest.mark.parametrize("params", [[1], "x", 5, True])
    def test_params_not_an_object(self, server, params):
        error_of(server.handle(request("tools/list", params)), INVALID_PARAMS)

    @pytest.mark.parametrize("name", ["teleport", None, 5, ["distance"]])
    def test_unknown_tool(self, server, name):
        params: dict[str, Any] = {"arguments": {}}
        if name is not None:
            params["name"] = name
        error_of(server.handle(request("tools/call", params)), INVALID_PARAMS)

    @pytest.mark.parametrize("arguments", [[1, 2], "x", 5, True])
    def test_arguments_not_an_object(self, server, arguments):
        error = error_of(server.handle(request("tools/call", {"name": "distance",
                                                              "arguments": arguments})),
                         INVALID_PARAMS)
        assert "arguments" in error["message"]

    @pytest.mark.parametrize("message", [
        notification("notifications/initialized"),
        notification("notifications/cancelled", {"requestId": 1}),
        notification("tools/call", {"name": "distance", "arguments": DISTANCE_ARGS}),
        notification("no/such/notification"),
    ])
    def test_notifications_get_no_reply(self, server, message):
        assert server.handle(message) is None

    @pytest.mark.parametrize("message", [
        {"jsonrpc": "2.0", "id": 99, "result": {}},
        {"jsonrpc": "2.0", "id": 99, "error": {"code": -1, "message": "x"}},
    ])
    def test_responses_ignored(self, server, message):
        assert server.handle(message) is None

    def test_batch(self, server):
        replies = server.handle([
            request("ping", msg_id=1),
            notification("notifications/initialized"),
            request("tools/list", msg_id="two"),
            request("nope", msg_id=3),
            {"jsonrpc": "2.0", "id": 77, "result": {}},
        ])
        assert isinstance(replies, list) and len(replies) == 3
        assert result_of(replies[0], 1) == {}
        assert tool_names(result_of(replies[1], "two"))
        error_of(replies[2], METHOD_NOT_FOUND, 3)

    def test_batch_of_notifications(self, server):
        assert server.handle([notification("notifications/initialized")]) is None

    def test_empty_batch(self, server):
        error_of(server.handle([]), INVALID_REQUEST, None)

    def test_batch_with_garbage(self, server):
        replies = server.handle([5, request("ping", msg_id=2)])
        assert isinstance(replies, list)
        error_of(replies[0], INVALID_REQUEST, None)
        result_of(replies[1], 2)

    @pytest.mark.parametrize("msg_id", [0, -1, "x", "server-discover-probe-1", 2**53, 1.5,
                                        None])
    def test_ids_echoed(self, server, msg_id):
        assert result_of(server.handle(request("ping", msg_id=msg_id)), msg_id) == {}

    def test_internal_error_keeps_serving(self, server, monkeypatch, capsys):
        def boom(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            raise RuntimeError("kaboom")

        monkeypatch.setitem(TOOLS, "distance", dataclasses.replace(TOOLS["distance"],
                                                                   handler=boom))
        error = error_of(server.handle(request("tools/call", {"name": "distance",
                                                              "arguments": DISTANCE_ARGS})),
                         INTERNAL_ERROR)
        assert "RuntimeError" in error["message"]
        assert "kaboom" not in error["message"]   # details go to stderr only
        assert "kaboom" in capsys.readouterr().err
        assert result_of(server.handle(request("ping", msg_id=2)), 2) == {}
        bearing = result_of(server.handle(request("tools/call", {
            "name": "bearing", "arguments": DISTANCE_ARGS}, msg_id=3)), 3)
        assert bearing["isError"] is False

    def test_huge_integer_argument_is_a_tool_error(self, server):
        line = ('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "buffer",'
                ' "arguments": {"center": {"lat": 0, "lng": 0}, "radius": 1' + "0" * 400 + "}}}")
        result = result_of(server.handle(json.loads(line)))
        assert result["isError"] is True
        assert "radius" in result["content"][0]["text"]

    def test_internal_error_in_modern_era(self, server, monkeypatch):
        def boom(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            raise RuntimeError("kaboom")

        monkeypatch.setitem(TOOLS, "distance", dataclasses.replace(TOOLS["distance"],
                                                                   handler=boom))
        error_of(server.handle(modern("tools/call", {"name": "distance",
                                                     "arguments": DISTANCE_ARGS})),
                 INTERNAL_ERROR)


# ─────────────────────────────────────────────────────────────────────────────
# stdio transport
# ─────────────────────────────────────────────────────────────────────────────

class TestServeStdio:
    def test_session(self, server, capsys):
        lines, _ = run_stdio(
            server,
            json.dumps(request("initialize", {"protocolVersion": "2025-06-18"}, msg_id=1)),
            json.dumps(notification("notifications/initialized")),
            "",
            "   ",
            json.dumps(request("tools/list", msg_id=2)),
            json.dumps(request("tools/call", {"name": "distance", "arguments": DISTANCE_ARGS},
                               msg_id=3)),
        )
        assert len(lines) == 3
        replies = [json.loads(line) for line in lines]
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
        assert replies[2]["result"]["structuredContent"]["distance"] == pytest.approx(111.195,
                                                                                      abs=0.01)
        err = capsys.readouterr().err
        assert "ready" in err and __version__ in err

    def test_banner_reports_sandbox(self, root, capsys):
        MCPServer(ToolContext(root=str(root), allow_write=False)).serve_stdio(io.StringIO(""),
                                                                              io.StringIO())
        err = capsys.readouterr().err
        assert "write: off" in err and "network: off" in err

    def test_parse_error_then_continues(self, server):
        lines, _ = run_stdio(server, "{not json", '{"jsonrpc": "2.0", "id": 5, "method": "ping"}')
        first, second = (json.loads(line) for line in lines)
        error_of(first, PARSE_ERROR, None)
        assert result_of(second, 5) == {}

    def test_batch_line(self, server):
        lines, _ = run_stdio(server, json.dumps([request("ping", msg_id=1),
                                                 request("ping", msg_id=2)]),
                             "[]")
        assert len(lines) == 2
        batch = json.loads(lines[0])
        assert [r["id"] for r in batch] == [1, 2]
        error_of(json.loads(lines[1]), INVALID_REQUEST, None)

    def test_notifications_and_responses_write_nothing(self, server):
        lines, raw = run_stdio(server, json.dumps(notification("notifications/initialized")),
                               json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
        assert raw == ""

    def test_output_is_single_line_ascii(self, server, root):
        (root / "uni.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [77.2, 28.6]},
             "properties": {"name": "दिल्ली\nline two\r ", "emoji": "🗺️"}}]},
            ensure_ascii=False), encoding="utf-8")
        lines, raw = run_stdio(
            server,
            json.dumps(request("tools/call", {"name": "query_features",
                                              "arguments": {"path": "uni.geojson"}}, msg_id=1),
                       ensure_ascii=False),
            json.dumps(request("tools/call", {"name": "describe_dataset",
                                              "arguments": {"path": "uni.geojson"}}, msg_id=2)),
            json.dumps(request("tools/call", {"name": "describe_dataset",
                                              "arguments": {"path": "nö\nfile.csv"}}, msg_id=3),
                       ensure_ascii=False),
        )
        assert raw.isascii()
        assert raw.endswith("\n") and raw.count("\n") == 3 and "\r" not in raw
        replies = [json.loads(line) for line in lines]
        props = replies[0]["result"]["structuredContent"]["features"][0]["properties"]
        assert props == {"name": "दिल्ली\nline two\r ", "emoji": "🗺️"}
        assert replies[1]["result"]["isError"] is False
        assert replies[2]["result"]["isError"] is True

    def test_each_reply_is_flushed(self, server):
        class Recorder(io.StringIO):
            flushes = 0

            def flush(self) -> None:
                Recorder.flushes += 1
                super().flush()

        out = Recorder()
        server.serve_stdio(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n' * 3), out)
        assert out.getvalue().count("\n") == 3
        assert Recorder.flushes >= 3

    @pytest.mark.parametrize("line", [
        pytest.param('{"jsonrpc": "2.0", "id": NaN, "method": "ping"}', id="nan-id"),
        pytest.param('{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"n": '
                     + "9" * 5000 + "}}", id="huge-int"),
        pytest.param("[" * 100_000, id="deep-nesting"),
    ])
    def test_hostile_line_does_not_kill_server(self, server, line):
        lines, _ = run_stdio(server, line, '{"jsonrpc": "2.0", "id": 9, "method": "ping"}')
        assert len(lines) == 2
        assert json.loads(lines[0])["error"]["code"] in (PARSE_ERROR, INVALID_REQUEST)
        assert result_of(json.loads(lines[1]), 9) == {}


# ─────────────────────────────────────────────────────────────────────────────
# End to end: python -m qoregeo mcp
# ─────────────────────────────────────────────────────────────────────────────

def test_subprocess_session(root):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("QOREGEO_") and k != "CLAUDE_PROJECT_DIR"}
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    messages = [
        request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "pytest", "version": "0"}}, msg_id=1),
        notification("notifications/initialized"),
        request("tools/list", msg_id=2),
        request("tools/call", {"name": "describe_dataset",
                               "arguments": {"path": "cities.csv"}}, msg_id=3),
        request("tools/call", {"name": "distance",
                               "arguments": {"from": {"lat": 0, "lng": 0},
                                             "to": {"lat": 0, "lng": 1}}}, msg_id="é-4"),
    ]
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "qoregeo", "mcp", "--root", str(root)],
        input=stdin, capture_output=True, env=env, cwd=str(root), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("ascii")   # every byte is ASCII
    lines = [line for line in out.split("\n") if line]
    assert len(lines) == 4
    replies = [json.loads(line) for line in lines]
    for reply in replies:
        assert reply["jsonrpc"] == "2.0"
        assert "result" in reply, reply
    assert [r["id"] for r in replies] == [1, 2, 3, "é-4"]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["serverInfo"]["name"] == "qoregeo"
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert "describe_dataset" in names and not names & NETWORK_TOOLS
    assert replies[2]["result"]["structuredContent"]["count"] == 2
    assert replies[3]["result"]["structuredContent"]["distance"] == pytest.approx(111.195,
                                                                                  abs=0.01)
    err = proc.stderr.decode("utf-8", "replace")
    assert "ready" in err
