"""The watchlist is hand-edited, so its validation has to be pedantic.

Almost everything here guards a *silent* failure: a typo'd industry that
fragments the dashboard, two targets fighting over one URL, an id that
changed and orphaned its own history.
"""

from __future__ import annotations

import pytest

from radar import targets as tgt

BASE = """
industries:
  fashion: 패션
  beauty: 뷰티
targets:
  - id: shop
    company: 가나다몰
    industry: fashion
    urls: [https://shop.example/]
"""


def write(tmp_path, text: str):
    path = tmp_path / "targets.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_example_watchlist_loads_and_is_complete():
    # The example is what a new install starts from, so it has to be valid
    # and every company in it has to have somewhere to look.
    watchlist = tgt.load(tgt.default_path().with_name("targets.example.yaml"))
    assert len(watchlist) >= 1
    assert all(t.urls for t in watchlist)
    assert {t.industry for t in watchlist} <= set(watchlist.industries)


def test_every_industry_used_is_declared():
    watchlist = tgt.load(tgt.default_path().with_name("targets.example.yaml"))
    assert {t.industry for t in watchlist} <= set(watchlist.industries)


def test_minimal_file_loads(tmp_path):
    watchlist = tgt.load(write(tmp_path, BASE))
    assert len(watchlist) == 1
    target = watchlist.get("shop")
    assert target.primary_url == "https://shop.example/"
    assert target.enabled is True


def test_undeclared_industry_is_rejected(tmp_path):
    path = write(tmp_path, BASE.replace("industry: fashion", "industry: fasion"))
    with pytest.raises(tgt.TargetError, match="not declared"):
        tgt.load(path)


def test_duplicate_id_is_rejected(tmp_path):
    path = write(tmp_path, BASE + """
  - id: shop
    company: 가나다몰 둘
    industry: beauty
    urls: [https://example.com/]
""")
    with pytest.raises(tgt.TargetError, match="duplicate target id"):
        tgt.load(path)


def test_same_url_under_two_targets_is_rejected(tmp_path):
    # Otherwise one company's stack silently overwrites another's.
    path = write(tmp_path, BASE + """
  - id: shop_kr
    company: 가나다몰 코리아
    industry: fashion
    urls: [https://shop.example/]
""")
    with pytest.raises(tgt.TargetError, match="is listed by both"):
        tgt.load(path)


def test_url_must_be_absolute(tmp_path):
    path = write(tmp_path, BASE.replace("https://shop.example/", "shop.example"))
    with pytest.raises(tgt.TargetError, match="must start with http"):
        tgt.load(path)


def test_unknown_key_is_rejected(tmp_path):
    path = write(tmp_path, BASE + "    sector: retail\n")
    with pytest.raises(tgt.TargetError, match="unknown keys"):
        tgt.load(path)


def test_duplicate_yaml_key_is_rejected(tmp_path):
    path = write(tmp_path, BASE + "    urls: [https://other.example/]\n")
    with pytest.raises(tgt.TargetError, match="duplicate key"):
        tgt.load(path)


def test_disabled_targets_are_kept_but_not_scanned(tmp_path):
    path = write(tmp_path, BASE + """
  - id: retired
    company: 옛날몰
    industry: beauty
    enabled: false
    urls: [https://retired.example/]
""")
    watchlist = tgt.load(path)
    assert len(watchlist) == 2
    assert [t.id for t in watchlist.enabled()] == ["shop"]


def test_an_industry_can_carry_a_second_label(tmp_path):
    path = write(tmp_path, """
industries:
  fashion:
    label: 패션
    label_en: Fashion
targets:
  - id: a
    company: A
    industry: fashion
    urls: [https://a.test/]
""")
    watchlist = tgt.load(path)
    assert watchlist.industry_label("fashion") == "패션"
    assert watchlist.industry_label("fashion", "en") == "Fashion"


def test_the_bare_label_form_still_loads(tmp_path):
    # A watchlist written before the lookup existed must keep working.
    watchlist = tgt.load(write(tmp_path, BASE))
    assert watchlist.industry_label("fashion") == "패션"
    # No English label on file: fall back rather than show a blank.
    assert watchlist.industry_label("fashion", "en") == "패션"


def test_a_typo_in_an_industry_table_is_refused(tmp_path):
    path = write(tmp_path, """
industries:
  fashion:
    label: 패션
    labell_en: oops
targets:
  - id: a
    company: A
    industry: fashion
    urls: [https://a.test/]
""")
    with pytest.raises(tgt.TargetError, match="unknown keys"):
        tgt.load(path)
