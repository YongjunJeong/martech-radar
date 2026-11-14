# 호스팅용 KB·제안 생성

현재 운영은 변경하지 않는다. 이 문서는 **새 서버를 준비하는 절차**이며 실행만으로
운영 데이터를 이전하거나 수집을 활성화하지 않는다.

## 구성

`web → 인증된 preparation 서비스 → 내장 Marketing KB / Anthropic Messages API`

- KB와 생성 gateway를 하나의 서비스로 묶는다. 별도 Chroma 서버·CLI·개인 PC·로그인
  디렉터리·Insider KB가 필요하지 않다. 기존 Marketing KB 검색 코드와 임베딩을 사용한다.
- `compose.preparation-hosted.yaml`은 `compose.hosting.yaml`에 추가하는 선택 overlay다.
  호스트 포트를 열지 않는다. `http://preparation:8612`는 해당 Compose 내부 주소만 허용한다.
  다른 서버의 gateway는 기존처럼 HTTPS를 사용한다.
- API 키는 preparation에만, gateway 공유 토큰은 preparation과 web에만 마운트한다.
  수집 워커와 예약 배치는 KB·LLM 장애와 분리된다. 이 서비스에는 Radar DB를 마운트하지 않는다.
- 서비스 한 개, Uvicorn worker 한 개로 실행한다. 생성 동시 실행 1건, 검색도 1건씩 처리한다.
  메모리 4 GiB / CPU 2개 한도다. 이 한도는 수집기 예약분에 **추가**되는 자원이다.
- 개인 CLI 대신 API 과금이 발생한다. API 모델 ID를 명시하고 해당 계정의 모델 접근,
  structured output·web search·web fetch 지원 및 지출 한도를 실제 전환 전에 확인한다.
  사용자 지정 솔루션/벤더 선택 및 기존 벤더 중립·출처 검증은 동일하다.

## KB 번들 만들기

기존 KB의 검색 인덱스, Python 검색 코드, 분류표, 임베딩 모델 캐시만 복사한다.
원본 수집물·개인 자료·로그·CLI 인증·환경 파일은 포함하지 않는다.

```sh
python deploy/export_preparation_kb.py \
  --kb-root /absolute/marketing_usecases \
  --model-cache /absolute/hf-cache/models--intfloat--multilingual-e5-base \
  --destination /absolute/private/kb-release-YYYYMMDD
```

항상 새 디렉터리를 지정한다. `BUNDLE.json`에 파일별 SHA-256, KB 빌드 정보와
모델 revision을 기록한다. 실패한 불완전 번들은 사용하지 않는다.
SQLite는 backup API로 WAL을 포함해 복사하고 무결성을 검사한다. 활성 인덱스의
물리 디렉터리를 고정해 복사하고 인덱스 바이너리 변경을 확인한다. 원본 인덱스를
수정하면서 export하지 않는다. 기존 KB의 새 build + current 교체 방식은 가능하다.

번들을 배포할 서버의 UID 1000이 읽을 수 있어야 한다. 공개 저장소나 공개 이미지에
넣지 말고 사설 아티팩트로 취급한다. 번들 안의 Python 코드를 실행하므로 신뢰한
export 결과만 사용한다. checksum은 손상 검사이며 서명 검증을 대신하지 않는다.

서비스 시작 시 전체 checksum을 검사하고 인덱스 34 MiB 정도만 `/tmp`로 복사한다.
Chroma의 읽기 중 DB 쓰기는 이 복사본에 한정된다. 모델은 읽기 전용 번들에서 로드하며
Hugging Face와 Transformers의 offline 모드를 강제한다. 모델 자동 다운로드나
KB 질의 번역을 위한 숨은 CLI 호출은 없다.

## 이미지와 설정 준비

```sh
docker build --target web -t radar-web:VERSION .
docker build -f deploy/Dockerfile.preparation \
  --build-arg RADAR_WEB_IMAGE=radar-web:VERSION \
  -t radar-preparation:VERSION .
```

배포할 때 두 이미지를 검증한 digest로 고정한다. Python/CPU 검색 의존성 버전은
`requirements.preparation.lock`으로 고정한다. NVIDIA/CUDA 런타임을 설치하지 않는다.

기존 hosting 환경 변수에 다음을 추가한다. 실제 API 키 값은 환경 파일 대신
소유자만 읽는 secret 파일에 저장하고, 아래에는 그 경로만 지정한다.

