"""Company-level, evidence-only explanations for sales readers."""
from datetime import datetime, date, UTC, timedelta
from math import ceil
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
import json

from radar import config, signals as sig, evidence as ev
from radar.detector import DIRECT_LAYERS
from radar.batch import weekly_due_date

USES = {
    'analytics': ('방문·행동 분석', '고객이 어디서 유입되고 어떻게 행동하는지 분석'),
    'advertising': ('광고 운영', '광고 유입과 성과를 측정하거나 광고를 제공'),
    'engagement': ('고객 메시지·개인화', '고객 메시지와 개인화된 경험을 운영'),
    'attribution': ('유입 성과 측정', '어떤 유입 경로가 전환으로 이어졌는지 측정'),
    'experimentation': ('화면·경험 실험', '화면이나 경험을 비교해 효과를 검증'),
    'platform': ('웹사이트 기반 솔루션', '웹사이트 운영·연결·안정성을 지원'),
}
# Product-level exceptions avoid calling chat, reviews or error monitoring
# generic infrastructure/visitor analytics. Sources: shipped fingerprint notes;
# https://channel.io/kr; https://sentry.io/product/error-monitoring/;
# https://support.google.com/tagmanager/answer/6102821?hl=ko
PRODUCT_USES = {
    'channel_talk': ('고객 상담', '웹사이트 방문 고객과 상담'),
    'crema': ('상품 리뷰', '상품 후기와 고객 콘텐츠를 관리'),
    'sentry': ('오류 모니터링', '웹사이트 오류를 추적하고 문제 해결을 지원'),
    'google_tag_manager': ('태그 관리', '분석·광고 솔루션의 태그를 배포하고 관리'),
}
PRODUCT_TYPES = {
    'channel_talk': 'customer_support', 'crema': 'reviews',
    'sentry': 'error_monitoring', 'google_tag_manager': 'tag_management',
}
SOLUTION_TYPES = {**{key: value[0] for key, value in USES.items()},
                  **{key: PRODUCT_USES[product][0] for product, key in PRODUCT_TYPES.items()},
                  'other': '기타 솔루션'}
SOLUTION_TYPE_PATTERN = '^(' + '|'.join(SOLUTION_TYPES) + ')?$'


def solution_type_of(fingerprint_id, category):
    return PRODUCT_TYPES.get(fingerprint_id, category if category in USES else 'other')


INDUSTRIES = {
    'retail': '쇼핑·이커머스', 'beauty_fashion': '뷰티·패션', 'food_bev': '식음료',
    'finance': '금융', 'health': '헬스케어', 'travel': '여행·숙박',
    'travel_mobility': '여행·모빌리티', 'platform': '플랫폼·교육·게임',
    'media': '미디어·구독', 'enterprise': '기업 서비스', 'public_edu': '공공·교육',
}

ARRIVAL_REVIEW_RUNS = 2

OBSERVATION_PATTERNS = {
    'appeared': dict(label='미검출 → 검출', hint='이전 정상 수집에서는 보이지 않다가 검출됐습니다. 신규 도입을 뜻하지 않습니다.'),
    'missing': dict(label='검출 → 미검출', hint='이전에 검출됐지만 최근 정상 수집에서는 보이지 않습니다. 사용 중단을 뜻하지 않습니다.'),
    'returned': dict(label='다시 검출', hint='검출 → 미검출 → 검출 순서로 관측됐습니다. 중단·재도입을 뜻하지 않습니다.'),
    'baseline': dict(label='첫 수집에서 확인', hint='첫 정상 수집에서 확보한 기준 기록입니다. 이전 사용 여부는 알 수 없습니다.'),
    'stable': dict(label='계속 검출', hint='비교 가능한 정상 수집에서 계속 검출됐습니다.'),
}
OBSERVATION_FILTERS = {**{key: value['label'] for key, value in OBSERVATION_PATTERNS.items()},
                       'fluctuating': '검출·미검출 반복'}
OBSERVATION_FILTER_PATTERN = '^(' + '|'.join(OBSERVATION_FILTERS) + ')?$'


def normalize_filters(value, choices, error):
    """Keep old single-value links/callers; canonicalize repeated query values."""
    selected = {v for v in ([value] if isinstance(value, str) else value or ()) if v}
    if selected - choices.keys():
        raise ValueError(error)
    return tuple(key for key in choices if key in selected)


