"""Page runtime interference and navigation must not create false absences."""
import asyncio
from urllib.parse import urlsplit

import pytest

from radar import collector as module, evidence as ev
from radar.collector import Collector, ScanConfig


def visit(monkeypatch, pages, path="/", **settings):
    async def run():
        async with Collector(ScanConfig(
                **{**dict(settle_ms=100, interact=False, nav_timeout_ms=1000,
                          hard_timeout_ms=5000, per_site_delay_ms=0), **settings})) as c:
            original = c._browser.new_context

            async def context(**kwargs):
                ctx = await original(**kwargs)

                async def serve(route):
                    name = urlsplit(route.request.url).path
                    if name == "/robots.txt":
                        await route.fulfill(body="User-agent: *\nDisallow:", content_type="text/plain")
                    else:
                        reply = dict(pages.get(name, dict(status=404, body="not found")))
                        await asyncio.sleep(reply.pop('delay', 0))
                        await route.fulfill(**reply)

                await ctx.route("**/*", serve)
                return ctx

            monkeypatch.setattr(c._browser, "new_context", context)
            return await c.scan("http://radar.test" + path)
    return asyncio.run(run())


def html(body, status=200, **headers):
    return dict(body=body, status=status, content_type="text/html; charset=utf-8", headers=headers)


@pytest.mark.parametrize('scheme', ['blob:https://radar.test/local-script', 'data:text/javascript,void(0)'])
def test_local_script_without_finished_event_does_not_hold_network_gate(scheme):
    from unittest.mock import Mock
    recorder = module._SessionRecorder(ScanConfig())
    recorder.readiness_window_key = 'storeReady'
    request = Mock(url=scheme, resource_type='script')
    recorder._on_request(request)
    assert not recorder.pending_scripts
    assert recorder.last_script_activity > 0  # still restarts the quiet window
    assert not recorder.requests


def test_real_script_still_requires_completion_or_failure():
    from unittest.mock import Mock
    recorder = module._SessionRecorder(ScanConfig())
    recorder.readiness_window_key = 'storeReady'
    request = Mock(url='https://radar.test/late.js', resource_type='script', method='GET')
    recorder._on_request(request)
    assert request in recorder.pending_scripts
    recorder._on_request_failed(request)
    assert not recorder.pending_scripts


def test_blob_bootstrap_and_real_followup_script_are_observed(monkeypatch):
    result = visit(monkeypatch, {
        '/': html('''<body>Store<script>
          const script = document.createElement('script');
          script.src = URL.createObjectURL(new Blob([
            "window.storeReady=true; const s=document.createElement('script');"
            + "s.src='/late.js'; document.body.appendChild(s);"
          ], {type: 'text/javascript'}));
          document.body.appendChild(script);
        </script></body>'''),
        '/late.js': dict(body='window.FollowupVendor={};',
                         content_type='text/javascript', delay=.3),
    }, readiness_window_key_by_host={'radar.test': 'storeReady'}, readiness_timeout_ms=2000)
    assert result['scan']['status'] == ev.STATUS_OK
    assert result['scan']['config']['browser_channel'] == 'chromium'
    assert result['scan']['observation']['stable']
    assert 'FollowupVendor' in result['evidence']['runtime']['window_keys']


def test_legacy_map_override_does_not_discard_runtime_evidence(monkeypatch):
    result = visit(monkeypatch, {"/": html("""<html><body>Store
        <script>window.Map = function LegacyMap() {};
        window.FakeVendor = {}; localStorage.setItem('fv.session', 'private-value');</script>
        </body></html>""")})
    assert result["scan"]["status"] == ev.STATUS_OK
    assert "FakeVendor" in result["evidence"]["runtime"]["window_keys"]
    assert "fv.session" in result["evidence"]["storage"]["local_storage_keys"]
    assert "private-value" not in str(result)


@pytest.mark.parametrize("value", ["null", "[]", "{page_info: null}"])
def test_invalid_probe_is_not_a_normal_empty_stack(monkeypatch, value):
    monkeypatch.setattr(module, "_PROBE_JS", "async () => (" + value + ")")
    result = visit(monkeypatch, {"/": html("<html><body>Store</body></html>")})
    assert result["scan"]["status"] == ev.STATUS_BROWSER_ERROR
    assert result["scan"]["error"] is None  # no Python AttributeError
    assert result["evidence"]["network"]["requests"]  # retained despite bad runtime
    assert any("invalid structure" in w for w in result["warnings"])


