# 컨테이너 부하 기록과 운영 점검

`RADAR_CONTAINER=1`인 이미지에서 cgroup v2의 자기 경로를 읽는다. 호스트의 systemd를
호출하거나 Docker socket을 마운트하지 않는다. cgroup v1·읽기 권한 부재 등으로
수집하지 못하면 보고서에 확인 불가로 남기며 정상 부하로 간주하지 않는다.

## 기록

- 웹·상시 워커: `RADAR_TELEMETRY_ROLE=web|worker`로 5초마다 기록한다.
- 예약 배치: 기존 `weekly-scan`의 시작·종료 표본을 유지한다.
- `cpu.stat`, `cpu.max`, 메모리 사용·한도·OOM, I/O, PID 수, pressure를 기록한다.
- 호스트 모드는 기존 `/proc` 지표를 보존한다. 컨테이너에서는 호스트 전체 지표를
  중복 저장하지 않고 자기 cgroup만 기록해 로그 크기와 해석 혼란을 줄인다. CPU 비율은
  `usage_usec` 증가량 ÷ 경과 시간 ÷ 할당 CPU로 계산한다.
- 상시 서비스 로그는 UTC 날짜별로 회전하고 30일 보관한다. 프로세스 실행마다
  파일을 분리해 재시작으로 초기화된 누적 카운터를 섞지 않는다.
- 메모리 최대치는 해당 구간의 `memory.current` 표본 최댓값이며 순간 최대치를
  보장하지 않는다. OOM은 구간 내 카운터 증가량이다. 첫 표본 이전·마지막 표본 이후의
  사건이나 컨테이너 밖의 OOM은 별도 호스트 감독이 필요하다.

## 보고서

예약 wrapper는 컨테이너에서만 수집·digest·최종 부하 기록 후 보고서를 만든다.
`data/operations/daily-reviews/YYYY-MM-DD.json`에 원자적으로 저장한다.
기존 Ubuntu wrapper의 수집·digest 동작과 기존 점검 타이머는 유지한다.

수동 점검:

```sh
docker compose -f deploy/compose.yaml --profile operations run --rm review \
  python -m radar.operations --day 2026-11-02 \
  --output /workspace/data/operations/daily-reviews/2026-11-02.json
```

날짜는 기본 Asia/Seoul 기준이다. 다른 지역은 `--timezone`으로 지정한다.
보고서는 DB를 읽기 전용·일관된 트랜잭션으로 열며 schema 변경·수집 재실행을 하지 않는다.

검사 항목:

- 해당 날짜의 `scheduled YYYY-MM-DD` 실행 존재·종료 여부.
- 수집 당시 큐의 예정 작업 수와 완료 상태, scan의 기업·URL·run 연결 정합성.
- 정상·robots 제외·탐색 실패·차단·시간 초과 등 상태별 수와 관측 오류.
- 부하 기록 누락·파싱 오류·DB 조회 오류·실행 구간 표본 부족.
- OOM 증가, 표본 메모리 사용률 90% 초과, 표본 CPU 할당량 95% 초과,
  표본 간격 30초 초과, 설정된 수집 동시 실행 한도 초과, 카운터 초기화. CPU 항목은 점검 신호이며 지속 과부하 확정은 아니다.

`complete`는 예정 작업의 기록 완료, `healthy`는 관측·부하 점검 신호까지 없는 상태다.
robots 정책 제외는 오류 수에서 분리한다. `healthy=false`이면 종료 코드 2이며,
수집·digest 자체 실패 코드는 우선 보존한다. 스케줄러에서 실패해도 자동 재수집하지 않는다.

예정 분모는 수집 전에 저장한 URL별 계획이다. 큐 등록 이전의 일일 한도·필터 제외도
점검하며, 현재 기업 목록 변경에 따라 과거 누락 수가 바뀌지 않는다.
주기상 수집일이 아닌 대상은 제외한다. 전체 대상 정책·요일 설정의 정확성은
배포 설정과 스케줄러 테스트에서 별도로 검증한다.
자동 경보 전송·외부 모니터링 서비스 등록은 하지 않는다.

## 확대 대비 점검

예약 CLI는 수집 전 URL별 예정 목록과 제외 사유를 `collection_plan`에 저장한다.
점검은 큐 완료와 예정 목록 충족을 따로 확인한다. 일일 한도·필터 때문에 빠진 URL도
`missing_expected_urls`에 남는다. 이전 실행에 예정 목록이 없으면 `pre_queue_plan_missing`으로
표시하며, 현재 watchlist로 과거 분모를 추정하지 않는다.

수집 종료 직후 점검 외에 hosting 독립 점검은 09:15~15:15 매시 실행한다.
기본 10시간을 넘긴 실행은 `collection_duration_exceeded`로 표시한다. DB/WAL 크기와
디스크 여유도 보고하며 재수집·서비스 재시작·외부 알림을 자동 실행하지 않는다.
Ubuntu에서는 수집 서비스의 `ExecStopPost`와 독립 `radar-coverage-review.timer`가
`deploy/maintenance.py review --output-dir data/operations/coverage-reviews`를 실행한다.
기존 daily-reviews와 다른 경로에 기록하므로 이전 점검 결과와 서로 덮어쓰지 않는다.

## 추가 점검 도구

`review_rollout.py`, `review_target_recovery.py`, `review_collector_followup.py`,
`review_policy_references.py`는 명시한 날짜와 대상 manifest로 결과를 저장하는 점검 도구다.
실행 옵션은 각 스크립트의 `--help`를 참고한다. 실제 기업 manifest와 날짜별 예약 unit은
공개 저장소에 포함하지 않으며, 새 설치에 자동 등록되지 않는다.
