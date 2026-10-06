[English](./CHANGELOG.md) | **한국어**

# 변경 이력

이 프로젝트의 모든 주요 변경 사항은 이 파일에 기록됩니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 따르며,
이 프로젝트는 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)을 준수합니다.

## [Unreleased]

## [1.18.0] — 2026-10-06 (Claude Code status view)

### Added

- Claude Code 2.1.287 이상에서 디스패치된 좌석을 보여 줍니다. Bash 호출 안의 `dispatch_agent.py run`을 명령줄에서 읽어 추적하고, 끝날 때까지 `status`로 확인해 `seats: codex·<model-id> RUNNING 4m/20m` 같은 상태줄에 표시합니다.
- 좌석이 끝날 때마다 토스트를 띄우고, 사람이 처리해야 하는 상태를 표시합니다: `TERMINATION_UNCONFIRMED`, orphaned·stale 감독, 마감을 한참 넘긴 `RUNNING` 영수증, `status`가 거부한 성공.
- `/router-seats`를 추가했습니다. attempt별 패널의 Status·Cancel·Verify 버튼은 입력창을 채우기만 하며, mod가 읽지 못한 디스패치용 `/router-seats add <receipt-dir> <attempt-id>`와 `/router-seats clear`가 함께 있습니다.
- Claude Code에서 라우트의 리뷰 좌석에 이 세션의 모델이 앉으면 경고하고, `review_context` 없는 `REVIEW` 라우트에 힌트를 한 번 보여 주며, `claude --bare` 좌석과 `caffeinate -i` 밖의 긴 macOS 디스패치를 표시합니다.
- 세션 시작 시 model-sync 퇴역 알림, 다시 시도할 때가 된 보류 프로브, 진행 중인 프로브 실행을 토스트로 한 번 알리고, 자세한 내용은 `/router-sync`에서 보여 줍니다.
- 선언된 호스트 모델이 리뷰 좌석에 앉았는데 `implementer`도 `review_context`도 선언되지 않으면, 모든 런타임에서 라우트에 note를 남깁니다. 좌석은 라우팅된 그대로입니다.

### Changed

- `claude -p --bare` 좌석이 `Not logged in`으로 실패한다는 점과, 유휴 수면이 디스패치와 그 감독 프로세스를 멈춘다는 점(긴 macOS 디스패치는 `caffeinate -i`로 감쌀 것)을 문서화했습니다.

## [1.17.2] — 2026-09-30

### Changed

- GPT-6.1 Sol과 Claude Sonnet 5.5의 입회 maker 좌석 프로브를 기록했습니다. 둘 다 통과했으므로 검증 원장은 더 이상 두 maker 좌석을 미검증으로 표시하지 않습니다.
- Claude Sonnet 5.5의 tier를 옮기기 전에 tier-2 수준인지 측정했습니다. 이 리포의 실제 리뷰를 재현한 평가에서 알려진 결함의 21%를 찾았습니다. tier-2 좌석은 31–42%, 이전 세대는 8%를 찾았으므로, 실행 전에 정한 기준에 못 미쳐 tier 1을 유지합니다. 이제 이 판단은 측정 근거에 기반합니다. 방법과 한계는 모델 프로파일과 검증 원장에 있습니다.

## [1.17.1] — 2026-09-30

### Changed

- 로컬 오버레이의 격리·effort 프로브를 거쳐 GPT-6.1 Sol을 reasoning specialist 모델로 앉혔습니다. 공표 가격은 입력/출력 백만 토큰당 $2 / $10으로 그대로이고, 캐시 입력만 $0.10으로 절반이 됐습니다. `capability_tier`는 Sol 계열에서 승계했고, 품질과 maker 좌석은 재확인하지 않았으며, 모델 프로파일과 검증 원장이 이를 명시합니다. GPT-6 Sol은 이력 입력으로 계속 유효합니다.
- 같은 프로브를 거쳐 Claude Sonnet 5.5를 Claude 균형 모델로 앉혔습니다. 가격은 그대로입니다($2 / $10, 캐시 입력 $0.20). `capability_tier`는 Sonnet 계열에서 승계했고, 품질과 maker 좌석은 재확인하지 않았으며, 모델 프로파일과 검증 원장이 이를 명시합니다. Claude Sonnet 5는 이력 입력으로 계속 유효합니다.
- 새 세대의 출시 벤치마크에도 불구하고 tier와 바인딩은 모두 그대로 둡니다. 모델 프로파일에 벤더 수치와 후보 변경(Sonnet을 tier 2로, Sonnet을 기본 균형 워커로, Sol을 tier 3으로)마다의 재생 결과를 기록했습니다.
- README: deep-loop 1.25.0 이상은 `policy_pin`을 넘기므로 오버레이 발행으로는 진행 중인 deep-loop 실행이 멈추지 않습니다. `DEEP_MODEL_ROUTER_AUTOUPGRADE=0`은 이제 이전 deep-loop 버전에서 긴 실행을 돌릴 때만 권합니다. 플러그인 업데이트 뒤에는 여전히 새 실행이 필요합니다.

### Fixed

- GPT-6.1 Sol은 OpenAI 가족 매핑이 보내는 `none` 토큰을 거부하므로, 이 모델에서는 MINIMAL effort를 `low`로 매핑합니다.
- Claude senior·균형 좌석의 현 세대는 대체 경로가 있는 거절 분류기를 유지하므로, `security_sensitive` 작업에서 제공자 쪽 모델 대체 가능성을 공시합니다. senior 좌석은 1.16.0부터 이 공시가 빠져 있었습니다.

## [1.17.0] — 2026-09-28 (review sized to the band)

### Added

- 이미 쓰기 작업을 끝낸 모델을 RouteRequestV1 `implementer`로 선언할 수 있습니다. 라우트는 그 모델을 기준으로 작업의 리뷰를 계획하고, `worker_seat_state: already_executed`를 보고하며, `dispatch_seats`에는 리뷰 좌석만 담고, 정책이 앉혔을 워커보다 약한 구현자는 게이트합니다(`implementer_below_worker_tier`, exit 3, hotfix로도 유예되지 않음).
- 저장소가 `LOW` 리뷰를 이루는 결정적 검사를 돌릴 수 없으면 `availability_snapshot.checks_available: false`(CLI `--checks-unavailable`)로 알립니다. 그러면 그 리뷰는 `MEDIUM` 모델 리뷰어를 앉힙니다.
- 쿼터 판독을 `availability_snapshot.family_quota`(CLI `--family-quota openai=low`)로 넘깁니다. `exhausted`는 그 family를 모든 좌석에서 보류하고, `low`는 워커만 같은 tier의 다른 family 모델로 옮깁니다.
- 타입 시도 레코드에 시도가 실제로 받은 effort와 같은 모델 재시도용 새 증거(`effort`, `retry_evidence_sha256`)를 기록할 수 있습니다.
- 모든 라우트가 `review.mode`(`model_review` 또는 `deterministic_checks`), `worker_seat_state`, `implementer_declared`, `implementer_source`를 출력합니다.
- 짝 측정에서 부팅 입력이 27% 줄어, Claude Code에서 가는 읽기 전용 Codex 리뷰어 좌석에 `--ignore-user-config --ephemeral`을 채택했습니다. 그런 세션은 재개할 수 없고 `config.toml`을 무시합니다.

