"""
tests/test_serve.py
===================
qoregeo.serve — the one-page localhost server behind ``qoregeo serve`` and
``GeoEngine.show()``.

Every server binds 127.0.0.1 on a free port (port 0) in a background thread
(``block=False``), is queried with urllib / http.client (proxies bypassed —
the traffic never leaves this machine) and is shut down after each test.
The browser is never opened. Also covered: requests for a foreign Host
(DNS rebinding) are refused, a busy port is a clean error, browsers that
disconnect mid-download leave no traceback, and bytes / non-UTF-8 file names.

Run with:
    pytest tests/test_serve.py -v
"""

from __future__ import annotations

import functools
import http.client
import http.server
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

import pytest

import qoregeo.serve as serve_mod
from qoregeo.exceptions import GeoFileNotFoundError, InvalidArgumentError
from qoregeo.serve import serve_file, serve_html

PAGE = "<!DOCTYPE html><html><head><title>t</title></head><body>Héllo ✓</body></html>"

# Never go through HTTP(S)_PROXY for 127.0.0.1.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers + fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def servers(monkeypatch) -> list[http.server.ThreadingHTTPServer]:
    """Record every server qoregeo.serve creates, and stop them afterwards."""
    made: list[http.server.ThreadingHTTPServer] = []
    real = serve_mod._make_server

    def recording(*args: Any, **kwargs: Any) -> Any:
        server, url = real(*args, **kwargs)
        # Poll for shutdown every 20 ms instead of 0.5 s so teardown is quick.
        server.serve_forever = functools.partial(   # type: ignore[method-assign]
            type(server).serve_forever, server, poll_interval=0.02)
        made.append(server)
        return server, url

    monkeypatch.setattr(serve_mod, "_make_server", recording)
    opened: list[str] = []
    monkeypatch.setattr(serve_mod.webbrowser, "open", lambda url, *a, **k: opened.append(url))
    yield made
    for server in made:
        if server.socket.fileno() == -1:
            continue   # already closed (a blocking server stopped by Ctrl+C)
        # shutdown() waits for serve_forever(); run it on a helper thread with a
        # timeout so a server that never started cannot hang the test run.
        stopper = threading.Thread(target=server.shutdown, daemon=True)
        stopper.start()
        stopper.join(timeout=5)
        server.server_close()


def fetch(url: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method=method)
    with _OPENER.open(req, timeout=10) as resp:
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()


def fetch_status(url: str, method: str = "GET") -> int:
    try:
        return fetch(url, method)[0]
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


