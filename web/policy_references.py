"""Read saved document references; detail-page views never trigger collection."""
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from radar import evidence
from radar import policy_references as policies

ZONE = ZoneInfo('Asia/Seoul')
ISSUES = {
    'external_domain_requires_review': '다른 도메인으로 연결되어 공식 문서인지 추가 확인이 필요합니다.',
    'robots_disallowed': '사이트의 자동 접근 제한으로 문서를 읽지 않았습니다.',
    'robots_unavailable': '자동 접근 허용 여부를 확인하지 못했습니다.',
    'robots_invalid': '자동 접근 규칙을 확인하지 못했습니다.',
    'policy_link_not_found': '공개된 약관 링크를 자동으로 찾지 못했습니다.',
    'not_a_policy_document': '약관 본문을 확인하지 못했습니다.',
    'unsupported_document_format': '현재 자동 수집을 지원하지 않는 문서 형식입니다.',
    'blocked': '사이트가 자동 접근을 제한했습니다.',
    'request_budget_exceeded': '부하 제한 안에서 문서를 끝까지 확인하지 못했습니다.',
    'document_too_large': '문서 크기 제한으로 자동 확인을 중단했습니다.',
}


def timestamp(value):
    try:
        return datetime.fromisoformat(value).astimezone(ZONE).strftime('%Y.%m.%d %H:%M')
    except (TypeError, ValueError):
        return '확인일 없음'


def view(store, target, now=None):
    now = now or datetime.now(UTC)
    result = {'state': 'pending', 'headline': '아직 참고 문서를 확인하지 않았습니다.',
              'headline_en': 'Policy references have not been checked yet.',
              'documents': [], 'organizations': [], 'checked_at': None, 'attempted_at': None,
              'next_due': None, 'issues': [], 'previous': False, 'enabled': bool(target['enabled'])}
    try:
        saved = policies.read(store, target['id'])
        if not isinstance(saved, dict):
            return result
        roots = [evidence.split_mobile(u)[1] for u in json.loads(target['urls_json'])]
        if saved.get('source', {}).get('roots') != roots or saved.get('parser_version') != policies.PARSER_VERSION:
            result['headline'] = '문서 참고 근거를 다시 확인할 예정입니다.'
            result['headline_en'] = 'Policy references are awaiting a fresh check.'
            return result
        attempted = datetime.fromisoformat(saved['checked_at'])
        result['attempted_at'] = timestamp(saved['checked_at'])
        result['next_due'] = timestamp((attempted+timedelta(days=7)).isoformat())
        result['overdue'] = now >= attempted+timedelta(days=7)
        result['state'] = saved['status']
        result['issues'] = list(dict.fromkeys(ISSUES.get(i, '접근 또는 응답 문제로 문서 확인을 완료하지 못했습니다.')
                                            for i in saved.get('issues', [])))
        documents = saved.get('documents', [])
        checked = saved['checked_at']
        if not documents and saved.get('previous_readable'):
            documents = saved['previous_readable']['documents']
            checked = saved['previous_readable']['checked_at']
            result['previous'] = True
        for document in documents:
            try:
                url = policies.clean_url(document['url'])
            except (ValueError, KeyError):
                continue
            result['documents'].append({**document, 'url': url, 'host': urlsplit(url).hostname})
        if result['documents']:
            result['checked_at'] = timestamp(checked)
        names = sorted({m['organization'] for d in result['documents'] for m in d.get('mentions', [])})
        result['organizations'] = names
        result['headline_en'] = ('Older references shown; the latest check failed.' if result['previous'] else
                                 'Some documents could not be checked.' if saved['status'] != 'READ' else
                                 f'{len(names)} organization names mentioned in documents.' if names else
                                 'Documents read; no names from the reference dictionary extracted.')
        if saved['status'] == 'READ':
            result['headline'] = (f'문서에 업체명 {len(names)}개가 언급되어 있습니다.' if names else
                                  '문서는 확인했지만 등록된 업체명은 추출되지 않았습니다.')
        elif result['previous']:
            result['headline'] = '최근 문서 확인에 실패했습니다. 아래는 이전 확인 자료입니다.'
        elif saved['status'] == 'PARTIAL':
            result['headline'] = '일부 문서만 확인했습니다.'
        else:
            result['headline'] = '참고 문서를 확인하지 못했습니다.'
        return result
    except (ValueError, TypeError, KeyError):
        # A malformed optional reference must not break the observed stack page.
        result.update(state='UNAVAILABLE', headline='참고 자료를 다시 확인해야 합니다.', documents=[])
        return result