### Changed

- `LOW` 밴드 라우트는 워커 자신의 모델 대신 라우트가 이름 붙인 결정적 검사(`tests`, `lint`)로 리뷰합니다. 호출자는 작업을 받아들이기 전에 검사를 통과시켜야 하며, 분쟁·검사 불가·`local_policy`의 리뷰어/family 하한은 그것을 감당하는 가장 낮은 밴드로 라우트를 올립니다.
- `LOW` 위험 작업의 표 effort를 `MEDIUM`으로 상한합니다. 원인 불명이거나 이전 시도 기록이 하나라도 있으면 예외이고, 실행 floor·로컬 하한·보상 규칙은 여전히 이깁니다.
- 불확실성을 한 번만 셉니다. 이중 가중치만으로 밴드가 올라갔다면 같은 불확실성이 리뷰를 다시 승격하지 않으며, 보고되는 신뢰도와 `ESCALATE_ROUTING`은 벌점 전체를 유지하고 워커 tier는 내려가지 않습니다.
- `MEDIUM` 리뷰어는 밴드 하한과 구현자 tier에 닿는 가장 낮은 tier를 교차 가족 우선으로 앉히며, 구현자 tier에 못 미치는 `MEDIUM` 리뷰는 게이트합니다.
- capability 실패가 effort와 새 증거를 선언하고, 실패한 모델이 그 하나뿐이며, `retry.same_model_higher_effort` 이내이고, 다음 effort가 모델 상한 안이며, 정착된 라우트가 여전히 그 모델을 앉히면, 그 모델의 어떤 기록보다도 한 단계 높은 effort로 같은 모델을 재시도합니다.
- `REVIEW` 작업의 리드를 `review_context` 유무와 무관하게 밴드의 reviewer-1로 셉니다. `HIGH`·`CRITICAL` 리뷰에 리뷰의 리뷰어 둘이 더 붙지 않습니다.
- `LOW` 라우트의 `minimum_reviewers: 2`는 `UNSATISFIABLE_LOCAL_POLICY`에서 멈추지 않고 `HIGH`로 라우팅됩니다.
- 1.16보다 약해짐: 릴리스 측정 격자 104,640개 라우트 중 5,136개가 규칙상 `LOW`의 모델 리뷰어를 잃고(C3), `LOW` `REVIEW` 리드 24개는 워커 effort로 돕니다.
- 1.16보다 약해짐: 불확실성이 자기 밴드를 승격시키던 7,506개 라우트가 한 밴드 낮게 리뷰되고(C1-iii) — 그중 6,860개는 `CRITICAL` 대신 `HIGH`라 그 밴드의 사람 게이트가 없습니다 — 그 승격에 기대던 `minimum_reviewers: 2` 라우트 33개는 `UNSATISFIABLE_LOCAL_POLICY`에서 멈춥니다.
- 1.16보다 약해짐: `MEDIUM` 리뷰 7,069개가 하한에 맞춘 더 낮은 tier 리뷰어를 앉히고(C4), 8개는 구현자 tier에 맞는 빈 모델이 없어 게이트됩니다.
- 1.16보다 약해짐: 재시도 7,408개가 tier를 올리는 대신 실패한 모델을 effort 한 단계 올려 유지합니다(C5). 그중 2,573개는 더 낮은 tier 좌석이 리뷰하고, 164개는 한 밴드 낮으며(120개는 더 이상 `CRITICAL`이 아님), 1,968개는 1.16의 더 강한 워커 때문에 판정할 모델이 없던(`no_adjudicator`) 곳에 판사를 앉힙니다.
- 1.16보다 약해짐: context 없는 `REVIEW` 라우트는 좌석 하나 적게 리뷰하므로(REVIEW 리드) 1,977개 라우트가 `cross_family_review`를 잃고(리드 혼자면 두 번째 family가 없음), 623개는 더 낮은 tier 리드를 리뷰어로 셉니다.
- 1.16보다 약해짐: `implementer`를 선언하면 밴드 tier를 그 family만 공급할 때 리뷰 549개가 한 family를 공유하고, 쿼터 판독에서 `exhausted` family는 보류된 모델처럼 슬레이트를 얇게 합니다(601개 라우트가 `cross_family_review`를 잃고, 607개는 더 낮은 tier 리뷰어를, 31개는 더 낮은 tier 워커를 앉힘).
- `LOW` 위험 작업의 워커 effort가 3,132개 라우트에서 표의 `HIGH`에서 `MEDIUM`으로 내려갑니다(C2). 그중 24개는 1.16보다 약해집니다: 그 워커가 `review_context`를 가진 `LOW` `REVIEW` 리드라 리뷰 좌석이 `HIGH` 대신 `MEDIUM`으로 돕니다.
- deep-loop는 `LOW`의 결정적 검사를 강제하지 않습니다. 이전에도 `LOW` 리뷰어를 디스패치하지 않았습니다.
- 업데이트는 정책 다이제스트 변경으로 진행 중인 deep-loop 실행을 멈춥니다. 실행 중인 루프를 먼저 마무리하세요. 1.17 필드를 쓴 요청은 1.16에서 exit 2를 받습니다.

### Removed

- `review.MEDIUM.preferred_by_implementer`를 제거하고 하한 맞춤으로 대체했습니다.

## [1.16.1] — 2026-09-26

### Changed

- 로컬 오버레이의 격리·effort·maker 좌석 프로브를 거쳐 GPT-6 Sol을 reasoning specialist 모델로 앉혔습니다. 공표 가격은 입력/출력 백만 토큰당 $2 / $10으로, 이전의 $4 / $20보다 낮습니다. `capability_tier`는 Sol 계열에서 승계했고 품질은 재측정하지 않았으며, 모델 프로파일과 검증 원장이 이를 명시합니다. GPT-5.6 Sol은 이력 입력으로 계속 유효합니다.
- 같은 프로브를 거쳐 GPT-6 Luna를 빠른 워커 모델로 앉혔습니다. 공표 가격은 입력/출력 백만 토큰당 $0.10 / $0.50으로, 이전의 $0.20 / $1.20보다 낮습니다. `capability_tier`는 Luna 계열에서 승계했고 품질은 재측정하지 않았으며, 모델 프로파일과 검증 원장이 이를 명시합니다. GPT-5.6 Luna는 이력 입력으로 계속 유효합니다.

### Fixed

- 사용량 한도 보류를 기록된 초기화 시각까지 붙잡지 않고, 새 사용량 측정값에 여유가 있으면 즉시 해제합니다.
- 후속 모델이 이미 현 모델이 된 보류는 정리해서, 쿼터가 회복된 뒤 SessionStart 틱이 세션마다 헛되이 깨어나지 않게 했습니다.

## [1.16.0] — 2026-09-25

### Added

