# Marketing KB 연결

제안 준비는 벤더 중립 마케팅 패턴을 검색하는 별도 KB 서비스가 필요합니다. 이 저장소는 검색 클라이언트와 gateway, 기존 KB를 배포하기 위한 도구를 제공합니다. KB 원문·벡터 인덱스·검색 엔진 전체를 포함하지 않습니다. 저장소를 복제하고 Codex에 로그인하는 것만으로 RAG가 준비되지는 않습니다.

기존 KB 서비스를 아래 계약에 맞추거나, 공개 이용이 가능한 자료로 KB를 구축해 연결합니다. 내부 고객 자료·벤더 비공개 자료·개인용 스킬 폴더를 그대로 공개 배포하지 않습니다. 코드의 MIT 라이선스는 외부 KB 자료에 적용되지 않습니다.

## 검색 API

`[preparation].mkb_url`의 `POST /search`를 사용합니다. 직접 연결은 사설 네트워크 또는 localhost용입니다. 다른 서버에서는 인증된 [사설 gateway](../deploy/PREPARATION.md)를 사용하세요.

요청 예:

```json
{
  "query": "retail retention repeat purchase loyalty journey orchestration",
  "industry": "retail",
  "channels": [],
  "market": "kr",
  "top_k": 10,
  "include_evidence": true,
  "translate": false
}
```

응답 구조 예시입니다. 아래 URL·패턴은 계약 설명용 가상 값이며 실제 추천 근거로 사용하지 않습니다.

```json
{
  "hits": [{
    "id": "play-001",
    "title": "Post-purchase onboarding",
    "title_ko": "구매 후 첫 경험 안내",
    "summary": "고객이 구매한 상품을 잘 활용하도록 돕는 시나리오",
    "mechanism": "구매 이벤트와 수신 동의를 확인해 안내하고 대조군과 재방문율을 비교한다.",
    "similarity": 0.85,
    "tier": "preferred",
    "axes": {
      "channels": [{"id": "ch:email"}],
      "requires_capabilities": []
    },
    "evidence": [{
      "title": "공개 사례 제목",
      "url": "https://example.com/public-case",
      "vendor": "",
      "source_id": "source-001",
      "published_at": ""
    }]
  }]
}
```

앱은 유사도 0.70 이상, 공개 근거 URL이 있는 결과를 사용하고 `tier="excluded"`를 제외합니다. 명시적으로 선택한 SMS에 대한 기존 호환 예외는 `web/preparation.py`에 있습니다. 채널·기능 taxonomy는 `web/preparation_catalog.py`와 맞춰야 합니다. 자체 검색기의 점수 척도가 다르면 임계값의 의미를 검증한 뒤 연결하세요.

자동 추천은 최대 3개 검색 질의의 결과를 섞어 최대 10개 패턴을 사용합니다. 출처 고객의 성과 수치는 생성 문맥에 포함하지 않습니다. 원문 링크와 벤더 귀속은 근거에서 확인할 수 있습니다.

## 별도 LLM API 없이 운영하려면

`translate=false`를 존중하는 KB를 사용하고 검색 단계의 번역·요약에 외부 LLM을 숨겨 호출하지 않아야 합니다. 로컬 임베딩과 미리 만든 인덱스를 이용하는 검색 서비스로 구성할 수 있습니다. 앱은 해당 옵션을 전달하지만 외부 KB 구현 내부까지 보장하지 않습니다.

기존 `mkb` 형식의 코드·taxonomy·인덱스·로컬 임베딩 모델을 이미 보유했다면 `deploy/export_preparation_kb.py`로 검증된 묶음을 만들 수 있습니다. 이 도구는 임의의 KB 저장소를 자동 변환하지 않습니다. 별도 프로젝트를 참고할 때도 응답 계약과 자료 이용 조건을 확인하세요.
