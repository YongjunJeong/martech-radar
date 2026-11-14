# 호스팅 전환 준비와 실행 절차

새 호스트에서 격리 검증을 마친 뒤 전환한다. 이 문서의 명령은 서비스 설치·DNS·운영 데이터 이전을 포함할 수 있다. 기존 수집과 중복 실행하지 않도록 전환 순서와 복원 경로를 먼저 정한다.

## 지원하는 배포 형태

첫 배포는 Docker Compose를 지원하는 **단일 Linux 호스트**, cgroup v2, 영속 로컬
디스크를 전제로 한다. 웹·수집 워커·예약 실행기는 같은 호스트의 SQLite 볼륨을 공유한다.
브라우저는 상시 워커 한 곳에서만 실행한다. 예약 실행기는 웹 이미지를 사용하며
`batch --external-worker`로 큐 등록·완료 대기만 한다(512MiB / 0.5 CPU 제한).
워커 heartbeat가 없으면 큐 등록 전에 실패하고, 대기 중 워커가 사라지거나 12시간을
넘기면 실패를 기록한다. 이미 등록된 작업은 보존하며 별도 브라우저로 대신 실행하지 않는다.
정적 웹 호스팅이나 요청 종료 시 프로세스가 사라지는 환경은 이 구성의 대상이 아니다.
NFS/SMB로 SQLite를 여러 호스트에 공유하지 않는다.

- `compose.hosting.yaml`: 검증용 Compose와 별도 프로젝트. 웹 포트를 공개하지 않고
  Caddy만 80/443을 노출한다. 웹·프록시·상시 워커는 `unless-stopped`로 재기동한다.
- 수집·예약 배치는 profile과 workspace의 `.collection-enabled`가 모두 필요하다.
  초기화·복원은 이 파일을 만들지 않는다. 실수로 worker를 시작해도 브라우저 실행 전에 거부한다.
- `hosting_preflight.py`: HTTPS 주소, 로그인 토큰, sandbox, 일일 전체 수집 설정,
  미완료 작업·DB 무결성·디스크 여유·별도 백업 경로를 읽기 전용으로 점검한다.
- `render_units.py`: 수집 03:00~03:20 KST, 백업 02:30~02:35 KST, 독립 점검 09:15~15:15 KST(매시)의
  systemd service/timer 6개를 **새 폴더에 생성만** 한다. 현재 서버에 설치하지 않는다.
  예약 배치가 강제 종료돼 자체 보고서를 못 써도 독립 점검에서 누락을 발견할 수 있다.
- 작업 실패를 자동 재수집하지 않는다. 컨테이너 health가 unhealthy여도 Docker가 이를
  이유로 자동 재시작하는 것은 아니다. 호스트의 경보 체계에서 별도로 감시한다.

## 배포 파일과 이미지 준비

도구는 프로젝트가 설치된 Python(`.venv/bin/python`)으로 실행한다. 새 호스트에 Python
환경을 두지 않을 경우 검증된 웹 이미지의 `/opt/radar/init_workspace.py`,
`/opt/radar/hosting_preflight.py`, `python -m radar.recovery`를 명시적 entrypoint로 실행한다.
초기화는 부모 디렉터리를 별도 마운트하여 **존재하지 않는 하위 폴더**를 대상으로 한다.

```sh
docker build --target web -t martech-radar-web:RELEASE .
docker build --target collector -t martech-radar-collector:RELEASE .
docker image inspect martech-radar-web:RELEASE martech-radar-collector:RELEASE \
  --format '{{.RepoTags}} {{.Id}}'
```

이미지는 레지스트리에 게시하지 않았다. 새 호스트로 전달할 때 같은 검증 이미지의 digest
또는 로컬 image ID를 사용하고, 태그를 재사용해 덮어쓰지 않는다. `hosting.env.example`을
새 호스트의 `hosting.env`(0600)로 복사한 뒤 경로·도메인·검증한 이미지 참조를 지정한다.
Caddy는 이번에 설정 검증한 digest를 예시에 기록했다. 업데이트는 별도 검증 후 수행한다.

`RADAR_BACKUP_HOME`은 workspace 밖에 만들고 UID/GID 1000에 쓰기 권한을 준다.
다른 UID를 쓸 때는 이미지·Compose·볼륨 소유자를 함께 맞춘다. 기존 운영 폴더의 소유권은
변경하지 않는다. 제안 기능을 끄면 gateway token secret에는 별도의 빈 파일을 사용한다.
켜면 [PREPARATION.md](PREPARATION.md)의 HTTPS gateway와 실제 secret이 필요하다.

