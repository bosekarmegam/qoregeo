"""
qoregeo.serve
=============
Serve a generated map over ``http://127.0.0.1`` and open it in the browser.

Why: OpenStreetMap's tile servers require an HTTP ``Referer``, which browsers
don't send for pages opened straight from disk (``file://``). Serving the page
from localhost gives the tiles a valid Referer. Only the one page is served,
and only to this machine: the server listens on 127.0.0.1 and answers only
requests addressed to 127.0.0.1 or localhost, so a web page that points its
own domain at 127.0.0.1 (DNS rebinding) cannot read the map.
"""

from __future__ import annotations

import http.server
import os
import sys
import threading
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote_to_bytes, urlsplit

from .exceptions import GeoFileNotFoundError, InvalidArgumentError

# Host names a browser uses for this machine. Any other Host header means a page
# on another site resolved its own domain to 127.0.0.1 (DNS rebinding).
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _OnePageHandler(http.server.BaseHTTPRequestHandler):
    """Serves one HTML page at ``/<name>`` (and ``/``); every other path is a 404."""

    page_name = b"map.html"   # the page's file name as bytes (os.fsencode), not URL-quoted

    @staticmethod
    def load() -> bytes:
        return b""

    def do_GET(self) -> None:   # noqa: N802 (http.server naming)
        self._respond(body=True)

    def do_HEAD(self) -> None:   # noqa: N802
        self._respond(body=False)

    def _local_host(self) -> bool:
        host = self.headers.get("Host")
        if host is None:   # browsers always send Host; other local clients may not
            return True
        try:
            name = urlsplit("//" + host.strip()).hostname
        except ValueError:
            return False
        return name is not None and name.rstrip(".") in _LOCAL_HOSTS

    def _respond(self, body: bool) -> None:
        if not self._local_host():
            self.send_error(403, "Forbidden host",
                            "This map is only served at http://127.0.0.1 and http://localhost.")
            return
        path = unquote_to_bytes(urlsplit(self.path).path).lstrip(b"/")
        if path not in (b"", self.page_name):
            self.send_error(404, "Not found")
            return
        try:
            data = self.load()
        except OSError:
            self.send_error(404, "The map file is gone")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            try:
                self.wfile.write(data)
            except ConnectionError:   # the browser reloaded or closed the tab mid-download
                pass

    def log_message(self, format: str, *args: Any) -> None:   # silence request logs
        pass


def _check_port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise InvalidArgumentError("port", port, "an integer from 0 (any free port) to 65535",
                                   "geo.show(port=8000)")
    return port


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that is already in
    # use, so "port in use" would go unnoticed; elsewhere it only skips TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return   # the browser went away (reload, closed tab): nothing to report
        super().handle_error(request, client_address)


def _name_bytes(name: str) -> bytes:
    """The page name as bytes, so non-UTF-8 file names (surrogate escapes) work too."""
    try:
        return os.fsencode(name)
    except UnicodeError:
        return name.encode("utf-8", "surrogatepass")


def _make_server(
    load: Callable[[], bytes], name: str, port: int
) -> tuple[http.server.ThreadingHTTPServer, str]:
    page = _name_bytes(name)
    handler: type[_OnePageHandler] = type(
        "Handler", (_OnePageHandler,), {"page_name": page, "load": staticmethod(load)}
    )
    port = _check_port(port)
    try:
        server = _Server(("127.0.0.1", port), handler)
    except OSError as exc:   # port in use, or a privileged port
        raise InvalidArgumentError(
            "port", port,
            f"a free port (127.0.0.1:{port} is unavailable: {exc.strerror or exc})",
            "geo.show(port=0)   # 0 = any free port (CLI: --port 0)",
        ) from None
    url = f"http://127.0.0.1:{server.server_address[1]}/{quote(page)}"
    return server, url


def _interactive() -> bool:
    return bool(getattr(sys, "ps1", None)) or bool(sys.flags.interactive) or "ipykernel" in sys.modules


def _run(
    server: http.server.ThreadingHTTPServer,
    url: str,
    block: bool | None,
    open_browser: bool,
    verbose: bool,
) -> str:
    if block is None:
        block = not _interactive()
    if not block:
        threading.Thread(target=server.serve_forever, daemon=True, name="qoregeo-serve").start()
    if verbose:
        print(f"Serving map at {url}" + ("  (press Ctrl+C to stop)" if block else ""))
    if open_browser:
        webbrowser.open(url)
    if not block:
        return url
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url


def serve_file(
    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    port: int = 0,
    block: bool | None = None,
    open_browser: bool = True,
    verbose: bool = True,
) -> str:
    """
    Serve the HTML file ``path`` at ``http://127.0.0.1:<port>/<file>`` and open it.

    The file is re-read on every request, so re-running the script that
    writes it and reloading the page shows the new map.

    Parameters
    ----------
    path         : the HTML file to serve
    port         : TCP port (0 = pick a free one)
    block        : keep serving until Ctrl+C. Default: block in scripts, run
                   in a background thread in interactive sessions/notebooks
    open_browser : open the page in the default browser
    verbose      : print the URL

    Returns
    -------
    The URL being served.
    """
    full = os.path.abspath(os.fsdecode(path))
    if not os.path.isfile(full):
        raise GeoFileNotFoundError(os.fsdecode(path))

    def load() -> bytes:
        with open(full, "rb") as fh:
            return fh.read()

    server, url = _make_server(load, os.path.basename(full), port)
    return _run(server, url, block, open_browser, verbose)


def serve_html(
    page: str,
    name: str = "map.html",
    port: int = 0,
    block: bool | None = None,
    open_browser: bool = True,
    verbose: bool = True,
) -> str:
    """
    Serve the HTML string ``page`` at ``http://127.0.0.1:<port>/<name>``
    without writing a file. Same options as :func:`serve_file`.
    """
    if not isinstance(page, str):
        raise InvalidArgumentError("page", page, "an HTML string", "serve_html(geo.map_html())")
    data = page.encode("utf-8", "replace")
    server, url = _make_server(lambda: data, name, port)
    return _run(server, url, block, open_browser, verbose)
