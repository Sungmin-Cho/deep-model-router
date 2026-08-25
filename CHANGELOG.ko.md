[English](./CHANGELOG.md) | **한국어**

# 변경 이력

이 프로젝트의 모든 주요 변경 사항은 이 파일에 기록됩니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 따르며,
이 프로젝트는 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)을 준수합니다.

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
