"""Keep staging separate and fail closed when a sandbox cannot be started."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from radar import collector, config

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'deploy'/f'{name}.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_initialization_cannot_overwrite_workspace(tmp_path):
    initialize = module('init_workspace').initialize
    home = initialize(tmp_path/'stage')
    original = (home/'radar.toml').read_bytes()
    with pytest.raises(FileExistsError):
        initialize(home)
    assert (home/'radar.toml').read_bytes() == original
    assert (home/'radar.toml').stat().st_mode & 0o777 == 0o600
    settings = config.load(home/'radar.toml')
    assert settings.scan.chromium_sandbox
    assert settings.server.requires_login
    assert not settings.preparation.enabled and not settings.notify.webhook
    assert not (home/'data/radar.db').exists()


@pytest.mark.parametrize('value',['1','"true"'])
def test_sandbox_configuration_rejects_non_boolean(tmp_path,value):
    path=tmp_path/'radar.toml'
    path.write_text('[scan]\nchromium_sandbox = '+value)
    with pytest.raises(config.ConfigError,match='chromium_sandbox'):
        config.load(path)


def test_container_refuses_unsandboxed_collector(tmp_path,monkeypatch):
    home=module('init_workspace').initialize(tmp_path/'stage')
    monkeypatch.setenv('RADAR_HOME',str(home))
    monkeypatch.setenv('RADAR_REQUIRE_SANDBOX','1')
    entry=module('entrypoint')
    monkeypatch.setattr(entry.os,'geteuid',lambda:1000)
    entry.validate()
    path=home/'radar.toml'
    path.write_text(path.read_text().replace('chromium_sandbox = true','chromium_sandbox = false'))
    with pytest.raises(ValueError,match='chromium_sandbox'):
        entry.validate()


def test_container_does_not_open_an_existing_production_workspace(tmp_path,monkeypatch):
    home = module('init_workspace').initialize(tmp_path/'production-like')
    (home/'.radar-staging').unlink()
    monkeypatch.setenv('RADAR_HOME',str(home))
    with pytest.raises(ValueError,match='staging workspace'):
        module('entrypoint').validate()
    assert not (home/'data/radar.db').exists()


def test_sandbox_start_failure_never_retries_without_sandbox(monkeypatch):
    launch=AsyncMock(side_effect=RuntimeError('namespace prohibited'))
    pw=SimpleNamespace(chromium=SimpleNamespace(launch=launch),stop=AsyncMock())
    manager=SimpleNamespace(start=AsyncMock(return_value=pw))
    monkeypatch.setattr(collector,'async_playwright',lambda:manager)
    c=collector.Collector(collector.ScanConfig(chromium_sandbox=True))
    with pytest.raises(RuntimeError,match='namespace prohibited'):
        asyncio.run(c.start())
    assert launch.await_count == 1
    assert launch.call_args.kwargs['chromium_sandbox'] is True
    assert '--no-sandbox' not in launch.call_args.kwargs['args']
    pw.stop.assert_awaited_once()
    assert c._pw is None and c._browser is None


def test_existing_deployment_keeps_its_browser_policy(tmp_path):
    assert config.load(tmp_path/'absent.toml').scan.chromium_sandbox is False


def test_container_preparation_uses_secret_without_local_cli(tmp_path,monkeypatch):
    home=module('init_workspace').initialize(tmp_path/'stage', 'http://private-gateway:8612')
    secret=tmp_path/'secret'
    secret.write_text('s'*40)
    path=home/'radar.toml'
    path.write_text(path.read_text().replace('/run/secrets/preparation_gateway_token',str(secret)))
    monkeypatch.setenv('RADAR_HOME',str(home))
    entry=module('entrypoint')
    entry.validate()
    secret.unlink()
    with pytest.raises(OSError):
        entry.validate()
    entry.validate(require_preparation=False)  # collector/reviewer do not need the secret


def test_config_requires_gateway_url_and_secret_together(tmp_path):
    path=tmp_path/'radar.toml'
    path.write_text('[preparation]\ngateway_url="https://private"\n')
    with pytest.raises(config.ConfigError,match='gateway_url'):
        config.load(path)