- 디스패치 가능한 모든 레지스트리 행에 벤더 계열(lineage)을 선언하고, 행의 id가 계열 템플릿을 어기거나 다른 행이 이미 가진 id인 정책을 거부합니다.
- 각 계열을 이 기계에서 따라갑니다. 로컬 모델 오버레이가 CLI 모델 카탈로그에서 후속 id를 오프라인으로 찾고(Claude 카탈로그 캐시가 없으면 Claude 행마다 격리된 별칭 프로브 한 번으로), 격리된 읽기 전용 프로브로 검증한 뒤 발행하므로, 라우터는 그 행을 새 id로 앉히며 tier는 승계, 가격은 unavailable, maker 좌석은 재프로브되지 않았다는 안내를 붙입니다.
- 모든 라우트에 오버레이 출처를 `model_overlay`로 보고하고, 커밋된 로컬 상태가 손상되었거나 상태 루트가 승인 검사를 통과하지 못하면 `MODEL_STATE_UNAVAILABLE`로 fail-closed 합니다. 이 라우트는 모델을 하나도 명시하지 않으며, 문서화된 모든 라우트 키를 null 또는 빈 값으로 담습니다.
- `model_sync.py` 명령 `status`, `revert`, `unblock`, `disable`, `enable`, `repair`, `quota`, `probe-maker`, `promote`를 추가하고, `DEEP_MODEL_ROUTER_AUTOUPGRADE=0`과 `DEEP_MODEL_ROUTER_OVERLAY=off`를 끄는 스위치로 제공합니다.
- 보관된 오버레이 세대, 또는 첫 세대 이전의 번들 정책의 라우팅 정책을 재현하는 `policy_pin`(`--policy-pin`)을 받습니다. 어떤 보관 세대와도 일치하지 않거나 이후의 폐기로 무효가 된 pin은 거부합니다.
- 오프라인 틱을 돌리고 후속 모델이 도래했을 때만 분리된 프로브 실행을 시작하는 SessionStart 훅을 함께 배포합니다.
- codex의 텍스트·JSON 출력을 CLI가 보고한 모델과 토큰 사용량을 기록하는 응답 엔벨로프로 읽으며, 실패했거나 끝나지 않은 턴은 fail-closed 합니다.

### Changed

- 격리·effort·maker 좌석 프로브를 거쳐 Claude Opus 5.5를 senior engineer 모델로 앉혔습니다. 공표 가격은 입력/출력 백만 토큰당 $4 / $20으로, 이전의 $5 / $25보다 낮습니다. `capability_tier`는 Opus 계열에서 승계했고 품질은 재측정하지 않았으며, 모델 프로파일과 검증 원장이 이를 명시합니다. Claude Opus 5는 이력 입력으로 계속 유효합니다.
- 대체된 모델 행을 `_retired` 키 대신 `<좌석>@<id>` 이름의 이력 행으로 둡니다. 그 id는 이력 입력으로 계속 유효하며 좌석에는 앉지 않습니다.
- 자식이 codex인 `--receipt-guard` 디스패치는 codex가 항상 자체 sandbox를 쓰므로, 호출자가 `--allow-nested-sandbox no-file-access`로 명시하지 않으면 모두 거부합니다.
- OpenAI 추론 좌석과 빠른 워커 좌석의 후속 모델인 GPT-6 Sol과 GPT-6 Luna는 이번 릴리스에 포함되지 않습니다. Codex 사용량 한도로 프로브가 보류됐으며 이후 패치 릴리스에서 반영합니다.
- 업데이트하면 진행 중인 deep-loop 실행이 정책 다이제스트 변경으로 멈춥니다. 업데이트 전에 진행 중인 루프를 마무리하고, 멈춘 루프는 새 실행으로 다시 시작하며, 오버레이 발행도 같은 효과를 내므로 긴 실행 동안에는 `DEEP_MODEL_ROUTER_AUTOUPGRADE=0`을 설정하십시오.

### Security

- 로컬 모델 상태를 검증된 디렉터리 핸들 하나를 기준으로 열고, 현재 사용자가 소유하며 소유자 전용 권한을 가진 단일 링크 일반 파일만 크기 상한 안에서 받아 엄격한 JSON으로 파싱합니다.
- SessionStart 훅의 신뢰 경계를 문서화했습니다. Codex의 훅 승인은 명령 문자열만 다루며, 훅이 실행하는 스크립트는 설치된 플러그인 코드로서 신뢰됩니다.

## [1.15.0] — 2026-09-22

### Added

- Grok 4.6은 디스패치되지 않는 레지스트리 행으로 남아 이력 입력(`--prior-models`, `--unavailable-models`, `--host-model`)으로 계속 유효합니다. 업그레이드 이전의 실패 이력을 들고 있는 컨트롤 루프가 그대로 동작하며, 좌석에는 앉지 않습니다.

### Changed

- 균형 워커 좌석을 xAI의 새 기본 모델인 Grok 4.7에 바인딩했습니다. 모델 수용 여부·effort 상한·리뷰어 및 maker 좌석 레시피는 새 모델에서 실측했고, 공표 가격·200K 전체 요청 과금 구간·500K 컨텍스트 윈도우는 문서를 다시 읽어 동일함을 확인했습니다. 라우팅·바인딩·`capability_tier`는 그대로입니다.
- 균형 좌석의 품질 근거를 현재 값이 아닌 승계된 값으로 기록했습니다. 보유 중인 헤드투헤드 점수는 Grok 4.6에서 측정한 값이고 재측정하지 않았으므로, 이 좌석의 tier-1 적합성은 이제 승계된 가정이며 모델 프로파일과 검증 원장이 이를 명시합니다.
- xAI 요율에 날짜가 붙은 출처를 기록했습니다. xAI가 캐시 쓰기 요율을 공표하지 않고 임의의 값을 넣으면 없는 사실을 증거로 기재하는 셈이므로, 이 좌석의 참조 비용 견적은 여전히 unavailable로 보고됩니다.

### Fixed

- Grok sandbox 증명을 CLI가 기록하는 모든 위치에서 판독합니다. Grok 1.0.40이 그 로그의 위치를 옮기면서 sandbox가 실제로 적용됐는데도 쓰기 좌석 디스패치가 미증명으로 fail-closed 판정돼 좌석을 쓸 수 없었습니다. 이제 감독자가 알려진 모든 위치를 예약·판독하고, 출하되는 maker 레시피는 판독하는 모든 위치에 쓰기를 차단합니다.
- 감독자가 예약하지 않은 sandbox 증명을 거부합니다. 이전에는 링크 수만 검사했기 때문에 예약된 로그를 자식이 만든 파일로 바꿔치면 진짜 기록으로 채점됐습니다. 이제 예약 파일을 열어 둔 채 inode로 고정합니다. 예약한 위치가 사라진 경우, 예약된 inode가 아닌 경우, 두 위치가 서로 어긋나는 경우를 각각 별도의 사유로 거부합니다.
- maker 좌석의 작업 디렉터리 안에 심볼릭 링크가 있으면 하드 링크와 마찬가지로 실행 전에 거부합니다. 경로 기반 쓰기 규칙은 두 번째 이름과 원본 파일을 구별하지 못하며, 그것이 이 검사가 존재하는 이유입니다.

## [1.14.0] — 2026-09-07