def test_navigation_during_probe_observes_final_document_once_stable(monkeypatch):
    monkeypatch.setattr(module, "_PROBE_JS", """async () => {
        await new Promise(resolve => setTimeout(resolve, 400));
        return {page_info: {title: document.title}, window_keys: Object.keys(window)};
    }""")
    result = visit(monkeypatch, {
        "/": html("<script>setTimeout(() => location.href='/final', 250)</script>Old"),
        "/final": html("<title>Final</title><script>window.FinalVendor={}</script>Store"),
    })
    assert result["scan"]["status"] == ev.STATUS_OK
    assert result["scan"]["title"] == "Final"
    assert "FinalVendor" in result["evidence"]["runtime"]["window_keys"]
    assert any("interrupted or invalid" in w for w in result["warnings"])


def test_final_document_status_and_headers_override_redirect_and_iframe(monkeypatch):
    result = visit(monkeypatch, {
        "/": html("<script>setTimeout(() => location.href='/final', 50)</script>Old"),
        "/final": html("<title>Restricted</title><iframe src='/frame'></iframe>", status=403,
                       **{"x-powered-by": "FinalServer"}),
        "/frame": html("<title>Frame</title>Frame", **{"x-powered-by": "WrongFrame"}),
    }, settle_ms=200)
    assert result["scan"]["http_status"] == 403
    assert result["scan"]["status"] == ev.STATUS_BLOCKED
    assert result["evidence"]["headers"]["x-powered-by"] == "FinalServer"


def test_continuous_navigation_still_hits_the_hard_deadline(monkeypatch):
    body = "<script>setTimeout(() => location.reload(), 40)</script>Redirecting"
    result = visit(monkeypatch, {"/": html(body)}, hard_timeout_ms=700)
    assert result["scan"]["status"] == ev.STATUS_TIMEOUT
    assert not result["evidence"]
    assert any("hard timeout" in w for w in result["warnings"])


def test_application_error_screen_is_not_a_usable_observation(monkeypatch):
    body = ("<title>메리츠증권</title><body>홈페이지 이용 중 오류가 발생하였습니다. "
            "로그인페이지로 이동 메인화면으로 이동<script>window.FakeVendor={}</script></body>")
    result = visit(monkeypatch, {"/": html(body)})
    assert result["scan"]["status"] == ev.STATUS_THIN
    assert result["scan"]["block_marker"] == "thin:application error screen"
    # An article mentioning an error must not be mistaken for the error screen.
    assert ev.detect_thin({}, "공지", "서비스 안내: " + body) is None


def test_host_floor_captures_late_body_tag_without_changing_other_sites(monkeypatch):
    pages = {'/': html('''<body>Store<script>
      setTimeout(() => { const s=document.createElement('script');
      s.src='/late.js';document.body.appendChild(s); }, 400);
      </script></body>'''), '/late.js': dict(content_type='application/javascript', body='window.LateVendor={};')}
    ordinary = visit(monkeypatch, pages, minimum_observation_ms_by_host={'unrelated.test': 650})
    assert 'LateVendor' not in ordinary['evidence']['runtime']['window_keys']
    assert 'observation' not in ordinary['scan']
    late = visit(monkeypatch, pages, minimum_observation_ms_by_host={'radar.test': 650})
    assert late['scan']['status'] == ev.STATUS_OK
    assert late['scan']['observation']['observed_ms'] >= 650
    assert late['scan']['observation']['stable']
    assert 'LateVendor' in late['evidence']['runtime']['window_keys']
    assert any(s.endswith('/late.js') for s in late['evidence']['scripts'])


def test_slow_script_completion_gets_a_quiet_window(monkeypatch):
    result = visit(monkeypatch, {
        '/': html("<body>Store<script async src='/slow.js'></script></body>"),
        '/slow.js': dict(content_type='application/javascript', body='window.LateVendor={};', delay=.8),
    }, minimum_observation_ms_by_host={'radar.test': 200}, late_script_grace_ms=1500)
    assert result['scan']['status'] == ev.STATUS_OK
    assert result['scan']['observation']['pending_scripts'] == 0
    assert 'LateVendor' in result['evidence']['runtime']['window_keys']


