"""Explicit seller context; names are choices, not product-support promises."""
import re

VENDORS = {
    'neutral': '벤더 중립', 'insider': 'Insider', 'braze': 'Braze',
    'salesforce': 'Salesforce', 'adobe': 'Adobe', 'bloomreach': 'Bloomreach',
    'moengage': 'MoEngage', 'clevertap': 'CleverTap', 'custom': '직접 입력',
}
ALIASES = {
    'insider': {'insider', 'insider one', '인사이더', 'architect', 'smart recommender', 'eureka'},
    'braze': {'braze', '브레이즈', 'canvas'},
    'salesforce': {'salesforce', '세일즈포스'}, 'adobe': {'adobe', 'marketo'},
    'bloomreach': {'bloomreach', '블룸리치'}, 'moengage': {'moengage'}, 'clevertap': {'clevertap'},
}


def neutral_sender():
    return {'vendor': 'neutral', 'name': '', 'context': ''}


def parse_sender(vendor, name='', context=''):
    from .preparation import PrepError
    if vendor not in VENDORS:
        raise PrepError('제안할 벤더를 목록에서 선택해 주세요.')
    if vendor == 'neutral':
        return neutral_sender()
    name, context = name.strip(), context.strip()
    if vendor != 'custom':
        name = VENDORS[vendor]
    if not 1 <= len(name) <= 80 or '\n' in name or '\r' in name or len(context) > 4000:
        raise PrepError('솔루션 이름은 한 줄 80자, 소개 내용은 4,000자 이내로 입력해 주세요.')
    return {'vendor': vendor, 'name': name, 'context': context}


def permits_name(match, sender):
    if not sender or sender['vendor'] == 'neutral':
        return False
    name = ' '.join(match.casefold().split())
    if sender['vendor'] == 'custom':
        return bool(re.search(r'(?<![a-z0-9_])' + re.escape(name) + r'(?![a-z0-9_])',
                              ' '.join(sender['name'].casefold().split())))
    allowed = ALIASES.get(sender['vendor'], set()) | {sender['name'].casefold()}
    return name in allowed