- 실제 모델 호출·고정 판정 기준·출처가 연결된 토큰/시간 측정을 추가하고 Sol 프로모션 가격과 캐시 쓰기 요금을 갱신했습니다. 24회 진단 결과를 공개하며 기본 라우팅은 유지합니다.

- 커널의 실제 경로와 상위 디렉터리·하드 링크 보호를 사용하는 Darwin 영수증 보호 옵션을 추가했습니다. 실행 전 차단 실험과 보호 정책에 연결된 증거 검증을 제공합니다.

- 완료 영수증 기록을 직렬화하고 신호 전송 전에 취소 의도를 저장합니다. 기록 실패 시 claim을 유지하고 종료 코드8을 반환하며, 모델 역량 실패와 구분합니다.

- 리뷰 깊이·독립 좌석·판정자가 부족하면 대체 모델을 공동 배정합니다. 작성자가 명시된 기존 산출물 리뷰는 리드 리뷰어를 한 번만 세고 `dispatch_seats` 실행 목록을 제공합니다.

### Added

- 영수증 기반 observation 검사에서 결과·역할·모델·effort 주장을 대조하고, 단일 근거로 확인된 제공 모델 식별자를 허용한다.

- 유형별 시도 이력으로 능력 실패 승격과 운영 오류 복구를 분리하고, 모든 시도를 재시도 한도에 포함한다.

- 호스트 모델 선언과 별개로 기존 산출물의 작성자를 리뷰 후보에서 제외하고, 대상·작성자 정보를 fingerprint에 연결한다.

- GPT-6 Astra를 최상위 추론·OpenAI 전용 상위 역할·아키텍트 대체 후보에 추가하고, Sol은 시니어 작업과 대체 후보로 유지한다.
- 추론을 끌 수 없는 모델이 `MINIMAL` 요청에 `low`를 받도록 모델별 네이티브 effort 매핑을 지원한다.

### Fixed

- 정상 null·CSV 입력은 유지하면서, boolean·가용성 값을 강제 변환하지 않고 모호하거나 잘못된 라우팅 JSON을 거부한다.
- observation JSON 값·boolean·enum·달력 시각을 엄격히 검사하고, FIFO 입력·참조를 대기 없이 거부한다.

- 디스크의 성공 표시가 감독기의 실제 실패를 덮지 못하게 하고, 상태 조회·취소·리뷰 증거 검증에서 미완성·미발행 성공을 거부한다.
- 기존 출력 경로를 덮어쓰지 않고 거부하며, 일반 출력과 영수증 읽기를 제한해 FIFO에서 멈추지 않게 한다.
- 명시된 최종 리뷰 구간을 읽고 상충·인용 판정을 거부하며, Claude 오류 필드에 boolean 타입을 요구한다.

## [1.13.0] — 2026-09-03 (2축 라우팅)

### Added

- 실행 난이도 축: `execution_score`(3×complexity + 2×uncertainty + `unfamiliar_codebase` +
  `tool_heavy` + `cross_service_change`)와 `execution_band`(EASY / NORMAL / HARD / VERY_HARD)를
  위험 점수와 함께 계산해 terminal 라우트를 포함한 모든 라우트에 낸다.
- `execution_selection` — class × 실행 밴드 워커 표. 워커는 그 셀과 class × 위험 밴드 표가
  골랐을 워커 중 resolved capability tier가 강한 쪽이며, 위험 밴드 선택은 floor로 남고, 실행
  셀을 앉히면 리뷰·통제 계약이 나빠지는 경우 실행 셀이 양보한다.
- effort floor `execution_HARD: HIGH`, `execution_VERY_HARD: VERY_HIGH`.
- `router.bands`, `router.score_weights`, `execution.bands`, `execution.score_weights`를 로드 시
  형태·연속성 검증.

### Changed

- 기술적으로 어렵지만 고립된 작업은 리뷰 깊이를 건드리지 않고 워커만 올린다. 리뷰 깊이는
  여전히 위험 밴드만 따른다. 어떤 라우트도 1.12.1보다 약한 워커를 받지 않으며, **1.12.1이
  이미 라우팅하던 라우트**는 없던 terminal이나 사람 통제를 새로 얻지 않는다. 1.12.1이 라우팅
  하지 *못하던* 라우트는 라우팅 가능해질 수 있다 — 더 강한 워커가 `local_policy`의 tier floor를
  만족하거나, `INDEPENDENCE_UNAVAILABLE`이 막던 독립 리뷰어 쌍을 확보할 때. 그런 라우트는
  새로 제약된 것이 아니라 새로 실행 가능해진 것이며, terminal 라우트가 아예 보고하지 않던
  통제를 실을 수 있다.
- fast tier에서 워커가 올라간 MEDIUM 라우트의 단일 리뷰어는 기존 cross-family 규칙이 그
  강한 워커에 대해 고른다.
- `unfamiliar_codebase`와 `tool_heavy`를 실행 축이 소비한다 — 이전에는 받아서 무시했다.
  `cross_service_change`는 이미 REFACTORING의 `multi_system_refactoring` effort를 골랐고, 이제
  모든 class에서 실행 난이도에도 기여한다.
- 주석 달린 라우트 인벤토리가 `SKILL.md`에서 `references/control-loop.md`로 이동해 그 파일이
  유일한 소유자가 됐다. Codex 플러그인 설명이 두 축을 명시한다.

## [1.12.1] — 2026-09-03 (모호하지 않은 인용)

### Fixed

- D-14 원장 항목이 acceptance receipt가 있는 디렉토리까지 적는다. 같은 attempt id를
  가진 receipt가 둘이고 결과가 반대인데, 실패본이 더 얕은 경로에 있어 id만 쫓는
  독자가 그것을 먼저 만났다.

## [1.12.0] — 2026-09-02 (grok 호스트의 Claude 리뷰어 좌석)

### Added

- `transports.grok.to_claude.mechanism_reviewer`: `--permission-mode plan`,
  `--allowedTools Read,Glob,Grep,LS`, `--strict-mcp-config`를 쓰는 별도 검증된
  읽기 전용 Claude 리뷰어 레시피를 추가했다. 범용 mechanism은 변경하지 않고
  쓰기 가능 좌석으로 유지한다.

### Changed

- 헤드리스 grok 호스트가 bare echo의 자동 허용을 증거로 오인하지 않고 감독기를
  띄우는 방법과, 바깥 호스트의 sandbox가 Claude 자식의 키체인을 가릴 수 있다는
  경계를 디스패치 계약에 기록했다.
- Claude 브리지의 부팅 절감폭이 환경 의존적임을 명시했다. 전역 MCP 커넥터가 실제
  handshake한 환경에서는 strict 플래그가 약 80%를 제거했지만, 그 스키마가 없던
  과거 grok 호스트 프로브에서는 약 10%만 절감됐다. 그 과거의 ~10% 결과는
  재현되지 않았다 — grok 호스트에서 다시 프로브했을 때는 80% 쪽이 나왔고,
  원래 원인은 아직 규명되지 않았다.

### Fixed

- Claude transport의 원장 결속과 문서 fence를 방향·좌석별로 분리했다. 이제 범용
  mechanism 행이 argv가 다른 reviewer mechanism을 조용히 보증할 수 없다.