```dotenv
RADAR_PREPARATION_IMAGE=private-registry/radar-preparation@sha256:VALIDATED_DIGEST
RADAR_API_MODEL=YOUR_SUPPORTED_ANTHROPIC_MODEL_ID
RADAR_API_KEY_FILE=/srv/radar/secrets/anthropic-api-key
RADAR_GATEWAY_TOKEN_FILE=/srv/radar/secrets/preparation-token
RADAR_KB_BUNDLE=/srv/radar/kb-releases/YYYYMMDD
RADAR_GENERATION_TIMEOUT=240
```

새 hosting workspace의 `radar.toml`:

```toml
[preparation]
enabled = true
gateway_url = "http://preparation:8612"
gateway_token_file = "/run/secrets/preparation_gateway_token"
```

모델 ID와 API 키는 web 설정에 넣지 않는다. 개인 PC/CLI 경로를 사용하지 않는
`web.preparation_hosted` 진입점이 서버 환경 변수로 API backend를 구성한다.
기존 설치의 기본 backend는 `cli`로 유지해 현재 운영이 바뀌지 않는다.

설정 검증만 하는 명령:

```sh
docker compose --env-file /private/hosting.env \
  -f deploy/compose.hosting.yaml -f deploy/compose.preparation-hosted.yaml config --quiet
```

이후 **실제 전환 승인 후** preparation과 web을 시작한다. 수집 활성화와 timer 설치는
`HOSTING.md`의 별도 전환 절차를 따른다. 기존 운영과 동시에 수집하지 않는다.

## 검증·장애 처리

- healthcheck는 실제 KB 검색 + API 키 파일 형식만 확인한다. **유료 호출이나 API
  인증 성공을 확인하지 않는다.** `model_auth=not_checked`는 의도한 결과다.
- 실제 전환 전, 승인한 예시 기업으로 조사 → 추천 → 선택 → 제안서·메일 생성 1회를
  수행해 API 권한·모델 호환성·문서 품질을 확인한다. 발송 기능은 없다.
- 조사에는 기업명·업종·공개 URL만 전송한다. 내부 메모·담당자 정보는 검색에 넣지 않는다.
  생성 API에는 선택한 제안 문맥과 근거가 전달되므로 조직의 API 사용 정책을 적용한다.
- 웹 검색 최대 3회, 페이지 읽기 최대 4회다. 검색과 JSON 추출은 별도 요청으로 나눈다.
  추천 1회는 조사 1~2회 + 추천 1회, 선택 후 제안서·메일 묶음은 1회 API 요청이다.
- 실제 검색 결과 또는 입력한 공개 URL, 성공적으로 읽은 본문, 본문에 존재하는 인용을
  모두 검증한다. 웹 조사가 실패하거나 검증 근거가 없으면 업종 가설로 표시한다.
  PDF처럼 원문 문자열 검증이 불가능한 자료는 현재 웹 근거에서 제외한다.
- 키 오류·rate limit·응답 중단·시간 초과 시 자동 재시도하지 않는다. 중복 과금을 막는다.
  JSON 형식 검사 뒤 기존 출처 ID·벤더 표현·제안/메일 동시 저장 검증도 적용한다.
- 최대 응답 2 MiB, 입력 2 MiB, 출력 8,000 tokens/요청. 응답이 잘리면 저장하지 않는다.
- 외부 서비스의 오류 본문·인증 키·프롬프트를 응답이나 access log에 남기지 않는다.

## 백업·업데이트

Radar의 DB 백업에는 KB 번들이 포함되지 않는다. KB는 재생성 가능한 **버전별 release**로
별도 사설 저장소/오브젝트 스토리지에 보관하고 현재 + 직전 검증본 2개 이상을 유지한다.
번들 전체 복사 후 checksum 검증으로 복원할 수 있다. 임시 `/tmp` 인덱스는 백업하지 않는다.
API 키와 gateway 토큰은 별도 secret 관리/복구 정책을 적용한다.

서버에서도 같은 export 명령을 사용할 수 있다. 원본 KB 업데이트 파이프라인을 서버에
이식하는 작업은 포함하지 않았다. 새 공개 사례를 수집·정규화한 KB release를 준비한 뒤,
격리 검증 → 번들 경로 교체 → preparation만 승인된 재시작 순으로 갱신한다.
검색/제안의 일상 운영에는 원본 PC나 KB 작성 파이프라인이 필요 없다.

API 계약 참고: [Messages structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
[web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool),
[web fetch](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool).
검색 citations와 strict JSON 출력은 함께 사용할 수 없어 조사와 구조화 단계를 분리했다.