```sh
.venv/bin/python deploy/init_workspace.py /NEW/workspace \
  --mode hosting --public-origin https://radar.example.com
# 초기 workspace에는 기업이 없다. 운영 이전은 아래의 최종 백업 복원 절차를 따른다.
docker compose --env-file deploy/hosting.env -f deploy/compose.hosting.yaml \
  --profile collection --profile jobs config --quiet
```

`server.public_origin`에는 소문자 호스트의 HTTPS origin을 지정한다. 경로·기본 `:443`은
붙이지 않는다(브라우저가 보내는 origin과 정확히 일치해야 한다). Caddy의
`RADAR_PUBLIC_HOST`와 같아야 한다. 애플리케이션은 이 주소로 링크·변경 요청을 검증하고
세션 쿠키를 Secure로 발급한다. 임의의 `X-Forwarded-*`를 기준으로 주소를 바꾸지 않는다.
인증서 발급·갱신에는 실제 DNS와 80/443 접근이 필요하며 이는 이전 환경에서 확인한다.
[Caddy HTTPS 문서](https://caddyserver.com/docs/automatic-https)

## 백업·복원

`radar.recovery backup`은 SQLite online backup을 이용한다. DB·설정·workspace의
fingerprint YAML을 보관하고, DB 무결성·FK·파일 SHA-256을 확인한 뒤 완성본만 공개한다.
대상 목록·이력·근거·제안 초안은 DB에 포함된다. 설정에 비밀 값이 있을 수 있어 백업
디렉터리는 0700으로 만들며, 외부 보관 시 별도로 암호화해야 한다.

```sh
.venv/bin/python -m radar.recovery backup --home /SOURCE/workspace \
  --destination /PRIVATE/backups --keep 3 --max-gib 80 --min-free-gib 2
.venv/bin/python -m radar.recovery verify /PRIVATE/backups/radar-SNAPSHOT
.venv/bin/python -m radar.recovery restore /PRIVATE/backups/radar-SNAPSHOT \
  /NEW/restored-workspace --mode hosting
```

로컬 기본 보관은 3개·합계 80GiB다. 용량을 초과하면 오래된 것부터 줄이되 최소 2개는 남긴다.
새 복사본을 만들 공간과 잔여 2GiB를 확보하지 못하면 기존 백업을 지우지 않고 실패한다.
한 개 DB가 40GiB보다 커지기 전에 외부 보관/용량 증설이 필요하다.
`hosting.env`의 `RADAR_BACKUP_*`로 설정하고, 기존 14벌 운영 백업은 승인 없이 일괄 삭제하지 않는다.
예약 백업과 관리 화면에서 요청한 워커 백업은 같은 `radar.recovery` 엔진을 사용한다.
`radar backup`도 같은 검증 묶음을 만든다. 이전 `.db` 단일 파일은 삭제 대상이 아니다.
워커와 예약 백업 컨테이너는 동일 `/backups` 볼륨과 환경 정책을 공유한다.
일반 CLI에서도 `RADAR_BACKUP_DIRECTORY`, `RADAR_BACKUP_KEEP`, `RADAR_BACKUP_MAX_GIB`,
`RADAR_BACKUP_MIN_FREE_GIB`를 사용하며 명시적 CLI 인자가 환경 설정보다 우선한다.

복원 경로가 이미 있으면 거부한다. 체크섬 불일치·손상·경로 이탈도 거부한다.
기존 폴더를 덮어쓰거나 원본 수집 기록을 고쳐 재실행하는 기능은 없다.
`.collection-enabled`, 실행 잠금, CLI 인증, 외부 secret, Caddy 인증서, 운영 로그는
복원 묶음에 포함하지 않는다. secret·KB 인덱스·인증서는 각 서비스의 별도 보존 정책을 따른다.
**같은 호스트의 백업만으로 디스크·호스트 장애를 복구할 수는 없다.** 호스팅 결정 후
암호화된 외부 보관 위치와 복원 시험을 연결한다. 이번에 외부로 데이터를 전송하지 않았다.

## 실제 전환 순서 — 승인 후 실행

1. 새 호스트에 이미지·TLS·gateway·secret·백업 경로를 준비하고 테스트 데이터로 점검한다.
   원본 모니터링은 계속 가동한다. 새 호스트의 수집 시작 파일은 만들지 않는다.
2. 승인된 전환 시간에 기존 웹의 쓰기와 예약 수집 진입을 막는다. 진행 중인 수집·작업·
   제안 생성이 끝났는지 확인한 뒤 기존 worker/서비스를 정지한다. 이전 호스트가 재부팅돼도
   수집이 다시 시작되지 않도록 앱의 timer·worker 자동 시작도 비활성화하고 확인한다.
   유지할 별도 KB/gateway 서비스까지 함께 중단하지 않는다. 이 순서가 최종 백업
   이후 새 기록이 생기거나 양쪽에서 중복 수집되는 것을 막는다.
3. 최종 online backup을 만들고 검증한다. 새 호스트의 **새 경로**로 복원한다.
   `public_origin`, sandbox, 일일 주기, gateway 주소·secret 경로를 새 환경에 맞춘다.
   복사된 이전 token을 유지할지 교체할지도 전환 시 정한다.
4. 아래 preflight를 통과시킨다. 이전 host가 정지했다는 사실은 이 도구가 원격으로
   증명하지 않으므로 운영자가 별도로 확인한다. 미완료 작업이 있으면 자동 재시도하지
   말고 원본 작업 상태를 먼저 정리한다.

   ```sh
   .venv/bin/python deploy/hosting_preflight.py --home /NEW/restored-workspace \
     --backups /PRIVATE/backups --public-host radar.example.com
   ```

5. 새 웹·프록시만 시작해 로그인·기업 수·최근 이력·필터·제안 초안을 확인한다.
   비밀번호·token을 명령줄 인자로 넘기지 않는다.
6. 이전 호스트의 수집이 정지한 것을 확인한 뒤 새 workspace에 내용이 정확히
   `collection-enabled-v1`인 `.collection-enabled` 파일을 생성한다. 그다음에만
   `--profile collection up -d worker`를 실행하고 heartbeat가 정상인지 확인한다.
   파일을 지우는 것만으로 **이미 실행 중인** worker가 중단되지는 않는다.
7. 새 호스트에 생성한 timer를 설치·활성화하고 다음 수집·백업·점검 시간을 확인한다.
   예약 실행기는 워커를 자동 시작하지 않으므로 6번 워커 시작·heartbeat 확인을 먼저 마친다.
   `Persistent=true`이므로 지난 예약이 있으면 시작 직후 실행될 수 있다.
   [systemd timer 동작](https://www.freedesktop.org/software/systemd/man/256/systemd.timer.html)

   ```sh
   .venv/bin/python deploy/render_units.py /NEW/rendered-units \
     --deployment /opt/martech-radar/deploy --workspace /NEW/restored-workspace
   ```

8. 새 수집의 예정/실제 작업 수·오류·부하·다음 예정일, 새 백업의 복원을 확인한다.
   collector의 기본 3CPU/6GiB 제한이 충분한지, 합산 CPU·메모리와 디스크 여유도 실제 부하로 확인한다.

## 되돌리기

- 새 호스트에서 아직 쓰기가 없으면 새 서비스를 정지하고 이전 호스트만 재개할 수 있다.
- 새 수집·초안 변경이 있으면 **새 호스트를 먼저 정지하고 최신 DB를 검증해 역이관**한다.
  오래된 백업을 덮어쓰면 새 기록이 사라진다. 이전 이미지가 새 스키마를 지원하는지도 확인한다.
- 두 호스트의 DB를 병합하거나 양쪽 수집을 동시에 재개하지 않는다.
- 수집 도중 강제 종료되면 기존 claim 회수 기준은 15분이다. 완료 여부가 불명확한 유료
  제안 생성은 자동 반복하지 않고 실패 상태와 기존 초안을 확인한다.

## 아직 실제 환경에서 확인할 사항

DNS·공인 TLS 발급/갱신, 영속 디스크, 재부팅, 백업 외부 보관, 외부 경보, gateway/KB
프로비저닝과 인증, egress·cloud metadata 접근 제한, 전체 대상 수집 부하와 사이트 차단률.
코드 검증을 통과했더라도 이 항목까지 완료한 것으로 보지 않는다. 현재 운영 타이머는
계속 유지하며, 실제 이전 일정은 준비와 환경 검증이 끝난 뒤 결정한다.

## KB·제안 생성도 함께 호스팅하기

[PREPARATION_HOSTED.md](PREPARATION_HOSTED.md)의 선택 overlay를 사용하면 개인 PC나
Claude CLI 로그인 없이 KB 검색·공개 웹 조사·제안서·메일 초안을 운영할 수 있다.
API 키와 검증된 KB 번들을 별도로 준비한다. 기존 CLI gateway 구성도 유지한다.
