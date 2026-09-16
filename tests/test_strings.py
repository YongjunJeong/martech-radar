"""Localisation.

The promise is that another language is a dictionary, not a rewrite. These
tests hold that promise to account: no key may exist in one catalogue and
not the other, and an English render must contain no Korean chrome.
"""

from __future__ import annotations

import re

import pytest

from radar import strings

HANGUL = re.compile(r"[가-힣]")


def test_every_language_has_every_key():
    for language in strings.languages():
        assert strings.missing_keys(language) == [], (
            f"{language} is missing keys; a partial catalogue shows mixed text")


def test_no_catalogue_has_keys_the_default_lacks():
    base = set(strings.CATALOGUE[strings.DEFAULT_LANGUAGE])
    for language in strings.languages():
        extra = sorted(set(strings.CATALOGUE[language]) - base)
        assert not extra, f"{language} defines keys nothing else has: {extra}"


def test_the_english_catalogue_contains_no_korean():
    leaked = [k for k, v in strings.CATALOGUE["en"].items() if HANGUL.search(v)]
    assert leaked == [], f"untranslated English entries: {leaked}"


def test_format_placeholders_match_across_languages():
    # A placeholder present in one language and not the other raises KeyError
    # at render time — on a page someone is looking at.
    def placeholders(text):
        return set(re.findall(r"\{(\w+)", text))
    for key, korean in strings.CATALOGUE["ko"].items():
        assert placeholders(korean) == placeholders(strings.CATALOGUE["en"][key]), key


def test_an_unknown_key_is_loud():
    t = strings.translator("ko")
    with pytest.raises(strings.MissingTranslation):
        t("no.such.key")


def test_an_unknown_language_falls_back_rather_than_crashing():
    t = strings.translator("xx")
    assert t("nav.overview") == strings.CATALOGUE["ko"]["nav.overview"]


def test_company_names_prefer_english_when_asked():
    row = {"company": "가나다몰", "company_en": "Ganada Mall"}
    assert strings.company_name(row, "ko") == "가나다몰"
    assert strings.company_name(row, "en") == "Ganada Mall"
    # No English name on file: show what we have rather than nothing.
    assert strings.company_name({"company": "라마몰", "company_en": None}, "en") == "라마몰"
