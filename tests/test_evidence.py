"""Unit tests for the normalisation rules. No browser required."""

from radar import evidence as ev


def test_query_strings_and_fragments_are_dropped():
    host, path = ev.split_url("https://abc.api.useinsider.com/ins.js?id=123&uid=secret#x")
    assert host == "abc.api.useinsider.com"
    assert path == "/ins.js"


def test_identifying_blobs_are_collapsed_out_of_paths():
    assert ev.normalise_path("/order/1234567890/detail") == "/order/{num}/detail"
    assert ev.normalise_path("/u/550e8400-e29b-41d4-a716-446655440000") == "/u/{uuid}"
    assert ev.normalise_path("/a/deadbeefdeadbeefdead") == "/a/{hex}"


def test_container_ids_survive_because_they_are_configuration():
    ids = ev.extract_identifiers("https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234")
    assert {"kind": "gtm_container", "value": "GTM-ABC1234"} in ids

    ids = ev.extract_identifiers("https://www.googletagmanager.com/gtag/js?id=G-ABC123XYZ")
    assert {"kind": "ga4_measurement", "value": "G-ABC123XYZ"} in ids

    ids = ev.extract_identifiers("https://www.facebook.com/tr/?id=123456789012&ev=PageView")
    assert {"kind": "facebook_pixel", "value": "123456789012"} in ids


def test_hostnames_are_found_inside_script_source():
    js = "var e={collect:'https://sdk.braze.eu/api/v3',b:'cdn.useinsider.com'};"
    hosts = ev.extract_hosts_from_text(js)
    assert "sdk.braze.eu" in hosts
    assert "cdn.useinsider.com" in hosts


def test_property_chains_are_not_mistaken_for_hostnames():
    js = "Object.prototype.hasOwnProperty.call(a,b);e.co=1;t.io=2;"
    assert ev.extract_hosts_from_text(js) == []


def test_csp_is_read_as_a_vendor_allowlist():
    csp = "script-src 'self' https://cdn.useinsider.com *.appboycdn.com; connect-src 'none'"
    hosts = ev.hosts_from_csp(csp)
    assert "cdn.useinsider.com" in hosts
    assert "appboycdn.com" in hosts
    assert "self" not in hosts


def test_personal_data_is_scrubbed_from_console_text():
    text = ev.redact("user person@example.com phone +82 10-1234-5678 id 987654321098")
    assert "person@example.com" not in text
    assert "{email}" in text
    assert "{num}" in text


def test_subdomains_group_under_one_owner():
    assert ev.base_domain("abc.api.useinsider.com") == "useinsider.com"
    assert ev.base_domain("shop.example.co.kr") == "example.co.kr"
    assert ev.base_domain("example.co.kr") == "example.co.kr"


def test_blocking_is_a_distinct_state_from_a_clean_negative():
    assert ev.detect_block(200, "Just a moment...", "") is not None
    assert ev.detect_block(403, "Shop", "") == "http_status:403"
    assert ev.detect_block(200, "Shop", "Welcome to our store") is None


def test_only_ok_counts_as_a_real_observation():
    # The rule that stops a bot wall from ever looking like a removed vendor.
    assert {"OK"} == ev.TRUSTWORTHY_STATUSES
    for status in (ev.STATUS_BLOCKED, ev.STATUS_TIMEOUT,
                   ev.STATUS_NAV_FAILED, ev.STATUS_BROWSER_ERROR):
        assert status not in ev.TRUSTWORTHY_STATUSES


EMPTY_COUNTS = {"requests": 3, "scripts": 0, "window_keys": 0, "cookie_names": 0,
                "csp_hosts": 0, "identifiers": 0, "third_party_domains": 0}


def test_a_maintenance_page_is_not_an_observation():
    """The real case: an insurer answered 200 with a 시스템 점검 notice.

    `detect_block` waves it through — it is a valid response, not a bot wall
    — and it carries no tags. Left as OK it reads as five vendors removed
    overnight.
    """
    assert ev.detect_thin(EMPTY_COUNTS, "시스템 점검 안내 - 예시생명") is not None
    assert ev.detect_thin(EMPTY_COUNTS, "Under Maintenance") is not None


def test_a_site_that_simply_runs_nothing_is_still_a_real_observation():
    """The design decision this rule must not break.

    Emptiness alone cannot mean "cannot tell" — a genuinely bare site is a
    real greenfield finding, and grading it unobservable throws it away.
    """
    assert ev.detect_thin(EMPTY_COUNTS, "회사소개") is None


def test_mentioning_maintenance_is_not_enough_either():
    # A real page with a maintenance notice in its title still loads its stack.
    assert ev.detect_thin({"requests": 118, "scripts": 43}, "정기 점검 안내 | 은행") is None


def test_a_real_page_is_never_thin():
    assert ev.detect_thin({"requests": 118, "scripts": 43}, "쇼핑몰") is None
    assert ev.detect_thin({"requests": 4, "scripts": 1}, "점검") is None


def test_evidence_in_any_single_layer_keeps_the_page_observable():
    # A page whose only trace is a CSP header is exactly what this project
    # exists to read.
    counts = {**EMPTY_COUNTS, "csp_hosts": 4}
    assert ev.detect_thin(counts, "시스템 점검") is None


def test_missing_counts_are_not_read_as_emptiness():
    assert ev.detect_thin({}, "시스템 점검") is None
    assert ev.detect_thin(None, "시스템 점검") is None


def test_storage_key_names_lose_their_per_visitor_blobs():
    # Braze keys its storage by a device UUID. The name is a name, but the
    # random half is an identifier and helps no fingerprint.
    assert ev.normalise_storage_name(
        "ab.storage.deviceId.aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    ) == "ab.storage.deviceId.{uuid}"
    assert ev.normalise_storage_name("_hjSessionUser_1234567890") == "_hjSessionUser_{num}"


def test_normalising_a_storage_name_never_breaks_a_prefix_pattern():
    # Every storage pattern we ship is prefix-anchored, so the tail may go.
    for name, prefix in (("WZRK_G", "WZRK_"), ("INSIDER_WEB_PUSH_DB", "INSIDER_"),
                         ("OptanonConsent", "Optanon"), ("_ga_ABC123", "_ga")):
        assert ev.normalise_storage_name(name).startswith(prefix)


def test_a_query_string_inside_a_storage_key_is_dropped():
    cleaned = ev.normalise_storage_name("pui-storage-//api.test/page?pageId=HOME&uid=42")
    assert cleaned.endswith("?{query}")
    assert "uid=42" not in cleaned


def test_normalising_storage_names_dedupes():
    names = ["ab.storage.deviceId.aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
             "ab.storage.deviceId.11111111-2222-3333-4444-555555555555"]
    assert ev.normalise_storage_names(names) == ["ab.storage.deviceId.{uuid}"]


def test_console_text_loses_query_values():
    text = ev.redact("blocked https://x.test/a.js?token=abc123&u=1 by policy")
    assert "token=abc123" not in text
    assert "{query}" in text
    assert "x.test" in text, "the host is the useful part and must survive"


def test_partner_slugs_are_extracted_as_identifiers():
    from radar.evidence import extract_identifiers
    found = extract_identifiers(
        "https://acme.api.useinsider.com/x __INSIDER_SCRIPT_VERSION_acme__ "
        "https://samplemarket-evt.rmp-api.moloco.com/e")
    pairs = {(f["kind"], f["value"]) for f in found}
    assert ("insider_partner", "acme") in pairs
    assert ("moloco_advertiser", "samplemarket") in pairs
    assert len([f for f in found if f["kind"] == "insider_partner"]) == 1  # deduped
