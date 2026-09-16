"""The registry is data, so its failure mode is silence.

A misspelled field or a swallowed YAML key does not crash anything — it just
makes a vendor stop matching, and "nobody uses Braze" reads like a finding
instead of a bug. Every test here is about making that failure loud.
"""

from __future__ import annotations

import pytest

from radar import registry as reg


def write(tmp_path, name: str, text: str):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_shipped_registry_loads_and_has_insider():
    registry = reg.load()
    assert len(registry) >= 50
    insider = registry.get("insider")
    assert insider is not None
    assert insider.category == "engagement"
    assert {s.field for s in insider.signals} >= {"hosts", "window", "storage"}


def test_every_shipped_fingerprint_has_at_least_one_strong_signal():
    # A vendor made only of weak signals can never reach DETECTED on its own,
    # which is almost never what the author intended.
    weak_only = [
        fp.id for fp in reg.load()
        if not any(s.strength == reg.STRENGTH_STRONG for s in fp.signals)
    ]
    # These three are deliberate: they are hosting/CDN hints, not products,
    # and must never reach DETECTED on their own. A fourth entry here is a
    # decision someone has to make consciously.
    assert sorted(weak_only) == ["naver_cloud", "nhn_toast", "sap_hybris"], (
        f"unexpected weak-only fingerprints: {weak_only}"
    )


def test_unknown_field_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", """
category: test
fingerprints:
  - id: thing
    name: Thing
    signals:
      hostz: [example.com]
""")
    with pytest.raises(reg.RegistryError, match="unknown evidence field"):
        reg.load(tmp_path)


def test_duplicate_yaml_key_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", """
category: test
fingerprints:
  - id: thing
    name: Thing
    signals:
      hosts: [example.com]
    signals:
      window: ["Thing"]
""")
    with pytest.raises(reg.RegistryError, match="duplicate key"):
        reg.load(tmp_path)


def test_duplicate_id_across_files_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", "category: t\nfingerprints:\n  - id: dup\n    name: A\n    signals:\n      hosts: [a.com]\n")
    write(tmp_path, "b.yaml", "category: t\nfingerprints:\n  - id: dup\n    name: B\n    signals:\n      hosts: [b.com]\n")
    with pytest.raises(reg.RegistryError, match="duplicate fingerprint id"):
        reg.load(tmp_path)


def test_bad_regex_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", "category: t\nfingerprints:\n  - id: x\n    name: X\n    signals:\n      window: ['re:^(unclosed']\n")
    with pytest.raises(reg.RegistryError, match="bad regex"):
        reg.load(tmp_path)


def test_fingerprint_without_signals_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", "category: t\nfingerprints:\n  - id: x\n    name: X\n")
    with pytest.raises(reg.RegistryError, match="has no signals"):
        reg.load(tmp_path)


def test_stray_key_is_rejected(tmp_path):
    write(tmp_path, "a.yaml", "category: t\nfingerprints:\n  - id: x\n    name: X\n    sigmals:\n      hosts: [a.com]\n")
    with pytest.raises(reg.RegistryError, match="unknown keys"):
        reg.load(tmp_path)
