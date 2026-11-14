"""Vendor-independent choices mapped to the Marketing KB's controlled vocabulary.

These are requirements to explore, not a claim that every platform supports them.
Channels say WHERE; capabilities say WHAT. No product aliases in prompts or UI.
"""
GOALS = {
    "conversion": ("구매·신청 전환 높이기", "conversion abandonment recovery"),
    "retention": ("재방문·재구매 늘리기", "retention repeat purchase reactivation"),
    "onboarding": ("가입 후 첫 이용 늘리기", "onboarding activation first conversion"),
    "crosssell": ("관련 상품·서비스 제안하기", "cross sell personalized recommendations"),
    "acquisition": ("잠재고객·회원 확보하기", "lead generation acquisition opt in"),
    "loyalty": ("고객 관계·충성도 높이기", "customer loyalty engagement retention"),
}
CHANNELS = {
    "onsite": "웹사이트", "in_app": "앱 내 메시지", "email": "이메일",
    "web_push": "웹 푸시", "app_push": "앱 푸시", "sms": "문자 · SMS/LMS",
    "kakao": "카카오 메시지", "inbox": "앱 인박스·카드",
    "chat": "채팅·상담", "paid_ads": "광고",
}
CHANNEL_IDS = dict(zip(CHANNELS, (
    "ch:web_onsite", "ch:in_app", "ch:email", "ch:web_push", "ch:mobile_push",
    "ch:sms", "ch:kakao_alimtalk", "ch:in_app_inbox", "ch:chat_conversational", "ch:paid_ads",
), strict=True))


def capability(label, hint, query, *ids):
    return dict(label=label, hint=hint, query=query, kb_ids=ids)


CAPABILITIES = {
    "journey_orchestration": capability("여정 자동화 · Journey Orchestration", "행동에 따라 분기하고 여러 채널을 연결",
        "journey orchestration behavioral trigger branching cross channel", "cap:journey_orchestration"),
    "recommendation": capability("상품·콘텐츠 추천 · Recommendation", "관심과 행동에 맞는 상품·콘텐츠를 제안",
        "personalized product content recommendations", "cap:product_recommendation"),
    "personalization": capability("웹·앱 개인화", "고객별로 화면과 메시지를 다르게 제공",
        "website in app personalization dynamic content", "cap:onsite_personalization", "cap:in_app_messaging"),
    "segmentation": capability("고객 세분화", "행동·속성·구매 이력으로 대상 그룹 구성",
        "audience segmentation behavioral RFM", "cap:audience_segmentation"),
    "customer_data": capability("고객 데이터·프로필 통합", "채널별 고객 정보와 식별자를 연결",
        "unified customer profile identity resolution", "cap:identity_resolution"),
    "behavioral_data": capability("행동 데이터 수집·활용", "방문·클릭·구매 등의 이벤트를 활용",
        "behavioral event tracking conversion events", "cap:behavioral_event_tracking"),
    "real_time_triggering": capability("실시간 행동 대응", "고객 행동이나 상태 변화 직후 실행",
        "real time event trigger messaging", "cap:real_time_triggering"),
    "experimentation": capability("A/B 테스트·효과 검증", "대안과 대조군을 비교해 증분 효과 확인",
        "experimentation AB testing control group incrementality", "cap:experimentation", "cap:control_group_measurement"),
    "predictive_scoring": capability("이탈·구매 가능성 예측", "예측 점수로 대응할 고객과 우선순위 선정",
        "predictive scoring churn purchase propensity", "cap:predictive_scoring"),
    "send_optimization": capability("발송 시점·빈도 최적화", "반응할 시점과 과도한 메시지 노출을 관리",
        "send time optimization frequency capping fatigue", "cap:send_time_optimization", "cap:frequency_capping"),
    "search": capability("검색 개인화", "고객 맥락에 맞는 검색 결과를 제공",
        "site search personalization relevance", "cap:site_search_personalization"),
    "conversational": capability("대화형 상담·자동 응대", "질문과 의도를 파악해 상담 흐름 연결",
        "conversational messaging chatbot assistance", "cap:conversational_messaging"),
    "catalog": capability("상품·재고·가격 데이터 활용", "상품 정보와 변화를 캠페인에 연결",
        "product catalog inventory price feed", "cap:catalog_inventory_feed"),
    "integration": capability("외부 데이터·API 연동", "다른 시스템의 정보와 실행을 연결",
        "external context data API integration webhook", "cap:external_context_data"),
    "ai_content": capability("콘텐츠 생성·변형", "메시지와 소재의 초안·변형을 제작",
        "AI content generation creative copy variants", "cap:ai_content_generation"),
}
MAX_CAPABILITIES = 4
