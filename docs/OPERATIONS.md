# 로컬·Ubuntu 운영

## Workspace와 서비스

`radar.toml`, `data/`, `backups/`가 있는 workspace를 기준으로 실행합니다. 기본값은 현재 디렉터리이며 `RADAR_HOME` 또는 CLI의 `--home`으로 지정합니다. 코드의 `.venv`를 사용하는 `bin/radar`와 일반 설치의 `radar` 명령을 혼동하지 마세요.

`bin/install-service`는 사용자 systemd에 대시보드·워커·수집·백업을 등록하고 즉시 활성화합니다. 기존 unit을 덮어쓸 수 있으므로 새 설치 또는 검토된 변경에 사용합니다. 일정은 기본 Asia/Seoul입니다. 로그아웃 이후 실행은 해당 계정의 lingering 설정이 필요합니다.

```bash
bin/radarctl status
bin/radarctl logs worker
bin/radarctl doctor
systemctl --user list-timers 'radar-*'
```

워커와 예약 배치의 수집은 DB 큐와 실행 한도를 공유합니다. `cadence.daily_limit`은 기업 수 제한이며 URL별 계획과 실제 완료 수를 함께 확인해야 합니다. 실패한 방문도 주기에 반영되므로 즉시 반복 방문하지 않습니다. 전체 일일 수집으로 바꾸려면 tier별 주기 재정의도 확인하세요.

## 약관 자동 수집

```bash
RADAR_HOME=/absolute/workspace .venv/bin/python -m radar.policy_schedule
```

OS 스케줄러에서 주기적으로 실행할 수 있습니다. 예를 들어 Asia/Seoul 08:00~22:30에 30분 간격으로 실행하도록 설정합니다. 스케줄러 설치·활성화는 직접 수행해야 하며 기본 서비스 설치기에 포함되어 있지 않습니다.

- 한 번에 최대 5곳, 최대 120초의 작은 HTTP 배치입니다. 기업별 재시도·갱신 간격은 최소 7일입니다.
- 야간 수집 시간에는 실행하지 않습니다. 수집·생성 작업이 있거나 여유 메모리가 4GiB 미만, load가 CPU 수의 절반 이상이면 연기합니다. 작은 호스팅 서버에서는 이 보수적 조건 때문에 계속 연기될 수 있습니다.
- robots와 명시적 차단을 존중하고 브라우저·로그인·차단 우회를 사용하지 않습니다.
- 기본 시간이 Asia/Seoul에 고정된 모듈입니다. 다른 지역에서 사용하려면 코드의 시간 창을 검토해야 합니다.
- 결과는 참고 정보입니다. 기업의 실제 솔루션 사용·미사용을 확정하지 않습니다.

## 누락·오류·부하

```bash
bin/radar runs
bin/radar hygiene --json
.venv/bin/python -m radar.operations --help
.venv/bin/python deploy/maintenance.py --help
```

예정 목록·큐 완료·정상 관측은 서로 다른 지표입니다. 모든 작업이 끝나도 사이트 차단이나 관측 오류가 있을 수 있습니다. 수집 시간이 느는 경우 동시 실행 수를 무조건 늘리기보다 느린 URL, 방문 제한, 관측 시간, DB·디스크 부하를 먼저 확인합니다.

컨테이너의 cgroup 계측과 일일 보고서는 [deploy/OPERATIONS.md](../deploy/OPERATIONS.md)에 있습니다. 재사용 가능한 `deploy/review_*.py`는 명시한 날짜·manifest로 결과를 저장하는 점검 도구이며 Codex 채팅에 메시지를 보내거나 대상을 자동 확대하지 않습니다. 실제 운영에서 사용한 기업 manifest·날짜별 unit·결과는 공개 저장소에 없습니다.

## 백업과 복원

```bash
bin/radar backup --keep 3
.venv/bin/python -m radar.recovery --help
```

백업은 online SQLite 복사, 무결성·체크섬 확인을 거친 묶음입니다. 기본 보관 수는 3개, 용량 한도는 80GiB이며 저장소와 회복 목표에 맞게 설정합니다. 검증한 최신 백업을 남긴 채 오래된 묶음을 정리합니다. 과거 단일 DB 백업은 자동 삭제 대상이 아닙니다.

복원은 새 디렉터리에서 수행하고 검증 후 서비스를 전환합니다. 원본 DB 파일을 실행 중에 단독 복사하거나 실제 운영 폴더에 복원본을 덮어쓰지 않습니다. 백업의 외부 서버·오브젝트 스토리지 보관은 별도 운영 작업입니다. 설정·초안이 포함되므로 백업도 비공개로 관리하세요.