def normalize_observations(value):
    return normalize_filters(value, OBSERVATION_FILTERS, 'unknown observation pattern')


def normalize_solution_types(value):
    return normalize_filters(value, SOLUTION_TYPES, 'unknown solution type')


def normalize_industries(value, industries):
    return normalize_filters(value, {**INDUSTRIES, **industries}, 'unknown industry')


def observation_filter_query(solution_type, observation, industry=()):
    return urlencode([('solution_type', key) for key in solution_type]
                     + [('observation', key) for key in observation]
                     + [('industry', key) for key in industry])


def apply_industry_context(card, industry, industries):
    """Carry the company-list scope through detail without changing its evidence."""
    card['industry_filter_label'] = ' 또는 '.join(
        industries.get(key, INDUSTRIES.get(key, key)) for key in industry)
    card['filter_query'] = observation_filter_query(card['solution_type'], card['observation'], industry)


PATTERN_BY_STATE = {
    sig.STATE_NEW: 'appeared', sig.STATE_REAPPEARED: 'returned',
    sig.STATE_REMOVAL_CANDIDATE: 'missing', sig.STATE_REMOVED: 'missing',
    sig.STATE_BASELINE: 'baseline', sig.STATE_STABLE: 'stable',
}


def observation_pattern(state, recent, fingerprint_id):
    """Describe normal, analysis-window/site-scoped observations, not adoption.

    Alternation is a secondary tag, so direction totals remain disjoint.
    Use the same eight observations displayed in the evidence history.
    """
    recent = [fingerprint_id in o['vendors'] for o in recent]
    transitions = sum(a != b for a, b in zip(recent, recent[1:], strict=False))
    key = PATTERN_BY_STATE[state]
    return dict(key=key, **OBSERVATION_PATTERNS[key], fluctuating=transitions >= 3,
                fluctuation_note=f'최근 정상 관측 {len(recent)}회에서 검출·미검출 결과가 {transitions}번 바뀌었습니다.')

BUCKETS = {
    'contact': '연락 전 확인', 'watch': '재확인 대기', 'baseline': '비교 기록 부족',
    'steady': '확인된 변화 없음', 'waiting': '첫 수집 대기', 'check': '수집 점검 필요',
}
# Every visible navigation item uses the same membership, title and count as
# its list. Collection sub-states stay on company cards, under one sales view.
VIEWS = {
    'contact': dict(label=BUCKETS['contact'], buckets=('contact',),
                    hint='근거를 읽고 연락 여부를 판단하세요',
                    description='이전과 다른 관측이 반복된 기업입니다. 기업 상세에서 근거와 질문을 읽고 연락 여부를 판단하세요. 실제 도입·중단이나 구매 의향이 확인된 것은 아닙니다.',
                    empty='현재 연락 전 확인할 기업이 없습니다.'),
    'watch': dict(label=BUCKETS['watch'], buckets=('watch',),
                  hint='다음 수집에서 같은 결과가 나오는지 기다려요',
                  description='이전과 다른 흔적이 보였지만 확인이 더 필요한 기업입니다. 이 변화만으로 연락을 서두르지 말고, 다음 수집에서도 같은 결과가 나오는지 기다려 주세요.',
                  empty='현재 재확인을 기다리는 기업이 없습니다.'),
    'steady': dict(label=BUCKETS['steady'], buckets=('steady',),
                   hint='기존 미팅을 준비할 때 참고하세요',
                   description='비교 가능한 수집에서 솔루션의 관측 결과가 유지된 기업입니다. 새 변화에 따른 연락 근거는 없으며, 기존 미팅을 준비할 때 솔루션 정보를 참고할 수 있습니다.',
                   empty='현재 관측 변화가 없는 기업으로 분류된 곳이 없습니다.'),
    'pending': dict(label='아직 판단 어려움', buckets=('baseline', 'waiting', 'check'),
                    hint='기록이 부족하거나 수집 점검이 필요해요',
                    description='아직 수집하지 않았거나, 비교 기록이 부족하거나, 수집 점검이 필요한 기업입니다. 솔루션을 쓰지 않는다는 뜻이 아닙니다. 기업별 사유를 확인하세요.',
                    empty='현재 판단을 기다리는 기업이 없습니다.'),
}
ALL_VIEW = dict(label='전체 기업', description='모니터링 중인 모든 기업입니다. 기업마다 표시된 상태에 따라 연락 전 확인할 곳과 다음 수집을 기다릴 곳을 구분하세요.',
                empty='등록된 모니터링 기업이 없습니다.')

