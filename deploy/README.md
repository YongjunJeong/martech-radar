# 컨테이너 격리 검증

이 구성은 **별도 staging 검증용**이다. 운영 전환 완료 구성이 아니며 현재 systemd
서비스·예약 수집을 대체하지 않는다. Docker Engine과 Compose를 지원하는 Linux에서
아래 순서대로 실행한다. 새 workspace에서 검증한 뒤 운영 환경에 맞춰 적용한다.

## 준비된 범위

- `Dockerfile`: wheel 기반 설치, 웹과 Chromium 수집 이미지 분리, 비루트 UID 1000.
- `requirements.runtime.lock`: 검증한 Python 3.12 환경의 런타임 의존성 20개.
  개발 도구는 런타임 설치에서 제외한다. 빌드 도구는 별도 lock을 사용한다.
- `.dockerignore`: 코드·필요한 빌드 파일만 허용. 운영 DB, 설정, 토큰, 백업 제외.
- `compose.yaml`: 동일 호스트의 staging 볼륨 공유, 웹은 localhost 18848만 노출.
  컨테이너 파일시스템은 읽기 전용이며 `/tmp`와 workspace만 쓰기 가능하다.
- 수집기·예약 배치는 명시적인 profile로 실행한다. 자동 예약 타이머는 설치하지 않는다.
- collector 이미지는 sandbox 설정이 꺼져 있거나 루트 사용자이면 시작을 거부한다.
  Chromium sandbox 시작에 실패해도 비활성화하여 재시도하지 않는다.

원본 코드의 기존 Ubuntu 동작은 유지한다. 새 `scan.chromium_sandbox` 기본값은
`false`이고, staging 초기화 스크립트에서만 `true`를 지정한다.

## 1. 새 workspace와 이미지

아래 명령은 저장소 루트에서 실행한다. `RADAR_STAGE_HOME`은 **아직 존재하지 않는**
별도 디렉터리로 지정한다. 현재 운영 workspace 경로를 사용하지 않는다.
초기화 스크립트는 기존 디렉터리 재사용을 거부하며 토큰을 화면에 출력하지 않는다.
생성된 `.radar-staging` 표시가 없는 볼륨은 컨테이너 진입점에서도 거부한다.

```sh
export RADAR_STAGE_HOME="$HOME/martech-radar-staging"
.venv/bin/python deploy/init_workspace.py "$RADAR_STAGE_HOME"
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml build web
docker compose -f deploy/compose.yaml --profile collector build worker
```

파일 소유자는 UID/GID 1000을 가정한다. 다른 계정이라면 이미지 사용자와 Compose
`user`를 해당 staging 디렉터리 소유자에 맞춘다. 운영 폴더의 소유권을 변경하지 않는다.
Python 기본 이미지는 실제 빌드에 사용한 digest로 고정했다. OS 패키지 업데이트까지
바이트 단위로 고정하는 빌드는 아니므로, 승격할 때 완성된 애플리케이션 이미지의
digest도 기록한다.

## 2. 설치 패키지 검증

검증기는 매번 `/tmp`에 독립 workspace와 가상 기업을 만든다. 운영 DB나 타깃
사이트를 사용하지 않는다. 로그인, 홈·상세·복수 필터, 정적 파일과 fingerprint,
별도 프로세스의 오래된 작업 회수, 백업 복원을 검증한다.

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps --entrypoint python web \
  /opt/radar/smoke.py --require-installed
docker compose -f deploy/compose.yaml --profile collector run --rm --no-deps \
  --entrypoint python worker /opt/radar/smoke.py --require-installed --browser