def test_unfinished_late_script_is_partial_not_trustworthy_absence(monkeypatch):
    result = visit(monkeypatch, {
        '/': html("<body>Store<script async src='/slow.js'></script></body>"),
        '/slow.js': dict(content_type='application/javascript', body='window.LateVendor={};', delay=3),
    }, minimum_observation_ms_by_host={'radar.test': 200}, late_script_grace_ms=300)
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert not result['scan']['observation']['stable']
    assert result['scan']['observation']['pending_scripts'] == 1
    assert result['scan']['duration_ms'] < 2500
    assert result['scan']['status'] not in ev.TRUSTWORTHY_STATUSES


def test_late_profile_cannot_exceed_hard_timeout(monkeypatch):
    result = visit(monkeypatch, {'/': html('<body>Store</body>')},
                   minimum_observation_ms_by_host={'radar.test': 4000}, hard_timeout_ms=600)
    assert result['scan']['status'] == ev.STATUS_TIMEOUT
    assert not result['evidence']


def test_redirect_restarts_late_observation_floor(monkeypatch):
    result = visit(monkeypatch, {
        '/': html("<script>setTimeout(()=>location.href='/final',300)</script>Store"),
        '/final': html("<script>setTimeout(()=>window.LateVendor={},400)</script>Final store"),
    }, minimum_observation_ms_by_host={'radar.test': 650})
    assert result['scan']['status'] == ev.STATUS_OK
    assert result['scan']['observation']['observed_ms'] >= 650
    assert 'LateVendor' in result['evidence']['runtime']['window_keys']


def test_bootstrap_checkpoint_waits_for_followup_sdk(monkeypatch):
    result = visit(monkeypatch, {
        '/': html('''<body>Store<script>
            setTimeout(() => { window.storeReady = function() {};
                setTimeout(() => {const s=document.createElement('script');
                    s.src='/late.js';document.body.appendChild(s)}, 100);
            }, 350);
        </script></body>'''),
        '/late.js': dict(content_type='application/javascript', body='window.LateVendor={};', delay=.15),
    }, readiness_window_key_by_host={'radar.test': 'storeReady'}, readiness_timeout_ms=2000)
    assert result['scan']['status'] == ev.STATUS_OK
    assert result['scan']['observation']['checkpoint_observed']
    assert 'LateVendor' in result['evidence']['runtime']['window_keys']


def test_complete_document_without_bootstrap_cannot_establish_absence(monkeypatch):
    result = visit(monkeypatch, {'/': html('<body>Store</body>')},
                   readiness_window_key_by_host={'radar.test': 'storeReady'}, readiness_timeout_ms=600)
    observation = result['scan']['observation']
    assert observation['ready_state'] == 'complete'
    assert observation['load_event_observed']
    assert not observation['checkpoint_observed']
    assert not observation['stable']
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert result['scan']['duration_ms'] < 2500


def test_ready_checkpoint_exits_early_even_without_any_vendor(monkeypatch):
    result = visit(monkeypatch, {'/': html('<body>Store<script>window.storeReady=()=>{};</script></body>')},
                   readiness_window_key_by_host={'radar.test': 'storeReady'}, readiness_timeout_ms=4000)
    assert result['scan']['status'] == ev.STATUS_OK
    assert result['scan']['observation']['stable']
    assert result['scan']['observation']['observed_ms'] < 2000
    assert 'LateVendor' not in result['evidence']['runtime']['window_keys']


def test_bootstrap_profile_does_not_change_other_hosts(monkeypatch):
    result = visit(monkeypatch, {'/': html('<body>Store</body>')},
                   readiness_window_key_by_host={'unrelated.test': 'storeReady'})
    assert result['scan']['status'] == ev.STATUS_OK
    assert 'observation' not in result['scan']