STATUS = {
    'OK': '정상', 'PARTIAL': '일부만 관측됨', 'BLOCKED': '사이트가 접근을 제한함',
    'SKIPPED_BY_ROBOTS': '사이트 수집 정책에 따라 제외', 'TIMEOUT': '응답 시간 초과',
    'NAV_FAILED': '페이지 접속 실패', 'THIN': '관측 정보 부족', 'BROWSER_ERROR': '브라우저 오류',
}


def local_date(value):
    if not value:
        return '아직 없음'
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y.%m.%d')


def company_cards(store, industries, target_id=None, solution_type='', observation='', *, target_ids=None):
    """Use incrementally refreshed facts; no history sweep on fresh requests."""
    solution_type = normalize_solution_types(solution_type)
    solution_type_label = ' 또는 '.join(SOLUTION_TYPES[key] for key in solution_type)
    observation = normalize_observations(observation)
    observation_label = ' 또는 '.join(OBSERVATION_FILTERS[key] for key in observation)
    ids = [target_id] if target_id else target_ids
    summaries = sig.summaries(store, target_ids=ids)
    targets = (store.targets() if ids is None else store.conn.execute(
        'SELECT * FROM target WHERE id IN (' + ','.join('?' for _ in ids) + ')', tuple(ids)))
    cards = []
    for target in targets:
        summary = summaries.get(target['id'])
        observations = summary.observations if summary else {}
        good_count = observations.get('good_runs', 0)
        recent = observations.get('recent', [])
        tools, events = [], []
        changes = summary.changes if summary else []
        if solution_type:
            changes = [c for c in changes if solution_type_of(c.fingerprint_id, c.category) in solution_type]
        patterns = {c.fingerprint_id: observation_pattern(c.state, recent, c.fingerprint_id) for c in changes}
        if observation:
            changes = [c for c in changes if patterns[c.fingerprint_id]['key'] in observation
                       or ('fluctuating' in observation and patterns[c.fingerprint_id]['fluctuating'])]
        for change in changes:
            use, purpose = PRODUCT_USES.get(change.fingerprint_id, USES.get(change.category, ('기타 솔루션', '웹사이트의 기능을 지원')))
            state = change.state
            facts = observations['vendors'][change.fingerprint_id]
            last_seen = local_date(facts['last_seen_at'])
            item = dict(name=change.name, id=change.fingerprint_id, use=use, purpose=purpose,
                        pattern=patterns[change.fingerprint_id],
                        solution_type=solution_type_of(change.fingerprint_id, change.category),
                        state=state, category=change.category, streak=change.run_streak,
                        ready=(state == sig.STATE_REMOVED or (state in (sig.STATE_NEW, sig.STATE_REAPPEARED) and change.run_streak >= ARRIVAL_REVIEW_RUNS)), since=local_date(change.since_at),
                        last_seen=last_seen, observations=change.judged_runs,
                        latest=local_date(observations['last_at']))
            if state in (sig.STATE_BASELINE, sig.STATE_STABLE):
                item.update(label='첫 수집에서 확인' if state == sig.STATE_BASELINE else '계속 확인됨',
                            before='첫 정상 수집' if state == sig.STATE_BASELINE else '확인됨',
                            after='확인됨', confidence='도입 시점은 알 수 없습니다.')
            elif state in (sig.STATE_NEW, sig.STATE_REAPPEARED):
                item.update(label='미검출 후 다시 검출됨' if state == sig.STATE_REAPPEARED else '이번 관측에서 검출됨',
                            before='정상 수집에서 확인되지 않음', after='확인됨',
                            confidence=('이전에도 검출된 솔루션이 미검출 후 다시 보입니다. 중단·재도입을 뜻하지 않습니다.' if state == sig.STATE_REAPPEARED else '이전 정상 수집에서는 보이지 않았던 솔루션입니다. 신규 도입 여부나 실제 도입일은 알 수 없습니다.'))
                item['question'] = f'{use} 솔루션 {change.name}이 최근 공개 페이지에서 확인됐습니다. 관련 운영 방식에 변화가 있었나요?'
                item['reason'] = f'{use} 운영에 변화가 있는지 확인할 계기입니다.'
                events.append(item)
            else:
                confirmed = state == sig.STATE_REMOVED
                item.update(label='이번 관측에서 미검출', before='확인됨 · ' + last_seen,
                            after='확인되지 않음',
                            confidence=f'정상 수집 {change.run_streak}회 연속 미검출. ' +
                            ('7일 이상 반복 확인했지만 사용 중단을 확정하지는 않습니다.' if confirmed
                             else '한 번의 미검출이나 짧은 기간의 재관측으로 사용 중단을 판단하지 않습니다.'))
                item['question'] = f'이전에 확인됐던 {change.name}이 최근 공개 페이지에서는 보이지 않습니다. {use} 방식이나 적용 범위가 달라졌나요?'
                item['reason'] = f'{use} 솔루션의 적용 범위나 운영 방식이 달라졌는지 확인할 계기입니다.'
                events.append(item)
            if state not in (sig.STATE_BASELINE, sig.STATE_STABLE):
                item.update(
                    previous_date=local_date(facts['previous_at']),
                    previous_result='검출' if facts['previous_detected'] else '미검출',
                    latest_result='검출' if change.fingerprint_id in recent[-1]['vendors'] else '미검출',
                    detected_count=facts['detected_count'], missing_count=good_count-facts['detected_count'],
                    evidence_run=facts['evidence_run'], evidence=[],
                    history=[dict(date=datetime.fromisoformat(o['scanned_at']).astimezone(
                        ZoneInfo('Asia/Seoul')).strftime('%Y.%m.%d %H:%M'),
                        result='검출' if change.fingerprint_id in o['vendors'] else '미검출')
                        for o in recent],
                )
            tools.append(item)
        events.sort(key=lambda e: (not e['ready'], e['name']))
        latest_status = summary.last_status if summary else None
        bucket = observation_bucket(observations.get('observed_runs', 0), good_count,
                                    bool(summary and summary.site and summary.site.shift),
                                    latest_status, len(events), any(e['ready'] for e in events))
        reason, next_step = '비교 가능한 관측에서 달라진 솔루션이 없습니다.', '기존 미팅 준비에 현재 솔루션 정보를 활용하세요.'
        if bucket == 'waiting':
            bucket, reason, next_step = 'waiting', '분석 시작일 이후의 관측을 기다리고 있습니다.', '정기 수집 일정에 따라 첫 수집 기록을 확보합니다. 이전 이력을 변화 근거로 쓰지 않습니다.'
        elif not good_count:
            bucket, reason, next_step = 'check', '분석 기간 안에 정상 수집이 없습니다.', '수집 상태를 확인하기 전에는 연락 근거로 쓰지 마세요.'
        elif summary.site and summary.site.shift:
            bucket, reason, next_step = 'check', '관측한 웹사이트 주소가 달라졌습니다.', '회사에 맞는 페이지인지 먼저 확인하세요. 솔루션 교체로 해석하지 마세요.'
        elif latest_status not in ev.TRUSTWORTHY_STATUSES:
            bucket, reason, next_step = 'check', '최근 수집을 정상 수집과 비교할 수 없습니다.', '아래 정보는 마지막 정상 수집 기준입니다. 최신 상태를 먼저 확인하세요.'
        elif bucket == 'baseline':
            bucket, reason, next_step = 'baseline', '첫 정상 수집을 확보했습니다. 아직 변화는 판단할 수 없습니다.', '다음 정상 수집과 비교합니다. 첫 검출은 신규 도입을 뜻하지 않습니다.'
        elif bucket in ('contact', 'watch'):
            repeated = bucket == 'contact'
            reason = events[0]['reason'] if repeated else '달라진 관측이 있지만 추가 확인이 필요합니다.'
            next_step = ('변화가 실제 운영 변경인지 질문하고, 답변에 맞춰 대화를 이어가세요.' if repeated
                         else '다음 정상 수집에서도 같은 변화가 이어지는지 확인하세요.')
        if not target['enabled']:
            bucket, reason, next_step = 'check', '모니터링이 중지된 기업입니다.', '이전 관측은 참고용입니다. 현재 연락 근거로 쓰지 마세요.'
        pairs, _ = sig._pair_migrations(
            [c for c in changes if c.state in (sig.STATE_REMOVED, sig.STATE_REMOVAL_CANDIDATE)],
            [c for c in changes if c.state in (sig.STATE_NEW, sig.STATE_REAPPEARED)])
        paired = [f"{gone.name}의 미검출과 {arrived.name}의 검출이 가까운 시기에 관측됐습니다. 제품 교체 여부는 확인이 필요합니다."
                  for gone, arrived in pairs]
        cards.append(dict(id=target['id'], company=target['company'], industry=target['industry'],
                          solution_type=solution_type, solution_type_label=solution_type_label,
                          observation=observation, observation_label=observation_label,
                          filter_query=observation_filter_query(solution_type, observation),
                          scope_label=' · '.join(filter(None, (solution_type_label, observation_label))),
                          industry_label=industries.get(target['industry'], target['industry']),
                          enabled=bool(target['enabled']), urls=json.loads(target['urls_json']), tier=target['tier'],
                          bucket=bucket, label=BUCKETS[bucket], reason=reason, next_step=next_step,
                          decision=detail_decision(bucket, events, good_count, reason),
                          events=events, tools=tools, paired=paired, good_count=good_count,
                          baseline=local_date(observations.get('first_at')),
                          last_good=local_date(observations.get('last_at')),
                          last_scan=local_date(summary.last_scan_at) if summary else '아직 없음',
                          last_status=STATUS.get(latest_status, latest_status or '아직 수집되지 않음'),
                          tool_count=sum(c.state not in (sig.STATE_REMOVED,sig.STATE_REMOVAL_CANDIDATE) for c in changes)))
    attach_waiting_guidance(store, cards)
    if target_id:
        attach_evidence(store, cards)
    order = list(BUCKETS)
    return sorted(cards, key=lambda c: (order.index(c['bucket']), c['company']))