```

`--browser`는 컨테이너 안의 로컬 테스트 페이지만 방문하고 실제 Chromium으로
runtime·storage 근거 검출 및 작업 완료를 확인한다. sandbox 오류가 나면 실행 환경을
점검하며 `--no-sandbox`, `privileged`, `seccomp=unconfined`로 통과시키지 않는다.

seccomp 파일은 [Playwright v1.62.0 원본](https://github.com/microsoft/playwright/blob/v1.62.0/utils/docker/seccomp_profile.json)을
기반으로 namespace 규칙에 `chroot`만 추가 허용했다. 원본 규칙은 컨테이너의
`CAP_SYS_CHROOT`가 있을 때만 이 호출을 허용하여, `cap_drop: ALL`에서 Chromium이
자기 sandbox를 만드는 단계가 실패했다. 컨테이너 capability를 추가하지 않고
해당 syscall을 허용한다. 커널의 권한 검사와 다른 seccomp 규칙은 유지된다.
원본·변경본 해시는 `seccomp_SOURCE.txt`, 라이선스는 `PLAYWRIGHT_LICENSE`에 있다.
비루트 사용자와 seccomp 구성은 [Playwright Docker 문서](https://playwright.dev/python/docs/docker)를 참고했다.

## 3. 웹 확인과 명시적 작업

```sh
docker compose -f deploy/compose.yaml up -d web
docker compose -f deploy/compose.yaml ps
```

접속 주소는 `http://127.0.0.1:18848`이며, 로그인 토큰은 staging의 `radar.toml`에 있다.
초기 등록 기업은 0개다. 실제 타깃을 추가하기 전에 위 브라우저 검증을 통과해야 한다.
운영 DB 복사본으로 화면을 검증하는 경우에는 수집 worker를 함께 실행하지 않는다.

수집 환경 검증 후 필요한 작업만 명시적으로 실행한다.

```sh
docker compose -f deploy/compose.yaml --profile collector up -d worker
docker compose -f deploy/compose.yaml --profile scheduled run --rm scheduled
docker compose -f deploy/compose.yaml run --rm web radar backup --keep 3
docker compose -f deploy/compose.yaml --profile collector --profile scheduled down
```

`scheduled`는 브라우저 없는 웹 이미지로 기존 wrapper를 실행하며, 작업은 상시 worker에
맡긴다(`--external-worker`). worker를 먼저 시작해야 한다. 중복 실행 잠금·일정 판단·로그·digest를
유지하지만 cron/timer를 자동 생성하지 않는다. 실제 이전 때에는 기존 Ubuntu
예약 수집과 중복되지 않는 별도 전환 절차가 필요하다.

`radar backup`은 DB·설정·fingerprint를 검증된 디렉터리 묶음으로 보관한다. 기본 3개,
80GiB 정책이며 이전 `.db` 백업은 자동 삭제하지 않는다. 검증·복원은
`python -m radar.recovery verify/restore`를 사용한다.

## 현재 제한과 완료 조건

- 제안 생성은 기본 비활성화다. 인증된 사설 gateway를 지정하면 실제 KB 검색·기업 조사·
  유스케이스 추천·제안서와 메일 생성까지 연결할 수 있다. [연동 절차](PREPARATION.md)를 따른다.
  CLI/KB 호스트까지 이관한 상태는 아니며, 웹에서 메일을 발송하지 않는다.
- SQLite WAL은 같은 호스트의 로컬 디스크에서만 공유한다. NFS/SMB로 여러 서버가
  공유하는 구조는 사용하지 않는다. [SQLite WAL 문서](https://www.sqlite.org/wal.html)
- 컨테이너 cgroup 계측과 일일 운영 점검을 패키징했다. [점검 항목과 한계](OPERATIONS.md)를
  확인한다. 호스팅의 자동 시작·예약·외부 경보와 실제 전체 대상 부하는 이전 환경에서 검증한다.
- 이미지 실행 검증 뒤 재시작·볼륨 보존·브라우저 sandbox·대표 수집 부하를 확인한다.
  호스팅 사업자 선정, 외부 공개와 운영 DB 이전은 별도 작업이다.

## 운영용 배포 파일 준비

검증용 구성과 별도로 `compose.hosting.yaml`, Caddy HTTPS 설정, 일일 수집·백업·독립
점검 unit 생성기, 수집 시작 잠금, worker heartbeat 검사, 백업·복원 도구를 추가했다.
절차는 [HOSTING.md](HOSTING.md)를 따른다. 운영용 구성을 작성했다고 현재 서비스를
이전하거나 예약 작업을 설치한 것은 아니다. 실제 이전은 승인 후 진행한다.

새 설정 `server.public_origin`은 HTTPS 주소·Secure 쿠키·같은 출처 변경 요청을 묶는다.
미설정인 기존 Ubuntu/로컬 동작은 유지한다. 백업은 online copy·무결성·체크섬을 검증하며
복원은 새 디렉터리에만 허용하고 수집 시작 파일을 복사하지 않는다.
