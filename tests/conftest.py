"""
Shared fixtures.

Importing server.py is expensive -- it loads a 2-million-node routing
graph and classifies 127 zones against 191 hazard polygons at module
scope -- so it is imported exactly once per session and shared.
"""

import os
import sys

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "data", "flood", "uttarakhand")

# server.py resolves its data directory relative to itself, but
# routing_engine and the other sibling modules are imported by bare
# name, so the app directory has to be importable.
sys.path.insert(0, APP_DIR)

# pyproj cannot find its own PROJ database in this environment; the
# build pipeline has the same workaround in floodsafe/pipeline/_proj.py.
_PROJ = os.path.join(sys.prefix, "Library", "share", "proj")
if os.path.isfile(os.path.join(_PROJ, "proj.db")):
    os.environ.setdefault("PROJ_DATA_OVERRIDE", _PROJ)


@pytest.fixture(scope="session")
def server():
    import server as server_module
    return server_module


@pytest.fixture()
def client(server):
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_rate_limits(server):
    """Each test starts with a clean allowance.

    Without this, the order tests run in decides whether they pass,
    because the limiter keys on client IP and the test client always
    presents the same one.
    """
    server._rate_buckets.clear()
    yield
    server._rate_buckets.clear()