def attach_waiting_guidance(store, cards, now=None):
    """Project the next attempt without writing schedules or queueing scans.

    Constant query count for the board; failed attempts also earn their full
    quiet interval. These dates are eligibility dates, not completion promises.
    """
    waiting = [c for c in cards if c['bucket'] == 'watch']
    if not waiting:
        return
    settings = config.load()
    cadence = settings.cadence
    zone = ZoneInfo(settings.scan.timezone)
    now = (now or datetime.now(UTC)).astimezone(zone)
    today = now.date()
    ids = tuple(dict.fromkeys(c['id'] for c in waiting))
    placeholders = ','.join('?' for _ in ids)
    schedules = {(r['target_id'], r['url']): r for r in store.conn.execute(
        f'SELECT * FROM scan_schedule WHERE target_id IN ({placeholders})', ids)}
    attempts = {(r['target_id'], r['url']): r['latest'] for r in store.conn.execute(
        f'SELECT target_id,url,MAX(started_at) AS latest FROM scan WHERE target_id IN ({placeholders}) GROUP BY target_id,url', ids)}
    active = {(r['target_id'], r['url']): r['state'] for r in store.conn.execute(
        f"SELECT target_id,url,state FROM job WHERE state IN ('pending','claimed') AND target_id IN ({placeholders})", ids)}
    for card in waiting:
        dates, states = [], []
        interval = cadence.tiers.get(card['tier'] or '', cadence.default)
        for url in card['urls']:
            key = (card['id'], url)
            latest = attempts.get(key)
            if key in active:
                states.append(active[key])
            if cadence.spread_weekly and interval.strip().lower() == '7d':
                due = weekly_due_date(card['id'], url, latest, schedules.get(key), today, zone)
            elif latest:
                previous = datetime.fromisoformat(latest).astimezone(zone)
                seconds = cadence.seconds_for(card['tier'])
                due = (previous.date() + timedelta(days=ceil(seconds / 86400))
                       if interval.strip().lower().endswith('d')
                       else (previous + timedelta(seconds=seconds)).date())
            else:
                due = today
            if cadence.not_before:
                due = max(due, date.fromisoformat(cadence.not_before))
            dates.append(due)
        due = min(dates) if dates else None
        days = (due-today).days if due else None
        if states:
            date_label = '수집 중' if 'claimed' in states else '수집 순서 대기 중'
            relative = '결과가 나오면 다시 판단합니다'
        elif due is None:
            date_label, relative = '일정 확인 필요', ''
        else:
            date_label = due.strftime('%Y.%m.%d')
            relative = ('예정일 지남 · 수집 대기' if days < 0 else '오늘 예정' if days == 0 else f'{days}일 후')
        results = []
        arrivals = [e for e in card['events'] if e['state'] in (sig.STATE_NEW, sig.STATE_REAPPEARED)]
        absences = [e for e in card['events'] if e['state'] in (sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED)]
        if arrivals:
            results.append(f'최근 보인 솔루션 {len(arrivals)}개가 다음 정상 수집에서도 검출되는지 확인합니다.')
        if absences:
            results.append(f'최근 보이지 않은 솔루션 {len(absences)}개가 다음 정상 수집에서도 미검출인지 확인합니다.')
        for event in card['events']:
            event['waiting_result'] = ('다음 정상 수집에서도 이 솔루션이 검출되는지 확인합니다.'
                                       if event in arrivals else '다음 정상 수집에서도 이 솔루션이 미검출인지 확인합니다.')
        card['waiting'] = dict(date=due.isoformat() if due else None, date_label=date_label,
                               relative=relative, timezone=settings.scan.timezone, results=results,
                               multiple_urls=len(dates) > 1,
                               note='수집이 실패하거나 결과가 달라지면 확인이 더 필요합니다. 예정일은 연락 가능일이 아닙니다.')