## [1.11.1] — 2026-09-02 (산출물 핀을 유지하는 이유)

### Changed

- 산출물 identity 핀이 하드 링크가 필요 없는 두 번째 세탁 시퀀스(루트 밖에 쓰고
  rename으로 들여오기)도 거부한다는 사실과, 자식 트리에 대한 어떤 사전 감사도
  그 핀을 대신할 수 없다는 사실을 디스패치 계약에 기록했다. 내용 기반 인증
  모드를 설계·구현·리뷰까지 하고 철회한 이유는 검증 원장에 남겼다. 따라서 상시
  지침은 그대로다 — Claude 좌석의 산출물은 receipt 옆에 기록한 호출자 측 내용
  해시로 인증하고, 그런 좌석에 `--require-artifact`를 선언하지 않는다.

## [1.11.0] — 2026-09-02 (Claude envelope, 엄격해진 verdict)

### Added

- `--output-envelope claude-print-json-v1`이 `claude -p --output-format json`
  문서를 grok 포맷과 같은 게이트로 판정한다. 키 이름은 그 포맷 자신의 것을 읽고,
  `type`·`subtype`·`is_error` 판별자가 모두 "턴이 끝났다"고 말해야 한다.
- receipt의 envelope가 자식의 토큰 회계(`usage`)를 싣는다. 컨텍스트·부팅 비용을
  재는 호출자는 보존된 stdout을 긁는 대신 receipt를 읽으면 된다. 두 포맷 모두
  수치를 싣고, 유한하고 음이 아닌 수만 남긴다.
- receipt가 파싱한 `verdict`와 그것이 `verdict_recovered`인지를 기록한다.
  `verify-evidence`는 복구된 receipt에 대해 알림을 낸다.

### Fixed

- 리뷰 프롬프트가 인용하는 형식 `verdict: PASS | PASS_WITH_CHANGES | FAIL`을 더
  이상 verdict로 받지 않는다. 행두에 있어도 마찬가지다 — 지금까지는 받아들였다.
  지시문만 되뇌고 아무것도 리뷰하지 않은 좌석은 이제 리뷰한 것으로 판정되지
  않는다.
- 헤드리스 포맷이 앞선 서술과 붙여 버린 verdict를 더 이상 "verdict 없음"으로
  판정하지 않는다. envelope 경로와 일반 stdout 경로 모두에 적용된다. 마지막 비앵커
  verdict는 스키마의 두 번째 필드가 바로 다음 줄에 범위 안의 값으로 올 때만
  인정된다.
- `.to_xai` 디스패치는 다른 벤더의 envelope 포맷을 선언할 수 없으며,
  `verify-evidence`는 그렇게 만들어진 receipt를 거부한다.

## [1.10.1] — 2026-09-02 (레지스트리 id 출처 기록)

### Changed

- `claude-haiku-4-5-20251001`과 `claude-haiku-4-5`가 서로 다른 모델을 서빙한다는
  사실을 검증 원장에 기록했다. 따라서 레지스트리는 날짜 접미를 떼지 않고 현행
  핀을 유지한다.

## [1.10.0] — 2026-09-02 (정직한 승격 기록, 원장에 묶인 쓰기 좌석)

### Fixed

- 낮은 라우팅 신뢰도로 리뷰 밴드가 승격된 라우트가 더 이상 스스로를 반증하지
  않는다. 승격은 리뷰어를 다시 앉히고, 그 과정에서 승격을 촉발한 fallback이
  사라질 수 있다. 이제 라우트는 승격이 승격 이전 계획으로 결정됐고 보고되는
  신뢰도는 승격 이후 계획의 것이라는 사실을 두 수치와 함께 note로 싣는다.
  밴드는 그대로 유지된다.

### Added

- 메이커 좌석 없이 `write_verified: true`를 선언하는 방향은 그 방향을 이름으로
  지목하는 verified 원장 항목이 있어야 하며, 없으면 라우터가 설정 로드를
  거부한다. 네 방향이 비어 있지 않은 레시피 문자열만으로 쓰기 디스패치를
  승인하고 있었다.
- Claude 좌석의 산출물은 `--require-artifact`로 인증할 수 없다는 사실을 디스패치
  계약에 기록했다. Claude 파일 도구는 내용을 새 inode에 설치하는 반면 OpenAI
  좌석은 제자리에서 잘라 쓴다. 그런 좌석은 receipt 옆에 기록한 내용 해시로
  인증한다.

## [1.9.0] — 2026-09-02 (Fable 5.1, 가벼운 Claude 브리지 좌석)

### Changed

- grok 호스트의 Claude 브리지 좌석이 — 리뷰어와 워커 모두 — 사용자 전역 MCP
  서버를 더 이상 싣지 않는다(`--strict-mcp-config`). 필요한 호출자는
  `--mcp-config <file>`을 `-p` 바로 뒤에 붙인다. 같은 레시피는 grok 아래
  중첩된 `codex exec` 턴에서 프로브됐으며, 대화형 Codex 세션에서 프로브된
  것은 아니다.
- `principal_architect`가 Claude Fable 5.1이다. Claude Fable 5는 이력
  입력(`--prior-models`, `--unavailable-models`, `--host-model`)으로는 계속
  유효하며 어떤 좌석에도 앉지 않는다.
- 단일 fallback만으로는 리뷰 밴드가 올라가지 않는다. 이전·이후 페널티가
  둘 다 fallback을 기록하면 `routing_confidence`가 0.04 높아진다.

### Added

- 선언된 호스트 모델이 레지스트리에 없는 라우트는 레지스트리가 낡았을 수
  있다는 note를 싣는다.

## [1.8.0] — 2026-09-01 (grok 메이커 좌석)

### Changed

- Claude Code 쓰기 라우트가 다시 `xai_frontier`를 앉힌다. 스킵 노트 대신
  디스패치가 `--seat-profile grok-maker-v1`을 써야 한다는 고지가 남는다.
  Codex 호스트의 쓰기 라우트는 여전히 xai를 건너뛴다.

### Added

- **Claude Code → xAI 쓰기 가능 메이커 좌석**, 감독기 측 예방 하에 출하.
  `transports.claude_code.to_xai.mechanism_maker`와 `write_verified: true`,
  원장 `verified`가 함께 맞다. argv만으로는 봉쇄가 아니다: 경로 기반
  샌드박스는 여전히 하드 링크와 원본을 구분하지 못한다. 디스패치는
  `--seat-profile grok-maker-v1`을 써야 한다.
- `dispatch_agent.py`: `--child-cwd` / `--require-single-linked-cwd`
  (정규 파일 `st_nlink > 1`이면 기동 거부); `--grok-home` /
  `--grok-auth-seed` (시도별 홈, auth는 새 inode로 복사);
  `--expect-sandbox-enforced` (`ProfileApplied.enforced` 채점);
  `--seat-profile grok-maker-v1` (위 전부 + 봉투·세션 증거, 아니면
  스폰 전 거부).
- 같은 메이커 argv를 `codex.to_xai`에도 거울로 싣되, 그 방향은
  `verified: false` / `write_verified: false`로 둔다.