def raw_get(url: str, path: str, method: str = "GET") -> int:
    """Request an arbitrary (unnormalised) path on the server behind ``url``."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def background(**kwargs: Any) -> dict[str, Any]:
    return {"block": False, "open_browser": False, "verbose": False, **kwargs}


# ─────────────────────────────────────────────────────────────────────────────
# serve_html
# ─────────────────────────────────────────────────────────────────────────────

class TestServeHtml:
    def test_serves_page_with_headers(self, servers):
        url = serve_html(PAGE, **background())
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/map.html")
        status, headers, body = fetch(url)
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["content-length"] == str(len(PAGE.encode("utf-8")))
        assert body.decode("utf-8") == PAGE

        # '/' serves the page too, a query string is ignored, anything else is a 404.
        base = url.rsplit("/", 1)[0]
        assert fetch(base + "/")[2] == body
        assert fetch(url + "?v=2")[2] == body
        assert fetch_status(base + "/other.html") == 404
        assert fetch_status(base + "/map.html/extra") == 404
        assert fetch_status(base + "/favicon.ico") == 404
        assert raw_get(url, "/../map.html") == 404
        assert raw_get(url, "/MAP.HTML") == 404

        # HEAD: same headers, no body.
        status, headers, body = fetch(url, method="HEAD")
        assert status == 200
        assert headers["content-length"] == str(len(PAGE.encode("utf-8")))
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert body == b""
        assert raw_get(url, "/nope", method="HEAD") == 404

        # Only GET and HEAD are implemented.
        assert raw_get(url, "/map.html", method="POST") == 501

    def test_binds_localhost_in_daemon_thread(self, servers):
        url = serve_html(PAGE, name="page.html", **background())
        server, = servers
        assert server.server_address[0] == "127.0.0.1"
        assert url == f"http://127.0.0.1:{server.server_address[1]}/page.html"
        assert server.daemon_threads is True
        threads = [t for t in threading.enumerate() if t.name == "qoregeo-serve"]
        assert threads and all(t.daemon for t in threads)

    def test_name_with_spaces_is_quoted(self, servers):
        url = serve_html(PAGE, name="my map #1.html", **background())
        assert url.endswith("/my%20map%20%231.html")
        assert fetch(url)[2].decode("utf-8") == PAGE

    def test_verbose_prints_url(self, servers, capsys):
        url = serve_html(PAGE, block=False, open_browser=False, verbose=True)
        out = capsys.readouterr().out
        assert url in out
        assert "Ctrl+C" not in out   # background server: nothing to interrupt

    def test_quiet(self, servers, capsys):
        serve_html(PAGE, **background())
        assert capsys.readouterr().out == ""

    def test_opens_browser(self, servers, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(serve_mod.webbrowser, "open", lambda u, *a, **k: opened.append(u))
        url = serve_html(PAGE, block=False, open_browser=True, verbose=False)
        assert opened == [url]

    def test_blocking_until_ctrl_c(self, servers, monkeypatch, capsys):
        real = serve_mod._make_server   # the recording wrapper from the fixture

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            server, url = real(*args, **kwargs)

            def ctrl_c(*a: Any, **k: Any) -> None:
                raise KeyboardInterrupt

            server.serve_forever = ctrl_c   # type: ignore[method-assign]
            return server, url

        monkeypatch.setattr(serve_mod, "_make_server", interrupted)
        monkeypatch.setattr(serve_mod, "_interactive", lambda: False)
        url = serve_html(PAGE, open_browser=False, verbose=True)   # block=None → blocks
        assert url in capsys.readouterr().out
        server, = servers
        assert server.socket.fileno() == -1   # closed after Ctrl+C

    def test_interactive_sessions_do_not_block(self, servers, monkeypatch):
        monkeypatch.setattr(serve_mod, "_interactive", lambda: True)
        url = serve_html(PAGE, open_browser=False, verbose=False)   # returns at once
        assert fetch(url)[0] == 200

    @pytest.mark.parametrize("page", [b"<html></html>", None, 42, ["<html>"]])
    def test_page_must_be_str(self, servers, page):
        with pytest.raises(InvalidArgumentError) as info:
            serve_html(page, **background())   # type: ignore[arg-type]
        assert info.value.name == "page"
        assert servers == []

    @pytest.mark.parametrize("port", [-1, 70000, 65536, True, False, "80", 80.0, None])
    def test_invalid_port(self, servers, port):
        with pytest.raises(InvalidArgumentError) as info:
            serve_html(PAGE, port=port, **background())
        assert info.value.name == "port"
        assert servers == []

    def test_lone_surrogates_do_not_crash(self, servers):
        url = serve_html("<p>\ud800</p>", **background())
        assert fetch(url)[2] == b"<p>?</p>"


# ─────────────────────────────────────────────────────────────────────────────
# serve_file
# ─────────────────────────────────────────────────────────────────────────────

class TestServeFile:
    def test_rereads_and_disappears(self, servers, tmp_path):
        path = tmp_path / "map.html"
        path.write_text("<p>v1</p>", encoding="utf-8")
        url = serve_file(path, **background())
        assert url.endswith("/map.html")
        status, headers, body = fetch(url)
        assert (status, body) == (200, b"<p>v1</p>")
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"

        path.write_text("<p>version two</p>", encoding="utf-8")
        assert fetch(url)[2] == b"<p>version two</p>"
        assert fetch(url.rsplit("/", 1)[0] + "/")[2] == b"<p>version two</p>"
        assert fetch_status(url.rsplit("/", 1)[0] + "/other.html") == 404

        path.unlink()
        assert fetch_status(url) == 404
        assert fetch_status(url, method="HEAD") == 404

        path.write_text("<p>back</p>", encoding="utf-8")
        assert fetch(url)[2] == b"<p>back</p>"

    def test_file_name_with_spaces(self, servers, tmp_path):
        path = tmp_path / "my map.html"
        path.write_bytes(b"<p>spaced</p>")
        url = serve_file(str(path), **background())
        assert url.endswith("/my%20map.html")
        assert fetch(url)[2] == b"<p>spaced</p>"

    def test_relative_path(self, servers, tmp_path, monkeypatch):
        (tmp_path / "rel.html").write_bytes(b"<p>rel</p>")
        monkeypatch.chdir(tmp_path)
        url = serve_file("rel.html", **background())
        monkeypatch.chdir(tmp_path.parent)   # the absolute path was captured
        assert fetch(url)[2] == b"<p>rel</p>"

    def test_missing_file(self, servers, tmp_path):
        with pytest.raises(GeoFileNotFoundError):
            serve_file(tmp_path / "nope.html", **background())
        assert servers == []

    def test_directory_is_not_a_file(self, servers, tmp_path):
        with pytest.raises(GeoFileNotFoundError):
            serve_file(tmp_path, **background())
        assert servers == []

    def test_missing_file_is_a_file_not_found_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            serve_file(tmp_path / "nope.html", **background())

    @pytest.mark.parametrize("port", [-1, 70000, True, "80"])
    def test_invalid_port(self, servers, tmp_path, port):
        path = tmp_path / "map.html"
        path.write_text(PAGE, encoding="utf-8")
        with pytest.raises(InvalidArgumentError) as info:
            serve_file(path, port=port, **background())
        assert info.value.name == "port"
        assert servers == []

    def test_verbose_prints_url(self, servers, tmp_path, capsys):
        path = tmp_path / "map.html"
        path.write_text(PAGE, encoding="utf-8")
        url = serve_file(path, block=False, open_browser=False, verbose=True)
        assert f"Serving map at {url}" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# Host header (DNS rebinding)
# ─────────────────────────────────────────────────────────────────────────────

def request_with_host(url: str, host: str | None, method: str = "GET") -> tuple[int, bytes]:
    """Request ``url`` with this Host header (None: no Host header at all)."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.putrequest(method, parts.path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class TestHostHeader:
    @pytest.mark.parametrize("host", ["attacker.example:{port}", "attacker.example",
                                      "127.0.0.1.attacker.example:{port}",
                                      "localhost.attacker.example", "[::2]:{port}",
                                      "user@attacker.example", "", "[bad"])
    def test_foreign_host_is_refused(self, servers, host):
        url = serve_html(PAGE, **background())
        host = host.format(port=urlsplit(url).port)
        for method in ("GET", "HEAD"):
            status, body = request_with_host(url, host, method)
            assert status == 403
            assert b"H\xc3\xa9llo" not in body

    @pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LocalHost:{port}",
                                      "[::1]:{port}", "localhost", "localhost.:{port}", None])
    def test_local_host_is_served(self, servers, host):
        url = serve_html(PAGE, **background())
        if host is not None:
            host = host.format(port=urlsplit(url).port)
        assert request_with_host(url, host) == (200, PAGE.encode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# A port that is already in use
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def busy_port() -> Any:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        yield busy.getsockname()[1]


class TestPortInUse:
    def test_serve_html(self, servers, busy_port):
        with pytest.raises(InvalidArgumentError) as info:
            serve_html(PAGE, port=busy_port, **background())
        assert info.value.name == "port" and info.value.value == busy_port
        message = str(info.value)
        assert f"127.0.0.1:{busy_port} is unavailable" in message and "port=0" in message
        assert info.value.__suppress_context__
        assert servers == []

    def test_serve_file(self, servers, tmp_path, busy_port):
        path = tmp_path / "map.html"
        path.write_text(PAGE, encoding="utf-8")
        with pytest.raises(ValueError, match="port=0"):
            serve_file(path, port=busy_port, **background())
        assert servers == []

    def test_windows_does_not_share_ports(self):
        # SO_REUSEADDR on Windows would let the server bind a port in use without an error.
        assert serve_mod._Server.allow_reuse_address is (os.name != "nt")


# ─────────────────────────────────────────────────────────────────────────────
# Browsers that go away mid-download
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_request_threads(timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any("process_request" in t.name for t in threading.enumerate()):
            return
        time.sleep(0.02)
    raise AssertionError("a request thread is still running")


class TestClientDisconnect:
    def test_aborted_download_is_silent(self, servers, capsys):
        url = serve_html("<p>" + "x" * 20_000_000 + "</p>", **background())
        parts = urlsplit(url)
        client = socket.create_connection((parts.hostname, parts.port), timeout=10)
        client.sendall(f"GET {parts.path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        assert client.recv(1024).startswith(b"HTTP/1.0 200")
        # Close with a reset, like a reload or a closed tab during the download.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                          struct.pack("hh" if os.name == "nt" else "ii", 1, 0))
        client.close()
        wait_for_request_threads()
        err = capsys.readouterr().err
        assert "Traceback" not in err and "Exception occurred" not in err
        assert fetch_status(url, method="HEAD") == 200   # still serving

    def test_handle_error_ignores_only_connection_errors(self, servers, capsys):
        serve_html(PAGE, **background())
        server, = servers
        for exc in (ConnectionResetError(104, "reset"), BrokenPipeError(32, "pipe"),
                    ConnectionAbortedError(103, "aborted")):
            try:
                raise exc
            except ConnectionError:
                server.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            raise ValueError("a real bug")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 1))
        assert "ValueError: a real bug" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────────
# bytes paths and unusual file names
# ─────────────────────────────────────────────────────────────────────────────

class TestFileNames:
    def test_bytes_path(self, servers, tmp_path):
        path = tmp_path / "map.html"
        path.write_bytes(b"<p>bytes</p>")
        url = serve_file(os.fsencode(path), **background())
        assert url.endswith("/map.html")
        assert fetch(url)[2] == b"<p>bytes</p>"

    def test_missing_bytes_path(self, servers, tmp_path):
        with pytest.raises(GeoFileNotFoundError) as info:
            serve_file(os.fsencode(tmp_path / "nope.html"), **background())
        assert "nope.html" in str(info.value) and "b'" not in str(info.value)
        assert servers == []

    def test_non_ascii_name(self, servers, tmp_path):
        path = tmp_path / "café map.html"
        path.write_bytes(b"<p>cafe</p>")
        url = serve_file(path, **background())
        assert url.endswith("/" + quote(os.fsencode("café map.html")))
        assert fetch(url)[2] == b"<p>cafe</p>"

    def test_serve_html_non_ascii_name(self, servers):
        url = serve_html(PAGE, name="नक्शा.html", **background())
        assert url.isascii()
        assert fetch(url)[2] == PAGE.encode("utf-8")

    def test_non_utf8_file_name(self, servers, tmp_path):
        try:
            # Windows file names are Unicode: there is no such byte name to make.
            raw = os.path.join(os.fsencode(tmp_path), b"caf\xe9.html")
            with open(raw, "wb") as fh:
                fh.write(b"<p>latin-1 name</p>")
        except (OSError, UnicodeError):
            pytest.skip("this file system only takes UTF-8 file names")
        for path in (raw, os.fsdecode(raw)):
            url = serve_file(path, **background())
            assert url.endswith("/caf%E9.html")
            assert fetch(url)[2] == b"<p>latin-1 name</p>"
