"""Deployment settings.

The point of this file is that another market's deployment is a config
change, not a patch. So the tests are about that promise: defaults describe
Korea, overrides actually take effect, and a typo is refused loudly rather
than silently ignored.
"""

from __future__ import annotations

import pytest

from radar import config as cfg


def write(tmp_path, text: str):
    path = tmp_path / "radar.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_file_is_fine_and_means_neutral_defaults(tmp_path):
    # No file, no market: English UI, UTC. A deployment says what it is by
    # writing a radar.toml, never by the code guessing.
    settings = cfg.load(tmp_path / "absent.toml")
    assert settings.scan.locale == "en-US"
    assert settings.scan.timezone == "UTC"
    assert settings.ui.language == "en"
    assert settings.source is None


def test_the_shipped_config_loads():
    settings = cfg.load()
    assert settings.scan.concurrency >= 1


def test_another_market_is_a_config_change(tmp_path):
    path = write(tmp_path, """
[scan]
locale = "tr-TR"
timezone = "Europe/Istanbul"
concurrency = 6

[ui]
language = "en"
""")
    settings = cfg.load(path)
    assert settings.scan.locale == "tr-TR"
    assert settings.scan.timezone == "Europe/Istanbul"
    assert settings.scan.concurrency == 6
    assert settings.ui.language == "en"
    # Untouched sections keep their defaults.
    assert settings.vendor.has_home is False


def test_no_home_vendor_is_the_neutral_default(tmp_path):
    # Anyone who is not selling one of the detected platforms wants this.
    assert cfg.load(tmp_path / "absent.toml").vendor.has_home is False


def test_a_home_vendor_can_be_declared(tmp_path):
    settings = cfg.load(write(tmp_path, '[vendor]\nhome = "braze"\n'))
    assert settings.vendor.home == "braze"
    assert settings.vendor.has_home is True


def test_a_typo_is_refused_not_ignored(tmp_path):
    # Silently ignoring `timezon` would leave the browser in Seoul while the
    # operator believed it was in Istanbul.
    with pytest.raises(cfg.ConfigError, match="unknown keys"):
        cfg.load(write(tmp_path, '[scan]\ntimezon = "Europe/Istanbul"\n'))


def test_an_unknown_section_is_refused(tmp_path):
    with pytest.raises(cfg.ConfigError, match="unknown sections"):
        cfg.load(write(tmp_path, '[scanning]\nlocale = "en-US"\n'))


def test_broken_toml_is_reported_clearly(tmp_path):
    with pytest.raises(cfg.ConfigError, match="invalid TOML"):
        cfg.load(write(tmp_path, "[scan\nlocale = "))