def test_bootstrap_script_grace_is_bounded(monkeypatch):
    result = visit(monkeypatch, {
        '/': html('''<body>Store<script>window.storeReady=()=>{};
          setTimeout(()=>{const s=document.createElement('script');s.src='/slow.js';
            document.body.appendChild(s)},50)</script></body>'''),
        '/slow.js': dict(content_type='application/javascript', body='window.LateVendor={}', delay=3),
    }, readiness_window_key_by_host={'radar.test': 'storeReady'},
        readiness_timeout_ms=500, late_script_grace_ms=300)
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert result['scan']['observation']['pending_scripts'] == 1
    assert result['scan']['duration_ms'] < 2500


def test_bootstrap_checkpoint_is_reset_after_navigation(monkeypatch):
    result = visit(monkeypatch, {
        '/': html('<script>window.storeReady=()=>{};setTimeout(()=>location.href="/final",200)</script>Old'),
        '/final': html('<body>Final store</body>'),
    }, readiness_window_key_by_host={'radar.test': 'storeReady'}, readiness_timeout_ms=600)
    assert result['scan']['final_url'].endswith('/final')
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert not result['scan']['observation']['checkpoint_observed']


def test_bootstrap_profile_still_obeys_hard_deadline(monkeypatch):
    result = visit(monkeypatch, {'/': html('<body>Store</body>')},
                   readiness_window_key_by_host={'radar.test': 'storeReady'},
                   readiness_timeout_ms=4000, hard_timeout_ms=600)
    assert result['scan']['status'] == ev.STATUS_TIMEOUT


def test_no_committed_document_skips_futile_observation(monkeypatch):
    from radar import detector, registry
    async def unexpected(*args, **kwargs):
        pytest.fail('An uncommitted document must not enter observation waits')
    monkeypatch.setattr(Collector, '_nudge', unexpected)
    monkeypatch.setattr(Collector, '_run_probe', unexpected)
    monkeypatch.setattr(Collector, '_wait_for_settle', unexpected)
    result = visit(monkeypatch, {'/': dict(**html('<body>Too late</body>'), delay=2)},
                   nav_timeout_ms=150, hard_timeout_ms=3000, interact=True)
    assert result['scan']['status'] == ev.STATUS_TIMEOUT
    assert result['scan']['stop_reason'] == 'navigation_uncommitted'
    assert result['scan']['duration_ms'] < 1500
    assert not result['evidence']
    assert detector.detect(result, registry.load())['judged'] is False
    timings = result['scan']['timings_ms']
    assert timings['navigation'] >= 100
    assert 'settle' not in timings and 'probe' not in timings
    assert sum(timings.values()) <= result['scan']['duration_ms'] + 2


def test_timings_survive_hard_timeout_and_keep_observation_floor(monkeypatch):
    result = visit(monkeypatch, {'/': html('<body>Store</body>')},
                   minimum_observation_ms_by_host={'radar.test': 4000}, hard_timeout_ms=600)
    assert result['scan']['status'] == ev.STATUS_TIMEOUT
    assert result['scan']['stop_reason'] == 'hard_timeout'
    assert result['scan']['timings_ms']['settle'] > 100
    assert 'cleanup' in result['scan']['timings_ms']


def test_committed_slow_script_keeps_full_observation_and_timings(monkeypatch):
    result = visit(monkeypatch, {
        '/': html('<body>Store<script>window.storeReady=()=>{};</script><script async src="/late.js"></script></body>'),
        '/late.js': dict(content_type='application/javascript', body='window.LateVendor={};', delay=.5),
    }, readiness_window_key_by_host={'radar.test': 'storeReady'}, readiness_timeout_ms=2000)
    assert result['scan']['status'] == ev.STATUS_OK
    assert 'LateVendor' in result['evidence']['runtime']['window_keys']
    assert result['scan']['observation']['stable']
    assert result['scan']['timings_ms']['bootstrap'] >= 200
    assert 'stop_reason' not in result['scan']


@pytest.mark.parametrize('runtime', [
    'indexedDB.databases = () => new Promise(() => {});',
    "Object.defineProperty(window, 'Insider', {get() {while (true) {}}});",
])
def test_stalled_runtime_is_bounded_and_not_judged(monkeypatch, runtime):
    from radar import detector, registry
    result = visit(monkeypatch, {'/': html('<body>Store<script>' + runtime + '</script></body>')},
                   probe_timeout_ms=200, hard_timeout_ms=3000)
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert result['scan']['stop_reason'] == 'probe_timeout'
    assert result['scan']['duration_ms'] < 2000
    assert result['scan']['timings_ms']['probe'] < 1000
    assert result['evidence']['network']['requests']
    assert detector.detect(result, registry.load())['judged'] is False
    assert not any('waiting for the current page once' in w for w in result['warnings'])


