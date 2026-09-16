"""Change detection.

Every test here is really the same question asked in different shapes: can a
scanning accident be mistaken for a business event? A bot wall, a skipped
run, a one-off miss — none of them may reach a rep as "they dropped Braze".
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar import signals as sig
from radar.store import Store
from radar.targets import Target, TargetSet

BRAZE = {"network": {"hosts": ["sdk.iad-03.braze.com"]},
         "runtime": {"window_keys": ["braze"]}}
MOENGAGE = {"network": {"hosts": ["cdn.moengage.com"]},
            "runtime": {"window_keys": ["Moengage"]}}
INSIDER = {"network": {"hosts": ["acme.api.useinsider.com"]},
           "runtime": {"window_keys": ["Insider"]}}
BUSY = {  # enough advertising + analytics to clear the greenfield bar
    "network": {"hosts": ["connect.facebook.net", "static.criteo.net",
                          "cdn.taboola.com", "widgets.outbrain.com",
                          "www.googletagmanager.com", "www.google-analytics.com",
                          "static.hotjar.com", "api.amplitude.com"]},
    "runtime": {"window_keys": ["fbq", "criteo_q", "_taboola", "obApi",
                                "google_tag_manager", "GoogleAnalyticsObject",
                                "hjBootstrap", "amplitude"]},
}


@pytest.fixture(scope="module")
def registry():
    return reg.load()


@pytest.fixture
def home_insider(monkeypatch):
    """Declare Insider as the home vendor for tests that need the split."""
    from radar import config as cfg
    settings = cfg.Config(vendor=cfg.VendorSettings(home="insider"))
    monkeypatch.setattr(cfg, "load", lambda *a, **k: settings)
    return settings


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="acme", company="에이스몰", industry="fashion",
                            urls=("https://acme.test/",)),),
            industries={"fashion": "패션"}, source="test"))
        yield s


def merge(*parts: dict) -> dict:
    merged: dict = {}
    for part in parts:
        for section, values in part.items():
            for key, items in values.items():
                merged.setdefault(section, {}).setdefault(key, []).extend(items)
    return merged


def record(store, registry, *evidence: dict, status: str = ev.STATUS_OK,
           url: str = "https://acme.test/", target: str = "acme",
           host: str = "acme.test") -> int:
    """One run containing one scan."""
    run_id = store.start_run(1)
    result = {
        "schema_version": 1,
        "scan": {"url": url, "page_host": host, "status": status,
                 # Weekly spacing — the fixture models the default cadence.
                 "started_at": (date(2026, 8, 20) + timedelta(days=7 * run_id)
                                ).isoformat() + "T00:00:00+00:00"},
        "evidence": merge(*evidence) if evidence else {},
        "counts": {}, "warnings": [],
    }
    scan_id = store.record_scan(run_id, target, result)
    store.record_detections(scan_id, det.detect(result, registry))
    return run_id


def states(store) -> dict[str, str]:
    return {c.fingerprint_id: c.state for c in sig.changes_for(store, "acme")}


def kinds(store) -> list[str]:
    return [s.kind for s in sig.all_signals(store)]


# --- the baseline is not an event ------------------------------------------


def test_the_first_run_is_a_baseline_not_a_discovery(store, registry):
    record(store, registry, BRAZE)
    assert states(store)["braze"] == sig.STATE_BASELINE
    assert sig.KIND_COMPETITOR_NEW not in kinds(store)


def test_unchanged_across_runs_is_stable_and_silent(store, registry):
    record(store, registry, BRAZE)
    record(store, registry, BRAZE)
    assert states(store)["braze"] == sig.STATE_STABLE
    assert kinds(store) == []


# --- arrivals ---------------------------------------------------------------


def test_a_vendor_absent_then_present_is_new(store, registry):
    record(store, registry, BUSY)
    record(store, registry, BUSY, BRAZE)
    assert states(store)["braze"] == sig.STATE_NEW
    assert sig.KIND_COMPETITOR_NEW in kinds(store)


def test_our_own_arrival_is_reported_separately(store, registry, home_insider):
    record(store, registry, BUSY)
    record(store, registry, BUSY, INSIDER)
    assert sig.KIND_HOME_NEW in kinds(store)
    assert sig.KIND_COMPETITOR_NEW not in kinds(store)


def test_without_a_home_vendor_every_arrival_is_just_an_arrival(store, registry):
    # The neutral default: nobody's product is special.
    record(store, registry, BUSY)
    record(store, registry, BUSY, INSIDER)
    assert kinds(store) == [sig.KIND_COMPETITOR_NEW]


# --- departures -------------------------------------------------------------


def test_one_absence_is_a_candidate_not_a_removal(store, registry):
    record(store, registry, BRAZE)
    record(store, registry, BRAZE)
    record(store, registry, BUSY)
    assert states(store)["braze"] == sig.STATE_REMOVAL_CANDIDATE
    signal = next(s for s in sig.all_signals(store, language="ko")
                  if s.kind == sig.KIND_COMPETITOR_REMOVAL)
    assert "확인" in signal.headline


def test_two_consecutive_absences_confirm_removal(store, registry):
    record(store, registry, BRAZE)
    record(store, registry, BUSY)
    record(store, registry, BUSY)
    assert states(store)["braze"] == sig.STATE_REMOVED


def test_losing_our_own_account_outranks_everything(store, registry, home_insider):
    record(store, registry, BUSY, INSIDER, BRAZE)
    record(store, registry, BUSY)
    found = sig.all_signals(store)
    assert {s.kind for s in found} >= {sig.KIND_HOME_REMOVAL, sig.KIND_COMPETITOR_REMOVAL}
    assert found[0].kind == sig.KIND_HOME_REMOVAL
    assert all(s.priority > found[0].priority for s in found[1:])


def test_a_vendor_that_comes_back_is_not_reported_as_new(store, registry):
    record(store, registry, BRAZE)
    record(store, registry, BUSY)
    record(store, registry, BUSY, BRAZE)
    assert states(store)["braze"] == sig.STATE_REAPPEARED


# --- the failure modes that matter -----------------------------------------


def test_a_blocked_run_is_skipped_not_counted_as_absence(store, registry):
    record(store, registry, BRAZE)
    record(store, registry, status=ev.STATUS_BLOCKED)
    # One usable observation, so BASELINE — the point is that it is neither
    # REMOVAL_CANDIDATE nor REMOVED.
    assert states(store)["braze"] == sig.STATE_BASELINE, \
        "a bot wall must never look like a removal"
    assert kinds(store) == []


def test_a_timeout_is_skipped_too(store, registry):
    record(store, registry, BRAZE)
    record(store, registry, status=ev.STATUS_TIMEOUT)
    record(store, registry, status=ev.STATUS_NAV_FAILED)
    assert states(store)["braze"] == sig.STATE_BASELINE


def test_a_target_never_scanned_successfully_produces_no_signals(store, registry):
    record(store, registry, status=ev.STATUS_BLOCKED)
    record(store, registry, status=ev.STATUS_BLOCKED)
    assert sig.changes_for(store, "acme") == []
    assert kinds(store) == []


def test_runs_that_skipped_this_target_do_not_count_as_absence(store, registry):
    record(store, registry, BRAZE)
    store.start_run(1)          # a filtered batch: acme was not scanned at all
    store.start_run(1)
    assert states(store)["braze"] == sig.STATE_BASELINE


def test_indirect_only_evidence_never_produces_a_signal(store, registry):
    # Indirect evidence alone must not establish a vendor change.
    record(store, registry, BUSY)
    record(store, registry, BUSY, {"csp_hosts": {"": ["sdk.iad-03.braze.com"]}})
    assert "braze" not in states(store)
    assert sig.KIND_COMPETITOR_NEW not in kinds(store)


def test_a_vendor_seen_on_only_one_url_still_counts(store, registry):
    run = store.start_run(1)
    for url, evidence in (("https://acme.test/", BUSY),
                          ("https://acme.test/pdp", merge(BUSY, BRAZE))):
        result = {"schema_version": 1,
                  "scan": {"url": url, "page_host": "acme.test", "status": ev.STATUS_OK,
                           "started_at": "2026-08-21T00:00:00+00:00"},
                  "evidence": evidence, "counts": {}, "warnings": []}
        scan_id = store.record_scan(run, "acme", result)
        store.record_detections(scan_id, det.detect(result, registry))
    assert "braze" in states(store)


# --- greenfield -------------------------------------------------------------


def test_greenfield_needs_real_marketing_activity(store, registry):
    record(store, registry, BUSY)
    assert sig.KIND_GREENFIELD in kinds(store)


def test_a_thin_site_is_not_a_greenfield_lead_but_is_still_reported(store, registry):
    record(store, registry, {"network": {"hosts": ["www.google-analytics.com"]}})
    assert sig.KIND_GREENFIELD not in kinds(store)
    quiet = sig.quiet_greenfield(store)
    assert [q["target_id"] for q in quiet] == ["acme"], \
        "filtered-out accounts must be visible, not silently dropped"


def test_an_account_with_an_engagement_platform_is_never_greenfield(store, registry):
    record(store, registry, BUSY, BRAZE)
    assert sig.KIND_GREENFIELD not in kinds(store)


def test_a_company_with_zero_detections_still_appears_somewhere(store, registry):
    """The regression that the dashboard's own arithmetic exposed.

    A site scanned successfully where nothing matched has an empty change
    list. Gating on that list dropped the company out of the signal inbox
    *and* the below-threshold list at once — judged, greenfield, invisible.
    """
    record(store, registry, {"network": {"hosts": ["nothing-we-know.test"]}})
    assert sig.changes_for(store, "acme") == []
    assert sig.has_judged_run(store, "acme") is True
    assert [q["target_id"] for q in sig.quiet_greenfield(store)] == ["acme"]


def test_a_removal_candidate_is_not_greenfield_until_confirmed(store, registry):
    """One missed Braze load must not turn last week's Braze customer into a
    "no platform" lead. The account leaves both greenfield buckets while the
    removal is a candidate, and enters them once it is confirmed."""
    record(store, registry, BRAZE)
    record(store, registry, BRAZE)
    record(store, registry, BUSY)
    assert states(store)["braze"] == sig.STATE_REMOVAL_CANDIDATE
    assert sig.KIND_GREENFIELD not in kinds(store)
    assert "acme" not in {q["target_id"] for q in sig.quiet_greenfield(store)}

    record(store, registry, BUSY)                      # a week later, still gone
    assert states(store)["braze"] == sig.STATE_REMOVED
    assert sig.KIND_GREENFIELD in kinds(store)


def test_greenfield_buckets_account_for_every_judged_company(store, registry):
    """Signals + below-threshold must equal the greenfield count, always."""
    record(store, registry, BUSY)                      # qualifies
    signalled = {s.target_id for s in sig.all_signals(store)
                 if s.kind == sig.KIND_GREENFIELD}
    quiet = {q["target_id"] for q in sig.quiet_greenfield(store)}
    assert signalled | quiet == {"acme"}
    assert not (signalled & quiet), "a company must be in exactly one bucket"


def test_a_maintenance_page_cannot_remove_a_whole_stack(store, registry):
    """A maintenance page with HTTP 200 must not imply vendor removal."""
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, BRAZE)
    # The interstitial: OK-looking, but nothing on it.
    run = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://acme.test/", "page_host": "acme.test",
                       "status": ev.STATUS_THIN, "started_at": "2026-08-25T00:00:00+00:00"},
              "evidence": {}, "counts": {"requests": 3, "scripts": 0}, "warnings": []}
    scan_id = store.record_scan(run, "acme", result)
    store.record_detections(scan_id, det.detect(result, registry))

    assert states(store)["braze"] == sig.STATE_STABLE
    assert not [s for s in sig.all_signals(store)
                if s.kind in (sig.KIND_COMPETITOR_REMOVAL, sig.KIND_INSIDER_REMOVAL)]


# --- migration --------------------------------------------------------------


def test_one_platform_out_and_another_in_is_a_single_event(store, registry):
    """The doc's fourth signal archetype.

    Reporting a departure and an arrival separately buries the fact that
    matters — the account swapped, and we know to what.
    """
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, MOENGAGE)

    found = sig.all_signals(store)
    migrations = [s for s in found if s.kind == sig.KIND_COMPETITOR_MIGRATION]
    assert len(migrations) == 1
    assert set(migrations[0].vendors) == {"Braze", "MoEngage"}
    # ...and the halves are not also reported on their own.
    assert sig.KIND_COMPETITOR_REMOVAL not in kinds(store)
    assert sig.KIND_COMPETITOR_NEW not in kinds(store)


def test_being_replaced_outranks_simply_being_dropped(store, registry, home_insider):
    record(store, registry, BUSY, INSIDER)
    record(store, registry, BUSY, BRAZE)
    found = sig.all_signals(store)
    assert found[0].kind == sig.KIND_HOME_MIGRATION
    assert found[0].priority < sig.PRIORITY[sig.KIND_HOME_REMOVAL]
    assert "Insider" in found[0].headline and "Braze" in found[0].headline


def test_a_departure_with_no_arrival_stays_a_plain_removal(store, registry):
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY)
    assert sig.KIND_COMPETITOR_MIGRATION not in kinds(store)
    assert sig.KIND_COMPETITOR_REMOVAL in kinds(store)


def test_a_departure_and_arrival_far_apart_are_not_one_move(store, registry):
    record(store, registry, BUSY, BRAZE)   # run 1
    record(store, registry, BUSY)          # run 2 — Braze gone
    record(store, registry, BUSY)          # run 3 — still gone, now REMOVED
    record(store, registry, BUSY, MOENGAGE)  # run 4 — something else arrives
    found = kinds(store)
    assert sig.KIND_COMPETITOR_MIGRATION not in found, \
        "three runs apart is a separate decision, not one swap"
    assert sig.KIND_COMPETITOR_REMOVAL in found
    assert sig.KIND_COMPETITOR_NEW in found


def test_each_vendor_takes_part_in_at_most_one_migration(store, registry):
    record(store, registry, BUSY, BRAZE, INSIDER)
    record(store, registry, BUSY, MOENGAGE, {"network": {"hosts": ["cdn.moengage.com"]},
                                             "runtime": {"window_keys": ["clevertap"]}})
    migrations = [s for s in sig.all_signals(store)
                  if s.kind in (sig.KIND_COMPETITOR_MIGRATION, sig.KIND_HOME_MIGRATION)]
    paired = [v for m in migrations for v in m.vendors]
    assert len(paired) == len(set(paired)), "a vendor was reported in two moves"


# --- cadence-aware windows ---------------------------------------------------


def record_on(store, registry, day, *evidence, target="acme"):
    """One judged run stamped a given number of days after the epoch."""
    run_id = store.start_run(1)
    stamp = (date(2026, 8, 1) + timedelta(days=day)).isoformat()
    result = {"schema_version": 1,
              "scan": {"url": "https://acme.test/", "page_host": "acme.test",
                       "status": ev.STATUS_OK,
                       "started_at": f"{stamp}T00:00:00+00:00"},
              "evidence": merge(*evidence) if evidence else {},
              "counts": {}, "warnings": []}
    scan_id = store.record_scan(run_id, target, result)
    store.record_detections(scan_id, det.detect(result, registry))


def test_two_daily_absences_are_not_a_removal(store, registry):
    """Under a P1 daily cadence, two runs is two days of evidence — a
    candidate, not the confirmed fact two weekly runs used to mean."""
    record_on(store, registry, 0, BUSY, BRAZE)
    record_on(store, registry, 1, BUSY)
    record_on(store, registry, 2, BUSY)
    assert states(store)["braze"] == sig.STATE_REMOVAL_CANDIDATE


def test_a_weeklong_daily_absence_is_confirmed(store, registry):
    record_on(store, registry, 0, BUSY, BRAZE)
    for day in range(1, 9):                     # absent days 1..8, span 7d
        record_on(store, registry, day, BUSY)
    assert states(store)["braze"] == sig.STATE_REMOVED


def test_daily_runs_do_not_shatter_a_migration(store, registry):
    """Braze out on day 2, MoEngage in on day 5 — three runs apart, which
    the old +-1-run window split into two unrelated events."""
    record_on(store, registry, 0, BUSY, BRAZE)
    record_on(store, registry, 1, BUSY, BRAZE)
    record_on(store, registry, 2, BUSY)
    record_on(store, registry, 3, BUSY)
    record_on(store, registry, 4, BUSY)
    record_on(store, registry, 5, BUSY, MOENGAGE)
    migrations = [s for s in sig.all_signals(store)
                  if s.kind == sig.KIND_COMPETITOR_MIGRATION]
    assert len(migrations) == 1
    assert set(migrations[0].vendors) == {"Braze", "MoEngage"}
    assert sig.KIND_COMPETITOR_REMOVAL not in kinds(store)


def test_changes_written_before_since_at_still_load():
    payload = {"target_id": "t", "fingerprint_id": "f", "name": "F",
               "category": "engagement", "state": "STABLE",
               "since_run": 1, "run_streak": 2, "judged_runs": 2}
    change = sig.change_from_dict(payload)      # pre-v2 summary rows
    assert change.since_at is None


# --- a different landing site is not a changed stack -------------------------


def test_a_subdomain_move_is_still_the_same_site(store, registry):
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, host="shop.acme.test")
    assert states(store)["braze"] == sig.STATE_REMOVAL_CANDIDATE
    assert sig.KIND_HOST_CHANGED not in kinds(store)


def test_landing_on_another_domain_resets_history_and_says_so(store, registry):
    """travel.travel.example landed on nol.booking.example one week: Braze was not
    removed from the original site, we simply looked at another site. No removal, no
    greenfield for a page that is not theirs — one HOST_CHANGED signal."""
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, host="nol.other.test")
    assert "braze" not in states(store)                 # history restarted on the new site
    found = sig.all_signals(store)
    assert [s.kind for s in found] == [sig.KIND_HOST_CHANGED]
    assert "acme.test" in found[0].headline and "other.test" in found[0].headline
    assert "acme" not in {q["target_id"] for q in sig.quiet_greenfield(store)}


def test_coming_back_onto_the_watched_domain_allows_greenfield_again(store, registry):
    record(store, registry, BUSY, BRAZE, host="old.partner.test")
    record(store, registry, BUSY, host="acme.test")
    found = {s.kind for s in sig.all_signals(store)}
    assert sig.KIND_HOST_CHANGED in found and sig.KIND_GREENFIELD in found
    assert sig.KIND_COMPETITOR_REMOVAL not in found


def test_the_host_change_signal_clears_once_the_new_site_has_a_second_run(store, registry):
    record(store, registry, BUSY, BRAZE)
    record(store, registry, BUSY, host="x.other.test")
    record(store, registry, BUSY, host="x.other.test")
    assert sig.KIND_HOST_CHANGED not in kinds(store)
    assert "braze" not in states(store)


def test_registrable_domain_heuristic():
    from radar import hosts
    assert hosts.registrable("nol.travel.example") == "travel.example"
    assert hosts.registrable("https://www.shop.foo.co.kr/x") == "foo.co.kr"
    assert hosts.registrable("www.booking.example") == "booking.example"
    assert hosts.off_domain(["https://travel.travel.example/"], ["nol.booking.example"])
    assert not hosts.off_domain(["https://travel.travel.example/"], ["nol.travel.example"])
    assert not hosts.off_domain([], ["nol.booking.example"])


def test_a_brand_that_moved_domains_is_still_on_domain():
    from radar import hosts
    assert not hosts.off_domain(["https://www.samplebrand.co.kr/"], ["www.samplebrand.com"])
    assert not hosts.off_domain(["https://www.samplecola.co.kr/"], ["www.sample-cola.com"])
    assert not hosts.off_domain(["https://www.samplebrandusa.com/"], ["samplebrand.co.kr"])
    assert hosts.off_domain(["https://www.samplefresh.com/"], ["www.samplemarket.com"])
    assert hosts.off_domain(["https://travel.travel.example/"], ["nol.booking.example"])


def test_chromes_error_page_is_not_an_observation(store, registry):
    """Playwright commits a navigation to chrome-error://chromewebdata and
    hands back a page. That page is Chrome's. Stored as OK before the rule
    existed, redetect must re-grade it and its host must not enter history."""
    from radar import batch as bat
    record(store, registry, BUSY, BRAZE)
    run_id = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://acme.test/", "final_url": "chrome-error://chromewebdata/",
                       "page_host": "chromewebdata", "status": ev.STATUS_OK,
                       "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": {}, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run_id, "acme", result)
    store.record_detections(scan_id, det.detect(result, registry))
    assert sig.KIND_HOST_CHANGED in kinds(store)          # the bad row is in play

    stats = bat.redetect(store, registry)
    assert stats["restatused"] == 1
    assert states(store)["braze"] == sig.STATE_BASELINE   # the bad row is out of history
    assert sig.KIND_HOST_CHANGED not in kinds(store)


def test_a_bot_manager_challenge_page_is_a_block_even_when_stored_as_ok(store, registry):
    from radar import batch as bat
    record(store, registry, BUSY, BRAZE)
    run_id = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://acme.test/", "page_host": "cdn-botmanager.wall.test",
                       "final_url": "https://cdn-botmanager.wall.test/sample/challenge/index.html",
                       "title": "Security Verification", "http_status": 200,
                       "status": ev.STATUS_OK, "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": {}, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run_id, "acme", result)
    store.record_detections(scan_id, det.detect(result, registry))
    stats = bat.redetect(store, registry)
    assert stats["restatused"] == 1
    assert states(store)["braze"] == sig.STATE_BASELINE
    assert sig.KIND_HOST_CHANGED not in kinds(store)


def test_a_site_that_always_lived_on_another_domain_is_taken_at_its_word(store, registry):
    """old.example has landed on new.example since the first run: that is where
    the company's site is, not a sign we are looking at someone else."""
    record(store, registry, BUSY, host="www.new.test")
    record(store, registry, BUSY, host="www.new.test")
    assert sig.KIND_GREENFIELD in kinds(store)
    assert sig.KIND_HOST_CHANGED not in kinds(store)


def test_a_parking_page_or_s3_gate_is_a_block_not_a_look(store, registry):
    from radar import batch as bat
    record(store, registry, BUSY, BRAZE)
    run_id = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://acme.test/", "status": ev.STATUS_OK, "http_status": 200,
                       "page_host": "demo-bucket.s3.eu-west-1.amazonaws.com",
                       "final_url": "https://demo-bucket.s3.eu-west-1.amazonaws.com/index.html",
                       "title": "", "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": {}, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run_id, "acme", result)
    store.record_detections(scan_id, det.detect(result, registry))
    assert ev.detect_block(200, "", "", "demo-bucket.s3.eu-west-1.amazonaws.com") == "standin_host:amazonaws.com"
    stats = bat.redetect(store, registry)
    assert stats["restatused"] == 1
    assert states(store)["braze"] == sig.STATE_BASELINE