def board_candidates(store, solution_type, observation):
    """Read small relational facts; do not decode/render all company payloads."""
    if store.summary_state() != sig._summary_state(store):
        sig.refresh_summaries(store)
    params = []
    category = 'CASE fingerprint_id '
    for product, kind in PRODUCT_TYPES.items():
        category += 'WHEN ? THEN ? '
        params.extend((product, kind))
    category += 'ELSE CASE WHEN category IN (' + ','.join('?' for _ in USES) + ") THEN category ELSE 'other' END END"
    params.extend(USES)
    filters = []
    if solution_type:
        filters.append('solution_type IN (' + ','.join('?' for _ in solution_type) + ')')
        params.extend(solution_type)
    if observation:
        states = [state for state, pattern in PATTERN_BY_STATE.items() if pattern in observation]
        filters.append('(state IN (' + ','.join('?' for _ in states) + ')' +
                       (' OR fluctuating=1' if 'fluctuating' in observation else '') + ')')
        params.extend(states)
    where = ' WHERE ' + ' AND '.join(filters) if filters else ''
    # Readiness uses the same state/streak thresholds as the evidence card.
    params.extend((sig.STATE_BASELINE, sig.STATE_STABLE, sig.STATE_REMOVED,
                   sig.STATE_NEW, sig.STATE_REAPPEARED, ARRIVAL_REVIEW_RUNS))
    sql = f"""WITH typed AS (
        SELECT *, {category} AS solution_type FROM company_solution_summary
    ), matched AS (SELECT * FROM typed{where}), counts AS (
        SELECT target_id, COUNT(*) AS tools,
          SUM(state NOT IN (?,?)) AS events,
          SUM(state=? OR (state IN (?,?) AND run_streak>=?)) AS ready
        FROM matched GROUP BY target_id
    ) SELECT t.id,t.company,t.industry, s.last_status,
        COALESCE(f.observed_runs,0) AS observed, COALESCE(f.good_runs,0) AS good,
        COALESCE(f.site_shift,0) AS site_shift,
        COALESCE(c.tools,0) AS tools, COALESCE(c.events,0) AS events, COALESCE(c.ready,0) AS ready
        FROM target t LEFT JOIN company_summary s ON s.target_id=t.id
        LEFT JOIN company_filter_summary f ON f.target_id=t.id
        LEFT JOIN counts c ON c.target_id=t.id WHERE t.enabled=1"""
    cards = []
    for row in store.conn.execute(sql, params):
        card = dict(row)
        card['bucket'] = observation_bucket(card['observed'], card['good'], card['site_shift'],
                                            card['last_status'], card['events'], card['ready'])
        cards.append(card)
    order = list(BUCKETS)
    return sorted(cards, key=lambda c: (order.index(c['bucket']), c['company']))