### Security

- grok argv 수준에서 하드 링크 이탈은 그대로다: cwd 안 별칭이 바깥
  inode를 덮어쓰며, 바깥 경로를 deny한 커스텀 프로파일에서도 그렇다
  (grok 1.0.13 / Darwin arm64). 출하 주장은 감독기 예방 + `ln`이 없는
  도구 화이트리스트 + 시도별 `GROK_HOME` (workspace 쓰기 허가는
  `$GROK_HOME`을 따르고 `~/.grok`가 아니다) + fail-closed 커스텀
  프로파일 + 이벤트 로그 Write/Edit deny다.

## [1.7.0] — 2026-09-01 (쓰기 좌석 라우팅)

### Changed

- **워커가 쓰기를 해야 하는 라우트는, 이 호스트에 write 가능한 레시피가 없는
  모델로 더 이상 채워지지 않는다.** 라우터가 `transports` 표를 읽는다: 읽기
  전용 좌석만 싣는 방향은 쓰기 좌석을 채울 수 없다. Claude Code와 Codex
  호스트에서 xai 워커가 쓰기 작업에서 빠지며, 이는 호출자가 그동안
  `--unavailable-models`로 직접 하던 일이다 — 이 용도로는 그 플래그를 더 쓰지
  마라. 리뷰 좌석에서까지 모델을 빼앗는다. 호스트 자신의 패밀리(네이티브
  좌석은 레시피가 필요 없다), 읽기 전용 좌석, `read_only`로 선언된 라우트는
  아무것도 바뀌지 않는다.
- 방향 복구에는 세 가지가 함께 필요하다: maker 레시피, 그 방향의
  `write_verified: true`, 그리고 프로브를 기록한 검증 원장 항목. 이 결합은
  특정 공급자에 대한 규칙이 아니라 조회지만, 프로브 없이 레시피만 추가하면
  아무것도 승인되지 않는다.

### Added

- `task_write_seat`: 라우트의 워커가 쓰는지에 대한 클래스별 기본값.
  fail-closed — 워커 산출물이 산출물이 아니라 판단인 두 클래스(`REVIEW`,
  `INVESTIGATION`)만 `read_only`다.
- `--worker-seat write|read_only`와 RouteRequestV1 `worker_seat`가 그 기본값을
  라우트 단위로, 양방향으로 덮는다. 선언하지 않은 좌석은 이 필드가 없던
  때와 정확히 같은 해시를 낸다.
- 모든 라우트의 `worker_seat` 블록: 적용된 kind, 출처, 이 호스트가 쓰기 작업을
  보낼 수 있는 family. 요구사항이 좌석을 옮겼을 때는 id 없는 노트가 남는다 —
  `fallbacks_applied`가 아니라 `notes`에, 그것이 바인딩 결정이기 때문이다.
  따라서 routing confidence를 소모하지도, 리뷰 밴드를 올리지도 않는다.
- 전송 방향별 `write_verified`: 교차 패밀리 쓰기 디스패치의 **유일한** 권한
  근거다. `verified`는 방향을 증명할 뿐이며 — `to_xai`에서는 리뷰어 좌석을
  증명하고 원장은 maker 좌석을 미출하로 적는다 — 그 자체로 쓰기 권한이었던
  적이 없다. Policy는 write 가능한 레시피가 없는 `write_verified: true`,
  검증 원장과 모순되는 값, 그리고 브리지 패밀리를 네이티브처럼 보이게 하는
  `degraded_binding`을 모두 거부한다.

### Security

- grok 메이커 좌석은 미출하 유지 — 이제 1.0.5가 아니라 실측된 1.0.13 증거에
  근거한다. 2026-09-01 grok 1.0.13 / darwin에서 동일한 출하 후보 argv로
  재프로브: 작업 디렉토리 안의 하드 링크가 여전히 외부 inode를 덮어쓰며,
  `--sandbox strict`에서도 그렇다 — 경로 기반 샌드박스는 하드 링크와 그것이
  가리키는 파일을 구분할 수 없고, 사용된 경로가 실제로 워크스페이스 안이므로
  커널은 위반을 기록조차 하지 않는다. 두 가지 봉쇄 사실이 추가로 기록됐다:
  `~/.grok`는 두 프로파일 모두에서 쓰기 가능하며(config·sandbox·신뢰 폴더
  상태 포함, hooks 경로만 보호되고 그것도 커널 거부가 아니라 취소된 턴으로
  나타난다), 세션 요약은 요청된 샌드박스 프로파일을 적을 뿐 그것이 적용됐다는
  증거는 담지 않는다.

## [1.6.0] — 2026-09-01

### 추가됨

- 선택적 `host_seat` 선언(`--host-model` / `--host-effort`, RouteRequestV1
  `host_seat`) — 라우터가 호스트 세션을 정책이 요구하는 오케스트레이터
  프로파일과 비교해 보고한다.
- 모든 route의 `host_seat_advisory` 블록: 오케스트레이터 요구
  (tier/effort/raised_by)와 선언된 호스트 비교 결과, `upgrade_recommended`
  권고, 호스트가 요구보다 낮을 때 id 없는 안내. route 자체는 바뀌지 않는다.

### 변경됨

- `router.default_orchestrator` / `default_orchestrator_effort`를 이제
  라우터가 읽고 검증한다(이전에는 호출자 안내만 담당).

## [1.5.1] — 2026-08-29 (grok 호스트 브리지 검증)

### 변경됨

- `grok.to_claude`와 `grok.to_openai`가 Darwin grok 호스트에서 검증되었다:
  단순 왕복, 리뷰어 좌석 모델 주소 지정, 그리고 디스패치 감독 아래 동시에
  완료된 두 개의 read-only 리뷰어 좌석이 스키마-유효 판정을 냈고, 서로 다른
  영수증 attempt id가 격리 증거다. 검증 원장은 호스트 플랫폼, CLI 버전,
  날짜를 기록한다. 이 브리지를 쓰는 grok 호스트 이중 리뷰는 더 이상 가정
  위에 서 있지 않다.
- Grok 네이티브 서브에이전트 격리는 검증되지 않은 채로 남고, 이번 프로브가
  의도적으로 건드리지 않는다: grok 네이티브 이중 리뷰는 두 리뷰어가 별도
  프로세스로 돌지 않는 한 여전히 저하된 상태다.

## [1.5.0] — 2026-08-25 (grok 좌석 무결성)

### 추가됨

