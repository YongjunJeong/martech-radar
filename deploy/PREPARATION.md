# 사설 CLI gateway 연결

Codex 또는 Claude CLI와 KB를 별도 실행 호스트에 두는 구성이다. Codex는 [ChatGPT 로그인 설정](../docs/CODEX.md)을 따른다. 명시적으로 API 과금 방식을 선택하려면 [API + 내장 KB 구성](PREPARATION_HOSTED.md)을 참고한다.

웹 컨테이너는 인증된 사설 gateway로 KB 검색·기업 조사·문서 생성을 요청한다.
Gateway는 기존 Marketing KB HTTP 서비스와 선택한 CLI가 인증된 환경에서
실행한다. 웹 이미지에 CLI 인증 파일이나 개인 홈 디렉터리를 넣지 않는다.

현재 Ubuntu 운영 설정은 그대로다. 아래 구성은 별도 staging에서 먼저 검증한다.
이 방식으로 웹을 옮겨도 gateway/KB 호스트는 필요하다. 모든 서버를 이전하려면
KB 인덱스·CLI 실행 환경·인증을 함께 이관해야 한다.

## Gateway 준비

같은 패키지 버전을 gateway에도 설치한다. gateway 설정의 `[preparation]`에
`mkb_url`, `timeout_seconds`와 백엔드를 지정한다. Codex는 `backend="codex"`, `codex_binary`, `codex_model`; 기존 Claude는 `backend="cli"`, `claude_binary`, `model`을 사용한다.
`gateway_url`은 비워 둔다. gateway는 DB를 열지 않는다.

1. 32자 이상의 무작위 ASCII 공유 토큰을 별도 파일에 만들고 권한을 0600으로 둔다.
   웹 컨테이너 UID 1000도 이 파일을 읽을 수 있어야 한다.
2. `RADAR_HOME`을 gateway 설정이 있는 디렉터리로 지정한다.
3. `RADAR_GATEWAY_TOKEN_FILE`에 토큰 파일의 절대 경로를 지정한다.
4. `python -m web.preparation_gateway --host 127.0.0.1 --port 8612`로 시작한다.

기본 주소는 loopback이다. 컨테이너·다른 서버에서 연결할 때는 사설 네트워크나
인증서를 검증하는 HTTPS reverse proxy를 사용한다. 외부에 평문 HTTP로 공개하지 않는다.
컨테이너의 `127.0.0.1`은 별도 네트워크에서는 호스트를 가리키지 않는다.

Gateway의 systemd/Supervisor 등록과 재시작 정책은 이전할 호스트에서 설정한다.
현재 운영 서버의 자동 시작·외부 공개는 이 작업에 포함하지 않았다.

## 새 웹 workspace

```sh
export RADAR_STAGE_HOME="$HOME/martech-radar-staging-with-preparation"
export RADAR_GATEWAY_TOKEN_FILE="/absolute/private/gateway-token"
.venv/bin/python deploy/init_workspace.py "$RADAR_STAGE_HOME" \
  --gateway-url https://your-private-gateway

docker compose -f deploy/compose.yaml -f deploy/compose.preparation.yaml config --quiet
docker compose -f deploy/compose.yaml -f deploy/compose.preparation.yaml build web
docker compose -f deploy/compose.yaml -f deploy/compose.preparation.yaml run --rm \
  --entrypoint python web /opt/radar/check_dependencies.py
docker compose -f deploy/compose.yaml -f deploy/compose.preparation.yaml up -d web
```

이미 있는 staging의 `radar.toml`은 아래 항목을 직접 지정한다. 운영 설정을 바꾸는
명령이 아니다. 준비 기능을 사용하지 않으면 기존처럼 `enabled=false`를 유지한다.

```toml
[preparation]
enabled = true
gateway_url = "https://your-private-gateway"
gateway_token_file = "/run/secrets/preparation_gateway_token"
```

secret은 웹 서비스에만 마운트된다. 수집 워커·예약 배치·운영 점검은 KB나 모델에
의존하지 않는다. 웹 healthcheck도 KB 장애로 전체 대시보드를 불건강 판정하지 않는다.
`check_dependencies.py`는 별도로 실제 KB 검색과 CLI 실행 파일 존재를 확인한다.
모델 인증 여부는 유료 호출 없이 확정할 수 없어 `model_auth=not_checked`로 표시한다.
실제 생성 성공을 별도로 검증해야 한다.

## 동작과 실패 처리

- `/v1/search`: 한국 시장, 근거 포함, 자동 번역 비활성. 기존 KB 선정·제외 규칙 유지.
- `/v1/research`: 공개 기업명·업종·URL만 전달. 기존 검색·본문·인용 검증 유지.
- `/v1/model`: 추천 또는 제안서·메일 묶음만 허용. 자유 명령·쉘·메일 발송 기능 없음.
- 모든 경로에 Bearer 인증. 요청·응답 크기 제한, 리디렉션 금지, 자동 재시도 없음.
- 모델·조사 실행은 gateway 파일 잠금으로 한 번에 하나만 허용한다. 여러 gateway
  프로세스를 실행한다면 같은 잠금 파일을 사용해야 한다. 현재는 단일 호스트용이다.
- 웹은 기존 DB 초안·revision·작업 상태와 출처·벤더 표현 검증을 유지한다.
  제안서와 메일은 둘 다 유효할 때 함께 저장한다.
- 생성 중 웹이 종료되면 다음 시작에서 해당 작업을 실패로 표시한다. 기존 초안은
  보존하며 유료 호출을 자동 재실행하지 않는다. gateway의 진행 호출은 제한 시간까지
  실행될 수 있다. 장기적으로 별도 영속 생성 큐가 필요하면 이 API 경계로 확장한다.

인증 토큰·기업 메모·생성 프롬프트를 access log에 남기지 않는다. gateway 버전과
웹 버전을 함께 배포해 스키마·검증 기준을 맞춘다.

생성 호스트의 상시 실행 예시는 `preparation-gateway.service.example`에 있다. 새 호스트의
계정·설치 경로·검증된 CLI 인증 환경에 맞춘 뒤 적용한다. 예시 파일은 현재 운영에
설치하지 않았으며, 공개 ingress는 별도 HTTPS proxy에서 끝내고 gateway는 loopback에
유지한다. 이번 단계에서는 CLI 인증이나 KB 인덱스를 다른 호스트로 옮기지 않았다.