def observation_bucket(observed, good, site_shift, latest_status, events, ready):
    if not observed:
        return 'waiting'
    if not good or site_shift or latest_status not in ev.TRUSTWORTHY_STATUSES:
        return 'check'
    if good == 1:
        return 'baseline'
    if events:
        return 'contact' if ready else 'watch'
    return 'steady'


def board(store, industries, *, status='changes', q='', industry='', page=1, solution_type='', observation=''):
    industry = normalize_industries(industry, industries)
    solution_type = normalize_solution_types(solution_type)
    observation = normalize_observations(observation)
    cards = board_candidates(store, solution_type, observation)
    scoped = [c for c in cards if (not q or q.casefold() in c['company'].casefold())
              and (not industry or c['industry'] in industry)
              and (bool(c['tools']) if observation else (not solution_type or c['events']))]
    counts = {key: sum(c['bucket'] == key for c in scoped) for key in BUCKETS}
    view_counts = {key: sum(counts[b] for b in view['buckets'])
                   for key, view in VIEWS.items()}
    # Old links to technical states land in the matching sales view. The home
    # selects a single view; it never silently combines two status buckets.
    if status in ('baseline', 'waiting', 'check'):
        status = 'pending'
    if status not in (*VIEWS, 'all'):
        status = 'all' if observation else ('contact' if view_counts['contact'] else 'watch')
    selected = [c for c in scoped if status == 'all' or c['bucket'] in VIEWS[status]['buckets']]
    page = max(1, min(page, max(1, (len(selected)+23)//24)))
    visible = company_cards(store, industries, solution_type=solution_type, observation=observation,
                            target_ids=[c['id'] for c in selected[(page-1)*24:page*24]])
    for card in visible:
        apply_industry_context(card, industry, industries)
    current_view = ALL_VIEW if status == 'all' else VIEWS[status]
    if status == 'all' and (q or industry or solution_type or observation):
        current_view = dict(ALL_VIEW, label='조건에 맞는 기업',
                            description='선택한 검색 조건에 맞는 기업입니다. 상태를 눌러 연락 전 확인할 곳과 재확인을 기다릴 곳을 좁혀 볼 수 있습니다.')
    # List cards show counts and observation summaries. Fetch direct matches
    # only when opening a company detail, where the evidence is displayed.
    return dict(cards=visible, total=len(cards), scoped_total=len(scoped), counts=counts,
                view_counts=view_counts, views=VIEWS, current_view=current_view,
                filtered=len(selected), page_number=page, pages=max(1,(len(selected)+23)//24),
                status=status, q=q, industry=industry, buckets=BUCKETS, multi_industries=True,
                industry_filter_label=' 또는 '.join(industries.get(key, INDUSTRIES.get(key, key)) for key in industry),
                solution_type=solution_type, solution_types=SOLUTION_TYPES, multi_solution_types=True,
                solution_type_label=' 또는 '.join(SOLUTION_TYPES[key] for key in solution_type),
                observation=observation, observation_filters=OBSERVATION_FILTERS, multi_observation=True,
                observation_label=' 또는 '.join(OBSERVATION_FILTERS[key] for key in observation),
                since=config.load().analysis.since)


EVIDENCE_LABELS = {
    'network': '브라우저 통신', 'runtime': '페이지 실행 흔적',
    'storage': '브라우저에 저장된 흔적', 'dom': '페이지 요소',
    'console': '브라우저 실행 로그',
}


def attach_evidence(store, cards, event_limit=None):
    """Fetch direct matches for visible events only, in bounded batches.

    The positive run comes from the scoped, host-reset timeline. Missing
    events deliberately show the last positive evidence, labelled by date.
    No raw evidence blobs or per-event queries are needed.
    """
    wanted = {(c['id'], e['evidence_run'], e['id']): e
              for c in cards for e in c['events'][:event_limit]}
    keys = list(wanted)
    cutoff = config.load().analysis.cutoff or None
    for start in range(0, len(keys), 200):
        batch = keys[start:start+200]
        placeholders = ','.join('(?,?,?)' for _ in batch)
        params = [v for key in batch for v in key]
        params.extend([cutoff, cutoff])
        rows = store.conn.execute(f"""
            WITH wanted(target_id, run_id, fingerprint_id) AS (VALUES {placeholders})
            SELECT s.target_id, s.run_id, s.started_at, s.page_host, s.id AS scan_id,
                   d.fingerprint_id, d.matches_json
            FROM wanted w JOIN scan s ON s.target_id=w.target_id AND s.run_id=w.run_id
            JOIN detection d ON d.scan_id=s.id AND d.fingerprint_id=w.fingerprint_id
            WHERE s.status='OK' AND d.verdict='DETECTED' AND d.indirect_only=0
              AND (? IS NULL OR julianday(s.started_at)>=julianday(?))
            ORDER BY s.started_at, s.id
        """, params)
        for row in rows:
            event = wanted[(row['target_id'], row['run_id'], row['fingerprint_id'])]
            for match in json.loads(row['matches_json'] or '[]'):
                layer = match.get('layer')
                if layer not in DIRECT_LAYERS:
                    continue
                event['evidence'].append(dict(
                    label=EVIDENCE_LABELS[layer], value=match.get('value', ''),
                    date=local_date(row['started_at']), host=row['page_host'],
                    scan_id=row['scan_id'],
                ))


DETAIL_ACTIONS = {
    'contact': '관측된 변화를 질문하며 연락을 검토할 수 있습니다.',
    'watch': '이 변화로 연락하기에는 아직 근거가 부족합니다.',
    'baseline': '비교할 기록이 더 쌓일 때까지 기다려 주세요.',
    'steady': '변화를 이유로 새로 연락할 근거는 없습니다.',
    'waiting': '첫 수집 결과를 기다려 주세요.',
    'check': '수집 상태를 확인하기 전에는 연락 근거로 쓰지 마세요.',
}


def detail_decision(bucket, events, good_count, reason):
    """Explain the existing verdict; never promote individual unready events.

    A collection problem or disabled target overrides even previously repeated
    evidence. Streaks count runs, not distinct days or network requests.
    """
    ready_count = sum(e['ready'] for e in events)
    allowed_count = ready_count if bucket == 'contact' else 0
    summaries = []
    for states, wording in (
        ((sig.STATE_NEW,), '이전에 보이지 않다가 최근 검출됨'),
        ((sig.STATE_REAPPEARED,), '미검출 후 다시 검출됨'),
        ((sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED), '이전에는 검출됐지만 최근 미검출'),
    ):
        group = [e for e in events if e['state'] in states]
        if group:
            names = ', '.join(e['name'] for e in group[:2])
            if len(group) > 2:
                names += f" 외 {len(group)-2}개 솔루션"
            summaries.append(f'{names}: {wording}')
    if not summaries:
        summaries = [reason]
    if events:
        evidence = f'정상 수집 {good_count}회를 비교했습니다. 반복 관측 기준을 충족한 솔루션 {ready_count}개, 재확인이 필요한 솔루션 {len(events)-ready_count}개입니다.'
        if bucket == 'check':
            evidence += ' 이전 기록의 집계이며, 현재 연락에 활용할 수 있는 근거는 아닙니다.'
    elif good_count == 1:
        evidence = '정상 수집 1회입니다. 첫 기록만으로 솔루션이 새로 도입됐거나 중단됐는지 판단할 수 없습니다.'
    elif not good_count:
        evidence = '비교에 사용할 정상 수집 기록이 없습니다.'
    else:
        evidence = f'정상 수집 {good_count}회에서 비교 가능한 솔루션 변화가 없습니다.'
    for event in events:
        event['contact_allowed'] = bucket == 'contact' and event['ready']
        if bucket == 'check':
            event['review_label'] = '연락 근거로 사용 보류'
            event['review_reason'] = reason + ' 이전에 반복 관측됐더라도 현재 연락 근거로 쓰지 마세요.'
        elif event['ready']:
            event['review_label'] = '확인 질문에 활용 가능'
            event['review_reason'] = '반복 관측 기준을 충족했습니다. 도입·중단을 단정하지 말고 실제 운영이 달라졌는지 물어보세요.'
        else:
            event['review_label'] = '재확인 대기'
            if event['state'] in (sig.STATE_NEW, sig.STATE_REAPPEARED):
                event['review_reason'] = f"최근 연속 검출은 {event['streak']}회입니다. 정상 수집에서 {ARRIVAL_REVIEW_RUNS}회 이상 연속 검출돼야 연락 전 확인 대상으로 봅니다. 다음 정상 수집을 기다려 주세요."
            else:
                event['review_reason'] = f"최근 연속 미검출은 {event['streak']}회입니다. 정상 수집에서 {sig.REMOVAL_CONFIRM_RUNS}회 이상 연속 미검출되고, 첫 미검출부터 마지막 확인까지 {sig.REMOVAL_CONFIRM_DAYS:g}일 이상이어야 반복 관측으로 봅니다. 아직 이 기준을 충족하지 못했습니다."
    return dict(action=DETAIL_ACTIONS[bucket], changes=summaries, evidence=evidence,
                ready_count=ready_count, allowed_count=allowed_count,
                view='pending' if bucket in ('baseline', 'waiting', 'check') else bucket,
                empty=reason)