- 디스패치가 grok stdout 봉투를 선언할 수 있다(`--output-envelope grok-headless-json-v1`): 턴의 `stopReason`을 채점해 `end_turn`만 성공이 될 수 있다 — 취소된 턴은 exit 0으로 끝나므로 이전에는 SUCCEEDED로 기록될 수 있었다.
- 디스패치가 좌석이 생산해야 할 파일을 요구할 수 있다(`--require-artifact`, `--artifact-root`, `--require-artifact-sha256`, `--require-artifact-allow-unchanged`): SUCCEEDED 이전에 존재·격리·해시를 증명하고, 실행 전에 캡처한 기준선과 대조해 그 파일이 잔존물이 아니라 이번 시도의 산물임을 증명한다. 요구 산출물은 자기 아이노드의 유일한 이름이어야 하며, 기준선을 읽지 못한 경우는 `ENOENT`가 확인될 때에만 부재로 간주한다 — 그 외의 판독 오류는 시도가 시작되기 전에 거부된다.
- 디스패치가 grok 세션 디렉토리를 읽을 수 있다(`--session-evidence`, `--session-id`): 영수증이 요청값과 나란히 유효 에이전트·샌드박스 프로파일·서빙 모델을 기록하며, 세션 id와 실행 시각으로 그 시도에 결박된다. FAILED·TIMED_OUT·TERMINATION_UNCONFIRMED 영수증도 같은 증거를 싣는다 — 기록하되 채점하지 않으므로, 감사 흔적이 가장 필요한 결말에서 그 흔적이 사라지지 않는다.
- `--expect-effective-agent`와 `--expect-sandbox-profile`은 유효 에이전트나 샌드박스가 좌석이 요청한 값과 다르면 성공을 거부한다 — 쓰기 좌석이 read-only 기본값을 조용히 상속할 수 없다.
- 영수증에 `result.invalid_reasons`가 실려 시도가 거부된 이유를 이름으로 남긴다 — 취소된 턴과 판독 불가한 증거를, 작업에 실패한 모델과 구별할 수 있다.
- `transports.*.to_xai`가 터미널·MCP 메타도구·웹 검색을 도구 표면에서 제외한, 검증된 read-only grok 리뷰어 좌석 레시피를 싣는다.

### 변경됨

- `verify-evidence`가 봉투나 세션 증거가 없는 `to_xai` 영수증과, 봉투가 `end_turn`으로 끝나지 않은 영수증을 거부한다. 1.4.x가 쓴 영수증도 해당된다: 구 증거 세트는 그것을 생산한 버전으로 검증하라.
- `--transport-id`가 `.to_xai`로 끝나는 디스패치는 봉투와 세션 증거를 선언해야 하며, 절반만 선언하면 시도가 시작되기 전에 거부된다.
- 채점이 성공을 기록하기 직전에 마감을 재검사하며, 산출물 해시는 마감을 넘기지 않고 중단된다.

### 보안

- 증거 파일은 심볼릭 링크를 따라가지 않고 FIFO에서 블록되지 않게 열리며, 산출물 격리 루트는 실행 전에 고정된다 — 감독 대상 자식이 감독기의 판독이나 자신의 작업 증명을 그 루트 밖으로 돌릴 수 없다.
- 하드 링크가 둘 이상인 요구 산출물은 거부된다 — 실행 전에는 사용 오류로, 채점 시에는 해시를 기록하지 않는 `artifact_multiply_linked`로. 격리는 경로를 막지만 쓰기는 아이노드에 닿는다: 루트 밖 파일을 가리키는 루트 안의 두 번째 이름은, 그러지 않으면 영수증이 격리되었다고 부른 경로를 통해 덮어써진다.
- 요구 산출물은 실행 전에 고정된 그 아이노드여야 한다. 링크 수를 실행 전과 채점 시 두 번만 표본으로 삼으면 그 사이의 시도 전체가 감시되지 않는다: 자식은 루트 밖 파일을 요구 경로에 하드 링크하고, 그것을 통해 써서 원본을 덮어쓰고, 그 이름을 지운 뒤 링크가 하나뿐인 새 파일을 남길 수 있으며, 두 표본 모두 `1`을 읽어 `contained: true` / `changed: true` SUCCEEDED가 나온다. 이제 모든 요구 경로는 실행 전에 하나의 `(st_dev, st_ino)`에 묶인다 — 부재한 경로는 감독자가 만들고 자식이 쓰지 않으면 철회하는 예약으로 — 시도 동안 열린 채 유지되며 채점 시 다시 확인되어, 어긋나면 `artifact_identity_replaced`가 된다. 따라서 요구 산출물은 **제자리에서** 써야 한다: `rename`/`os.replace`로 덮어쓰면 새 아이노드가 설치되므로 거부된다. 생산되지 않은 요구 산출물은 여전히 `artifact_missing`이며, 감독자는 여전히 제약 없는 자식이 루트 밖에 쓰는 것을 막지 못한다 — 다만 그것을 증명해 주지 않을 뿐이다.
- 세션 증거 디렉토리를 읽다가 시도의 결과가 바뀌는 일은 이제 없다. `events.jsonl`의 open 이후 판독 오류나 디코드 오류가 아닌 `json` 예외는 최선 노력 종단 수집을 빠져나가 크래시 핸들러에 도달했고, 이미 결정된 FAILED 또는 TIMED_OUT 영수증을 CANCELLED / TERMINATION_UNCONFIRMED로 다시 이름 붙이고 9로 종료했다. 두 판독은 이제 각각 보호되며 언제나 같은 널 형태를 낸다. 새 `session_evidence.unreadable` 플래그가 "읽지 못함"과 "쓰인 적 없음"을 구분한다.
- 감독기 자신의 파일 I/O가 산출물 증거를 만들어내거나 지우는 일은 이제 없다. 실행 전 예약(reservation)의 `write`가 성공했지만 짧게 쓰였을 때에도 본문 전체가 쓰인 것처럼 기록되어, 아무것도 생산하지 않은 자식이 잘린 표식을 두고 채점되고 그에 대해 성공을 인증받을 수 있었다. 예약은 이제 완전 기록 루프로 쓰이며, 본문 전체에 못 미치면 실행 전에 실패한다(exit 2, 영수증·클레임·잔존 파일 없음). 예약을 철회하다 오류가 나면 해제 루프가 중단되어 현재와 이후의 디스크립터가 누출되고, 이미 기록된 FAILED 또는 TIMED_OUT 영수증 위로 9가 반환되었다. 해제는 이제 항목 단위이며 각 핀을 자신의 `finally`에서 닫고, 기록된 결과를 바꿀 수 없다. 또 `unlink` 실패가 무시된 채 기록이 `exists: false`로 다시 쓰여, 디스크에 남아 있는 파일을 영수증이 부인했다. 기록은 이제 unlink 성공을 따르며, 남은 파일은 그대로인 `artifact_missing:<path>` 옆의 새 `artifact_reservation_cleanup_failed:<path>` 사유로 이름 지어진다.

### 제거됨

- 단일 `transports.*.to_xai.mechanism` 문자열이 좌석별 레시피로 대체됐다. 이번 릴리스에는 쓰기 가능 grok 좌석 레시피를 싣지 않는다: grok 1.0.5 실측에서 작업 디렉토리 안의 하드 링크가 경로 범위 쓰기 규칙과 `--sandbox workspace`를 모두 무력화해, 그 링크를 통한 쓰기가 디렉토리 밖 파일에 도달한다. 이 문제가 닫힐 때까지 쓰기 가능 작업은 xai 워커를 우회해 라우팅하라.

## [1.4.0] — 2026-08-20 (RouteObservationV1)

### 추가됨

- RouteObservationV1 레코드 검증기: 관측 스키마를 검사하고, 원문 산출물을 복사하지 않은 채 참조 파일과 dispatch receipt를 확인할 수 있다.

