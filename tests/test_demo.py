"""The portfolio demo is reproducible without company data or network access."""

import socket

import pytest
from fastapi.testclient import TestClient

from demo.seed import seed
from radar import signals
from radar.store import Store
from web.app import create_app


def test_offline_demo_and_overwrite_guard(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Demo must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    home = tmp_path / "demo"
    database = seed(home)
    with Store(database) as store:
        assert len(store.targets()) == 3
        assert len(store.runs()) == 3
        found = signals.all_signals(store)
        assert any(s.kind == signals.KIND_COMPETITOR_MIGRATION for s in found)
        assert any(s.kind == signals.KIND_GREENFIELD for s in found)
        assert not any(s.target_id == "walled" or s.kind in signals.HOME_KINDS for s in found)
    client = TestClient(create_app(str(database)))
    for path in ("/", "/signals", "/companies/shop_b"):
        response = client.get(path)
        assert response.status_code == 200
        assert "[DEMO]" in response.text
    before = database.read_bytes()
    with pytest.raises(FileExistsError):
        seed(home)
    assert database.read_bytes() == before
