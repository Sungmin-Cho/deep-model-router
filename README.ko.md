[English](./README.md) | **한국어**

# deep-model-router

![version](https://img.shields.io/github/package-json/v/Sungmin-Cho/deep-model-router?label=version)
![license](https://img.shields.io/github/license/Sungmin-Cho/deep-model-router)
[![part of deep-suite](https://img.shields.io/badge/part%20of-deep--suite-5b8def)](https://github.com/Sungmin-Cho/deep-suite)

Claude Code, Codex, Grok를 위한 결정적 모델 / effort / 리뷰 라우터.

위임할 소프트웨어 엔지니어링 작업을 분류하면, 채점기가 파일 수·토큰 수·지금 열려 있는 모델이 아니라 워커는 *난이도*로, 리뷰 깊이는 *리스크*로 고릅니다(reasoning effort의 하한은 두 축이 함께 정합니다). 리뷰 깊이는 리스크 밴드만의 함수이며, 워커 선택이 몰래 약화시킬 수 없습니다. 어렵지만 고립된 작업은 더 강한 워커를 받고, 쉽지만 민감한 작업은 깊은 리뷰를 유지합니다.

[deep-suite](https://github.com/Sungmin-Cho/deep-suite) 에코시스템의 일원입니다. [deep-work](https://github.com/Sungmin-Cho/deep-work)와 [deep-loop](https://github.com/Sungmin-Cho/deep-loop)가 공유 결정 평면으로 이 플러그인에 의존합니다. 릴리스 이력은 [CHANGELOG](CHANGELOG.md)를 참고하세요.

---

## deep-suite에서의 역할

deep-model-router는 **결정 평면**입니다. 형제 플러그인은 집행, durable state, 각자의 안전 하한을 유지합니다. 이 플러그인은 두 질문을 분리해서 답합니다.

1. **누가 하는가** — 사용 가능한 모델과 effort에 묶인 역할. 리스크 밴드를 하한으로 두고 실행 난이도가 고릅니다. effort 하한은 두 축이 함께 정합니다.
2. **얼마나 엄하게 검사하는가** — 리스크 밴드를 따르는 리뷰 정책(독립 리뷰가 필요한지 포함).

작업을 대신 구현하지 않으며, 실제로 집행하지 않은 통제를 주장하지 않습니다. `independence_required`는 정책이고 `review_independence`는 증거입니다. 라우터가 없으면 로컬 폴백이지, HIGH/CRITICAL 하한을 내릴 이유가 아닙니다.

---

## 설치

### 방법 1 — 마켓플레이스 (deep-suite 등록 완료)

```text
# Claude Code
/plugin marketplace add Sungmin-Cho/deep-suite
/plugin install deep-model-router@claude-deep-suite

# Codex
codex plugin marketplace add Sungmin-Cho/deep-suite
codex plugin add deep-model-router@claude-deep-suite
```

### 방법 2 — 로컬 clone

```text
# Claude Code
claude plugin add https://github.com/Sungmin-Cho/deep-model-router.git

# Codex — Codex 설정에서 로컬 경로를 plugin 디렉터리로 추가
```

채점기와 dispatch supervisor는 Python 3와 **PyYAML**이 필요합니다 — 정책이 YAML 파일이므로, PyYAML이 없는 환경에서는 첫 라우팅부터 실패합니다. 인터프리터에 없다면 `python3 -m pip install pyyaml`로 설치하세요. supervisor는 POSIX 전용입니다(프로세스 그룹 제어). Node 런타임 의존성은 없습니다.

---

## 사용법

### Claude Code

```text
/deep-model-router:model-router
```

### Codex

```text
$deep-model-router:model-router
```

분류 규약은 세션당 한 번 스킬을 로드해 익힙니다. 반복 결정은 CLI로 하고, 밴드를 손으로 다시 계산하지 않습니다.

```text
SKILL_DIR=<스킬 로드 시 안내된 skill-base-directory>
python3 "$SKILL_DIR"/scripts/route_task.py --class IMPLEMENTATION \
    --complexity 1 --uncertainty 1 --blast-radius 1 --reversibility 0 \
    --format json
```

`SKILL_DIR`은 `SKILL.md`가 있는 디렉터리입니다. 백그라운드 서브에이전트는 스킬 루트가 아니라 프로젝트 루트를 상속하므로, 스크립트 경로는 항상 이 접두사로 호출합니다.

RouteRequestV1 파일은 플래그보다 우선합니다.

```text
python3 "$SKILL_DIR"/scripts/route_task.py --request-json ./route-request.json --format json
```

백그라운드 dispatch는 별도 단계입니다. 라우트는 결정이고, `scripts/dispatch_agent.py`가 deadline·kill ladder·완료 receipt를 소유합니다. 세션의 첫 백그라운드 dispatch 전에 `skills/model-router/references/adapters.md`를 읽으세요.

---

## 스킬

| 스킬 | Claude Code | Codex | 목적 |
|---|---|---|---|
| model-router | `/deep-model-router:model-router` | `$deep-model-router:model-router` | 위임 작업을 분류하고 RouteDecisionV1을 방출 |

소비자는 `../deep-model-router`나 개인 `~/.claude/skills/model-router` 심링크를 import하면 안 됩니다. CLI 탐색은 [`docs/locator.md`](docs/locator.md)를 따르세요.

---

## 라우팅 방식

당신이 분류하고, 스크립트가 채점합니다. 싼 모델이 물량을 처리하고, 승격은 증거로만 일어납니다. 리뷰 깊이는 리스크를 따릅니다.

| 당신이 주는 것 | 채점기가 돌려주는 것 |
|---|---|
| 작업 클래스, 0–3 차원 네 개, 플래그 | 리스크 밴드, 워커 역할+모델, effort, 리뷰 정책 |
| 런타임, 가용성, 이전 실패 | 폴백, terminal 상태, human-gate exit code |

```
risk_score = complexity + 2×uncertainty + 2×blast_radius + reversibility     (0–18)
LOW 0–3 · MEDIUM 4–7 · HIGH 8–10 · CRITICAL 11–18

execution_score = 3×complexity + 2×uncertainty + context 플래그 셋            (0–18)
EASY 0–8 · NORMAL 9–11 · HARD 12–14 · VERY_HARD 15–18   → 워커와 effort 하한
                                                        → 리뷰 밴드·사람 통제는 아님
```

critical-domain 플래그(auth, security, financial, data integrity)는 채점 후 모든 작업 클래스에서 밴드를 올립니다. 잘 이해된 작은 인가 경로 수정도 강한 워커와 독립 리뷰를 받습니다.

리뷰는 밴드만큼만 합니다. `LOW` 리뷰는 라우트가 이름 붙인 결정적 검사(`tests`, `lint`)이며, 호출자는 작업을 받아들이기 전에 그것을 통과시켜야 하고, 검사를 돌릴 수 없는 저장소라면 `--checks-unavailable`로 다시 라우팅합니다. `MEDIUM` 리뷰는 밴드 하한과 구현자 tier 둘 다에 닿는 가장 낮은 tier의 리뷰어 한 명을, 가능하면 다른 가족에서 앉힙니다. `REVIEW` 작업의 리드는 리뷰어 수에 포함됩니다. 이미 끝난 작업에는 RouteRequestV1 `implementer`로 실제 구현 모델을 기준으로 리뷰를 계획합니다.

정책은 `skills/model-router/config/model-routing.yaml`에 있습니다. 모델 식별자는 이 레지스트리 또는 이 기계에서 프로브를 통과한 로컬 오버레이 항목(아래)에서만 생기며, 레지스트리로 돌아가는 길은 `model_sync.py promote`뿐입니다. 스킬 본문과 `references/`는 스크립트가 실행하는 규칙과 같습니다.

exit status도 계약입니다. **0** 디스패치 가능, **1** terminal, **2** 잘못된 입력, **3** 먼저 확인 필요, **4** production hotfix(배포 후 확인), **5** 내부 오류. 이 중 3만 설정 가능합니다 — `human_in_the_loop.human_gate_exit_status`이며 3..255 범위의 값을 가질 수 있으므로, 3을 이미 다른 용도로 쓰는 호출자는 게이트 코드를 옮길 수 있습니다. 하드코딩하지 말고 config에서 읽으세요. 0·1·2는 이미 사용 중이고 255를 넘으면 성공 코드로 잘리기 때문에, 이 범위는 로드 시점에 검증합니다.

---

## 모델 자동 업그레이드 / 로컬 오버레이

벤더는 릴리스보다 빨리 새 모델 세대를 내놓습니다. `skills/model-router/scripts/model_sync.py`는 레지스트리 행마다 선언된 계열(lineage)을 이 기계에서 따라갑니다. CLI 모델 카탈로그를 오프라인으로 읽고, 후속 id를 봉쇄된 읽기 전용 프로브로 확인한 뒤, 로컬 오버레이 항목으로 발행합니다. 그러면 라우터는 그 행을 새 id로 라우팅합니다 — tier는 승계, 가격은 `unavailable`, 그리고 이 id의 maker 좌석은 재검증되지 않았다는 note가 붙습니다. 플러그인 파일은 수정하지 않습니다.

- **트리거.** SessionStart 훅이 `model_sync.py tick --detach`를 실행합니다(오프라인, 수 ms; 후속 id가 대상일 때만 분리된 프로브 실행을 띄웁니다). Codex는 훅 명령을 신뢰할지 한 번 묻습니다. Grok이나 훅이 없는 호스트는 스킬에서 같은 틱을 실행합니다.
- **상태.** `$DEEP_MODEL_ROUTER_STATE_DIR`, 없으면 `$XDG_STATE_HOME/deep-model-router`, 없으면 `~/.local/state/deep-model-router`(0700; 도구가 쓰는 상태이며 사람이 편집하지 않습니다). 라우터는 `committed/`만 읽습니다. `committed/`를 지우면 처음 설치 상태로 돌아가며, 폐기도 함께 사라집니다.
- **명령.** `model_sync.py status`(현 세대, 보류, 알림) · `revert <key>`(항목을 빼고 그 id를 폐기) · `unblock <id>` · `disable` / `enable`(자동 업그레이드; `disable`은 진행 중 프로브도 취소) · `repair [--to <generation> [--force]]` · `quota`(로컬 rollout 기록에서 읽는 codex 사용량; 모델 호출 없음) · `promote --repo … --key … --price …`(항목을 리포 체크아웃으로 옮김).
- **끄기.** `DEEP_MODEL_ROUTER_AUTOUPGRADE=0`은 틱과 발행을 멈춥니다. `DEEP_MODEL_ROUTER_OVERLAY=off`는 비상 스위치입니다: 라우터가 오버레이 항목을 무시하되 폐기는 유지하며, 이미 좌석에 앉혔던 오버레이 id는 재시도 이력 입력으로 계속 유효합니다. 손상된 committed 상태는 그래도 fail closed(`MODEL_STATE_UNAVAILABLE`)입니다 — `repair`를 쓰세요. 승인 검사를 통과하지 못한 상태 루트(내가 소유한 0700 모드 디렉터리가 아님)도 마찬가지이며, 라우트 note가 `chmod 700` 조치를 알려 줍니다.
- **진행 중인 deep-loop 실행.** deep-loop 1.25.0 이상은 실행의 고정 정책 다이제스트를 `policy_pin`으로 넘기므로, 오버레이 발행으로는 진행 중인 실행이 멈추지 않습니다. 플러그인 업데이트(번들 정책이 바뀜), 이후의 폐기, 세대 소실은 pin으로도 재현할 수 없습니다. 이때 deep-loop는 `router-policy-pin:<사유>`를 보고하고 `HIGH`/`CRITICAL` 작업을 멈추므로, 업데이트 전에 실행을 마무리하거나 멈춘 실행을 새로 시작하세요. deep-loop 1.25.0 미만에서는 오버레이 발행도 실행을 멈추므로, 긴 실행 동안에는 `DEEP_MODEL_ROUTER_AUTOUPGRADE=0`을 설정하세요.


---

## Claude Code 상태 표시

Claude Code 2.1.287 이상은 이 플러그인의 작은 mod(`hooks/hooks.claude.json`)도 함께 불러옵니다. mod는 스크립트가 이미 아는 것을 보여 줄 뿐이며 라우트·영수증·리뷰 하한을 바꾸지 않습니다. 스스로 실행하는 명령은 상태를 읽기만 합니다(`dispatch_agent.py status`, `model_sync.py status`, `uname`). Cancel을 비롯한 버튼은 입력창을 채우기만 합니다. Codex와 Grok은 이 mod를 불러오지 않습니다 — 모든 호스트에 필요한 검사는 스크립트에 있습니다.

- **디스패치된 좌석.** Bash 호출 안의 `dispatch_agent.py run`을 명령줄에서 읽어 추적을 시작하고, 끝날 때까지 20초마다 `dispatch_agent.py status`로 확인합니다. 상태줄은 `seats: codex·<model-id> RUNNING 4m/20m · <model-id> SUCCEEDED PASS`처럼 보입니다. 끝날 때마다 토스트가 뜨고, 사람이 처리해야 하는 상태에는 ⚠가 붙습니다: `TERMINATION_UNCONFIRMED`, `orphaned`·`stale` 감독, 마감을 한참 넘긴 `RUNNING` 영수증, `status`가 거부한 성공, 영수증을 남기지 않은 디스패치, 영수증 없이 남은 claim. `/router-seats`는 attempt별 패널을 열며, Status·Cancel·Verify 버튼은 명령을 입력창에 채우기만 합니다. Verify는 attempt id만 채우고 기대값(`--expect-count`, `--expect-fingerprint`, `--expect-models`)은 라우트를 보고 직접 넣도록 남깁니다. mod가 확실히 읽지 못하는 디스패치(변수나 패턴에 든 id·영수증 디렉터리, heredoc, 따라갈 수 없는 `cd` 뒤의 상대 디렉터리)는 건드리지 않습니다. `/router-seats add <receipt-dir> <attempt-id>`로 직접 추가하세요. `/router-seats clear`는 처리를 마친 끝난 좌석과 표시된 좌석을 지웁니다.
- **작성자 선언.** JSON을 출력한 `route_task.py` 호출 뒤, 리뷰 좌석에 이 세션의 모델이 앉아 있으면 결정마다 한 번 토스트로 경고합니다. 이 세션이 작업을 작성했다면 `implementer`나 `review_context`를 선언(또는 선언을 정정)하고 다시 라우팅하세요. `review_context`가 없는 `REVIEW` 라우트에는 힌트가 한 번 뜹니다. 모든 호스트에서, `--host-model`로 넘긴 모델이 리뷰 좌석에 앉았는데 작성자 선언이 없으면 라우트 자체가 note를 남깁니다.
- **디스패치 힌트.** `claude --bare` 좌석은 경고합니다(`Not logged in`으로 실패합니다). macOS에서 `caffeinate -i` 없이 10분 이상의 디스패치를 띄우면 힌트가 한 번 뜹니다(유휴 수면이 감독 프로세스를 멈춥니다).
- **model-sync 알림.** 세션 시작 5초 뒤 `model_sync.py status`를 한 번 읽습니다. 퇴역 알림, 다시 시도할 때가 된 보류 프로브, 진행 중인 프로브 실행이 있으면 토스트를 한 번 띄웁니다. 자세한 내용은 `/router-sync`에서 봅니다. 자동 업그레이드가 꺼져 있으면 아무것도 표시하지 않습니다.

Claude Code가 플러그인 hook을 끄는 곳(`disableAllHooks`, 관리형 hook 전용 정책, bare 모드, 신뢰하지 않은 워크스페이스)과 그릴 화면이 없는 headless `claude -p` 세션에서는 mod가 동작하지 않습니다.

---

## deep-suite 링크

| 플러그인 | 역할 |
|---|---|
| [deep-model-router](https://github.com/Sungmin-Cho/deep-model-router) | 이 플러그인 — 공유 결정 평면 |
| [deep-work](https://github.com/Sungmin-Cho/deep-work) | 단계별 구현 오케스트레이터 |
| [deep-review](https://github.com/Sungmin-Cho/deep-review) | APPROVE 판정의 독립 평가자 |
| [deep-loop](https://github.com/Sungmin-Cho/deep-loop) | 다중 세션 durable 제어 평면 |
| [deep-goal](https://github.com/Sungmin-Cho/deep-goal) | goal 조건 컴파일러 |
| [deep-evolve](https://github.com/Sungmin-Cho/deep-evolve) | 자율 fitness metric 실험 루프 |
| [deep-docs](https://github.com/Sungmin-Cho/deep-docs) | 문서 정비 에이전트 |
| [deep-wiki](https://github.com/Sungmin-Cho/deep-wiki) | 지식 베이스 수집·관리 |
| [deep-memory](https://github.com/Sungmin-Cho/deep-memory) | 프로젝트 간 시맨틱 메모리 |
| [deep-dashboard](https://github.com/Sungmin-Cho/deep-dashboard) | 하네스 진단과 스위트 텔레메트리 |
| [deep-suite (마켓플레이스)](https://github.com/Sungmin-Cho/deep-suite) | 통합 마켓플레이스와 하네스 매트릭스 |

## 링크

- [변경 이력](CHANGELOG.ko.md)
- [기여 안내](CONTRIBUTING.md)
- [보안](SECURITY.md)
- [Locator](docs/locator.md)
- [deep-suite 마켓플레이스](https://github.com/Sungmin-Cho/deep-suite)

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