## [1.3.0] — 2026-08-20 (latency-sensitive 바인딩)

### 추가됨

- `latency_sensitive`가 `large_context`와 같은 바인딩을 결정한다: 이 플래그가 붙은 작업은 `worker_balanced`를 Claude balanced 좌석으로 보낸다. 2026-08-20 B.1 반복 측정에서 품질은 15/18 동률이었고 모든 태스크 유형에서 sonnet 중앙값 지연이 더 낮았다. 기본 바인딩과 capability_tier는 변경 없음.

## [1.2.1] — 2026-08-20 (balanced-seat 반복 측정)

### 변경됨

- worker_balanced 쌍(grok-4.6 vs sonnet-5)을 6개 멀티파일 태스크 × 3회 반복으로 재측정했다: 품질은 각각 15/18로 여전히 동률이고, 모든 태스크 유형에서 sonnet의 중앙값 지연이 더 낮아 latency-sensitive 라우팅 규칙은 이제 설계 후보가 됐다 — 구현하지 않았으며, 180K/230K 후속 셀(태스크 2개 × 1회 × 2모델)도 품질 동률이었다. 바인딩과 capability_tier는 변경 없음.

## [1.2.0] — 2026-08-19 (증거 연결)

### 추가됨

- 모든 route가 `request_sha256`과 결정적 `decision_fingerprint`를 방출한다. dispatch receipt가 이 값을 실을 수 있고(`--decision-fingerprint`, `--policy-sha256`, `--transport-id`, `--host-cli-version`), `verify-evidence`가 `--expect-fingerprint` / `--expect-models`로 대조한다.
- Receipt가 요청된 모델과 실제 서빙된 모델의 구분을 명시한다: `observed_model_id` / `observed_model_source` 자리(정직한 기본값 — 아직 어떤 transport도 서빙된 모델을 관측할 수 없다).
- `routing_confidence_kind`가 confidence 값이 보정된 확률이 아니라 heuristic gate 점수임을 라벨로 밝힌다.
- 레지스트리가 OpenAI GPT-5.6의 272K 장문맥 구간을 명시적 경계 어휘와 함께 기록하고, 모든 좌석의 표준 cached-input 요율과, 가격 경계 및 Fable 5의 공급자 측 모델 대체에 대한 원장 항목을 남긴다.
- 서빙 모델 caveat가 선언된 모델이 좌석에 앉은 route는 대체 가능성을 notes에 공시한다.

### 수정됨

- `policy_sha256`이 실제로 사용된 정책을 해시한다. 라이브러리 API로 주입한 config가 더 이상 디스크의 해시로 보고되지 않고, route 사이에 그 자리에서 변경된 config도 캐시가 아니라 새 내용으로 다시 읽힌다 — 하나의 fingerprint는 하나의 결정을 가리킨다.
- 모델 프로파일이 더 이상 워커 바인딩의 품질을 미측정으로 서술하지 않는다. effort 표에서 정책이 삭제한 작업 유형이 사라졌고, xAI 장문맥 경계는 "200K 이상"으로 읽힌다.

## [1.1.1] — 2026-08-18 (바인딩 품질 측정)

### 변경됨

- worker_fast·worker_balanced 바인딩의 품질을 가정이 아닌 측정으로 확인했다: 변별력 있는 446-노드 숨긴 테스트 헤드투헤드에서 haiku가 luna를 근소하게 앞섰고, grok-4.6 대 sonnet-5는 천장에서 동률이었다. 두 바인딩과 모든 `capability_tier`는 변경 없음 — 여전히 검증된 가격 우위에 근거하며, 검증 원장(verification ledger)에 점수·실패 유형·지연 시간이 기록됐다.

## [1.1.0] — 2026-08-18 (설계·가격 감사)

### 추가됨

- `large_context`가 바인딩을 결정한다: 이 플래그가 붙은 작업은 `worker_balanced`를 Claude balanced 좌석으로 보낸다. xAI 좌석은 입력 200K 토큰을 넘기면 요청 전체를 두 배 요율로 청구하고, 컨텍스트 창도 500K 더 일찍 끝나기 때문이다.
- 레지스트리가 xAI의 long-context 가격 구간과 컨텍스트 창을 기록하고, 모델 프로파일도 이 비교가 조건부라는 사실을 그대로 적는다.

### 수정됨

- `local_policy`의 키 이름뿐 아니라 값도 검증한다. 알 수 없는 effort 값은 적용된 하한으로 보고되면서 실제로는 조용히 무시됐고, 숫자가 아닌 tier는 내부 오류용 상태로 크래시했다. 둘 다 이제 잘못된 입력(exit 2)이다.
- CLI locator가 문서화된 순서(env → root → Claude 캐시 → Codex 캐시)대로 해석한다. 알파벳 순서 때문에 `.codex`가 이기던 문제, 문자열 정렬로 `1.9.0`이 `1.10.0`을 이기던 문제, 소스 체크아웃 거부가 첫 단계에서만 걸리던 문제를 함께 고쳤다.
- 알 수 없는 attempt id에 대한 `dispatch_agent.py status`가 traceback 대신 `cancel`과 같은 한 줄 메시지와 exit 2로 답한다.
- 정책 파일과 어긋나 있던 문서: REVIEW/CRITICAL 워커, 빠져 있던 `concurrency_sensitive` 오버라이드, 빠져 있던 `termination_unconfirmed` 운영 플래그.
- README가 PyYAML 요구 사항과, human-gate exit status가 3..255 범위에서 설정 가능하다는 사실을 명시한다.

### 제거됨

- `review.MEDIUM.reviewer_count`와 `review.MEDIUM.prefer_cross_family`: 둘 다 읽히기만 하고 아무 효과가 없었다. 이들이 서술하던 동작은 그대로이며, 처음부터 상수였다는 사실을 문서에 적었다.
- 아무것도 선택하지 않던 `effort_by_work` 항목 3개와, 쓰이지 않던 `implementation_role` 작업 필드.

## [1.0.1] — 2026-08-17

### 수정됨

- Claude Sonnet 5 정식 가격이 $2 / $10으로 확정된 뒤, 레지스트리에 남아 있던 $3 / $15 수치를 고쳤다.

## [1.0.0] — 2026-08-17

### 추가됨

- Claude Code, Codex, Grok를 위한 공유 결정 평면의 첫 공개 릴리스.
- 스킬 기반 분류와 RouteDecisionV1(밴드, 워커, effort, 리뷰 정책, 정직한 독립성)을 방출하는 결정적 채점기.
- RouteRequestV1 파일 입력, local-policy 병합, HIGH/CRITICAL 하한을 내리지 않는 가용성 인식 폴백.
- wall-clock deadline, 프로세스 그룹 kill ladder, 격리 증거와 구분되는 완료 receipt를 가진 백그라운드 dispatch supervisor.
- 형제 소스 트리와 개인 스킬 심링크를 거부하는 호스트 중립 CLI locator.
- 나머지 deep-suite와 같은 공개 플러그인 문서: 한/영 README와 CHANGELOG, CONTRIBUTING, SECURITY, LICENSE.
