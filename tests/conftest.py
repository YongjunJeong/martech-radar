"""Local HTTP server for deterministic scanner tests.

Third-party websites change without warning, so they cannot be the foundation
of a regression suite. These fixtures serve pages we control that reproduce the
awkward cases: body-injected scripts, loader chains, scroll-triggered SDKs and
a CSP header that names vendors the page never contacts.
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def isolated_workspace(tmp_path_factory):
    """Run every test in its own workspace, never in the developer's.

    The tool resolves radar.toml / targets.yaml / data/ from $RADAR_HOME (or
    the cwd). Without this fixture the suite would read whatever deployment
    happens to sit where pytest was launched — and pass or fail depending
    on it. The example watchlist is copied in because a few tests load it.
    """
    ws = tmp_path_factory.mktemp("workspace")
    shutil.copy(REPO / "targets.example.yaml", ws / "targets.example.yaml")
    previous = os.environ.get("RADAR_HOME")
    os.environ["RADAR_HOME"] = str(ws)
    from radar import workspace
    workspace.set_home(None)          # make sure nothing pinned it earlier
    yield ws
    if previous is None:
        os.environ.pop("RADAR_HOME", None)
    else:
        os.environ["RADAR_HOME"] = previous

PAGES_DIR = Path(__file__).parent / "pages"

# Named in the CSP but never requested — this is what proves the header layer
# finds tools a plain visit cannot see.
CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.useinsider.com https://js.appboycdn.com "
    "https://www.googletagmanager.com; "
    "connect-src 'self' https://collect.hidden-vendor.io"
)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.startswith("/csp_page.html"):
            self.send_header("Content-Security-Policy", CSP_HEADER)
        self.send_header("X-Powered-By", "TestHarness/1.0")
        super().end_headers()

    def log_message(self, *args):  # keep pytest output readable
        pass


@pytest.fixture(scope="session")
def site() -> str:
    handler = functools.partial(_Handler, directory=str(PAGES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