def test_continuously_reloading_document_has_total_settling_limit(monkeypatch):
    from radar import detector, registry
    result = visit(monkeypatch, {'/': html("<body>Store<script>setTimeout(()=>location.reload(),80)</script></body>")},
                   settle_ms=200, settle_timeout_ms=650, hard_timeout_ms=3000)
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert result['scan']['stop_reason'] == 'settle_timeout'
    assert result['scan']['document_navigations'] >= 2
    assert result['scan']['duration_ms'] < 2000
    assert 'probe' not in result['scan']['timings_ms']
    assert detector.detect(result, registry.load())['judged'] is False


def test_probe_timeout_does_not_poison_next_context(monkeypatch):
    async def run():
        async with Collector(ScanConfig(settle_ms=50, interact=False, probe_timeout_ms=150,
                                        hard_timeout_ms=3000, per_site_delay_ms=0)) as collector:
            original = collector._browser.new_context
            async def context(**kwargs):
                ctx = await original(**kwargs)
                async def serve(route):
                    path = urlsplit(route.request.url).path
                    if path == '/robots.txt':
                        await route.fulfill(body='User-agent: *\nDisallow:')
                    else:
                        runtime = ('indexedDB.databases=()=>new Promise(()=>{});' if path == '/stuck'
                                   else 'window.FakeVendor={};')
                        await route.fulfill(**html('<body>Store<script>' + runtime + '</script></body>'))
                await ctx.route('**/*', serve)
                return ctx
            monkeypatch.setattr(collector._browser, 'new_context', context)
            first = await collector.scan('http://radar.test/stuck')
            second = await collector.scan('http://radar.test/good')
            assert first['scan']['stop_reason'] == 'probe_timeout'
            assert second['scan']['status'] == ev.STATUS_OK
            assert 'FakeVendor' in second['evidence']['runtime']['window_keys']
    asyncio.run(run())


def test_stalled_interaction_is_bounded_before_settling(monkeypatch):
    async def nudge(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(Collector, '_nudge', nudge)
    result = visit(monkeypatch, {'/': html('<body>Store</body>')}, interact=True,
                   interaction_ms=100, interaction_timeout_ms=200, hard_timeout_ms=3000)
    assert result['scan']['status'] == ev.STATUS_PARTIAL
    assert result['scan']['stop_reason'] == 'interaction_timeout'
    assert result['scan']['duration_ms'] < 1500
    assert 'settle' not in result['scan']['timings_ms']
    assert result['evidence']['network']['requests']


@pytest.mark.parametrize('message,marker', [
    ('개발자 도구 사용이 불가능합니다.', 'developer tools access denied'),
    ('개발자 도구 사용이 탐지되었습니다!!', 'developer tools access denied'),
    ('시스템에서 비정상적인 활동이 감지되었습니다.', 'unusual activity detected'),
])
def test_explicit_inspection_refusal_stops_before_runtime_probe(monkeypatch, message, marker):
    from radar import detector, registry
    result = visit(monkeypatch, {'/': html('''<body>Store<script>
        setTimeout(()=>{alert('MESSAGE');location.reload()},150);
        </script></body>'''.replace('MESSAGE', message))}, settle_ms=2000, hard_timeout_ms=4000)
    assert result['scan']['status'] == ev.STATUS_BLOCKED
    assert result['scan']['stop_reason'] == 'site_dialog_blocked'
    assert result['scan']['block_marker'] == 'dialog:' + marker
    assert result['scan']['duration_ms'] < 1500
    assert 'probe' not in result['scan']['timings_ms']
    assert detector.detect(result, registry.load())['judged'] is False


def test_ordinary_notice_does_not_mark_site_blocked(monkeypatch):
    result = visit(monkeypatch, {'/': html("<body>Store<script>alert('서비스 안내');window.FakeVendor={};</script></body>")})
    assert result['scan']['status'] == ev.STATUS_OK
    assert 'FakeVendor' in result['evidence']['runtime']['window_keys']
