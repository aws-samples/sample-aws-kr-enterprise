# QA 리포트: 대시보드 메트릭 패널 실시간성 개선 검증

**날짜**: 2026-05-13 17:57 KST (UTC+09)
**마일스톤**: 우측 메트릭 패널 5개 카드 (E2E Latency / Valkey CPU 포함) 실시간 데이터 표시 개선
**상태**: ❌ FAIL
**프로젝트 유형**: Full-stack (API Gateway + CloudFront SPA)
**권장사항**: **FIX REQUIRED**

---

## 평가 점수

| 평가 축 | 점수 | 기준 | 판정 |
|---------|------|------|------|
| Functionality (기능 완성도) | 5/5 | ≥ 4 | ✅ |
| Spec Fidelity (스펙 충실도) | 5/5 | ≥ 4 | ✅ |
| User Experience (사용자 경험) | 3/5 | ≥ 4 | ❌ |
| Edge Cases (경계 조건) | 4/5 | ≥ 3 | ✅ |
| Design Quality (디자인 품질) | 3/5 | ≥ 4 | ❌ |

**Scores**: Func 5/5 | Spec 5/5 | UX 3/5 | Edge 4/5 | Design 3/5

→ 두 축(UX, Design Quality)이 기준치 미달. 추가로 MEDIUM 등급 시각 레이아웃 결함 확인. 자동 **FIX REQUIRED**.

---

## 요약

| 카테고리 | 테스트 | 성공 | 실패 |
|----------|--------|------|------|
| API 기능 테스트 (/admin/metrics 스키마) | 3 | 3 | 0 |
| API 기능 테스트 (/demo/start-load + 실데이터 수집) | 3 | 3 | 0 |
| CORS / 보안 | 1 | 1 | 0 |
| UI 기능 테스트 (5개 카드 값 + 스파크라인) | 5 | 5 | 0 |
| UI 폴링 주기 검증 (10초) | 1 | 1 | 0 |
| UI 회귀 테스트 (리더보드, 게임 선택, 로드 버튼) | 3 | 3 | 0 |
| 반응형 (모바일 375px) | 1 | 1 | 0 |
| 디자인 품질 (카드 레이아웃 breathing room) | 5 | 3 | **2** |
| 콘솔 에러 | 1 | 1 | 0 |
| **합계** | **23** | **21** | **2** |

---

## 스펙 충실도 체크리스트

원본 태스크 요구사항 기준:

| # | 요구사항 | 구현 여부 | 동작 확인 | 비고 |
|---|---------|----------|----------|------|
| 1 | `/admin/metrics` 응답에 5개 key 모두 포함 (`sqs_depth`, `lambda_invocations`, `lambda_errors`, `valkey_cpu`, `e2e_latency`) | ✅ | ✅ | 모두 존재, HTTP 200, 0.6s |
| 2 | 각 key 아래 `timestamps: string[]`, `values: number[]`, `label: string` 필드 존재 | ✅ | ✅ | jq로 타입 확인 완료 |
| 3 | Valkey CPU가 idle 상태에서도 값 수집 (빈 배열 아님, "n/a" 라벨 아님) | ✅ | ✅ | 10 datapoint, 0.23~4.5%, label="Valkey CPU (max)" |
| 4 | E2E Latency 부하 시 datapoint ≥ 3, 값 > 0 | ✅ | ✅ | 15~16 datapoint, 모두 양수 |
| 5 | E2E Latency 라벨에 "score-processor" dimension 반영 | ✅ | ✅ | `"Leaderboard score-processor end_to_end_latency_ms"` |
| 6 | UI 5개 `.metric-card`에 숫자 표시 (`--` 없음), 스파크라인 렌더링 | ✅ | ✅ | 전 카드 숫자 + polyline 렌더됨 |
| 7 | 폴링 주기 10초 | ✅ | ✅ | 10.01s 간격 4회 측정 확인 |
| 8 | 회귀: 리더보드, 게임 선택, 로드 버튼 정상 | ✅ | ✅ | 100 rows, 게임 변경 시 데이터 갱신, 버튼 4종 모두 클릭 가능 |

→ **Spec Fidelity 5/5** — 원 스펙(메트릭 실시간성 개선)은 기능적으로 완전히 달성됨.

---

## 테스트 결과 상세

### [1] ✅ `/admin/metrics` 스키마 검증 (PASS)

**요청**:
```bash
curl -s https://pijtf5xn90.execute-api.us-east-1.amazonaws.com/admin/metrics
```

**응답 shape (jq 검증)**:
```
sqs_depth           present=true ts=10 vals=10 label="AWS/SQS leaderboard-score-events ApproximateNumberOfMessagesVisible"
lambda_invocations  present=true ts=4  vals=4  label="AWS/Lambda leaderboard-score-processor Invocations"
lambda_errors       present=true ts=4  vals=4  label="AWS/Lambda leaderboard-score-processor Errors"
valkey_cpu          present=true ts=10 vals=10 label="Valkey CPU (max)"
e2e_latency         present=true ts=16 vals=16 label="Leaderboard score-processor end_to_end_latency_ms"
```

- HTTP 200, 600ms
- CORS: `access-control-allow-origin: *` ✅
- 모든 key가 `timestamps:string[]`, `values:number[]`, `label:string` 스키마 일치

### [2] ✅ Valkey CPU 실데이터 수집 (PASS)

- `valkey_cpu.values` 길이 10, 최소값 0.23%, 최대값 4.5%
- idle(부하 없음) 상태에서도 0.23% 근처 실측값 수신
- 부하 인가 시 4.5%까지 상승하며 현실적 리소스 반응 반영
- label이 `"Valkey CPU (max)"` (fallback id 아님)

### [3] ✅ E2E Latency 실데이터 수집 (PASS)

부하 시나리오 (`sustain_5k_1min`, executionArn 발급 후 재측정):
```
e2e_latency.values (last 5): [57384.6, 62849.0, 67169.9, 72002.5, 75537.9] (ms)
e2e_latency.values.length: 15
e2e_latency.all_positive: true
e2e_latency.label: "Leaderboard score-processor end_to_end_latency_ms"
```

- datapoint 15개 (≥3 요건 충족)
- 전 값 양수
- label에 `score-processor` 서비스 dimension 반영 확인

**관찰 (QA 범위 외 맥락)**: E2E Latency가 최대 108초까지 치솟음. 이번 QA는 "UI에 숫자가 표시되는가"를 검증하는 것이므로 이는 observability 결과로서 PASS. 단, 백엔드 측면의 parking lot 이슈로 기록 필요 (P1-처리: 부하 인가 시 5K TPS에 대한 score-processor Lambda 처리 tail이 커 queue 적체가 지속됨).

### [4] ✅ UI 메트릭 카드 5개 모두 숫자 + 스파크라인 (PASS)

부하 후 60초 시점 DOM 스냅샷:
```
metric-sqs_depth          value="105.0K"  sparkline polyline points=10
metric-lambda_invocations value="12.0K"   sparkline polyline points=3
metric-lambda_errors      value="0"       sparkline polyline points=3
metric-valkey_cpu         value="2.1"     sparkline polyline points=10
metric-e2e_latency        value="70.6k"   sparkline polyline points=14
```

- 5/5 카드 숫자 표시 (`--` 없음)
- 5/5 카드 스파크라인 polyline 렌더링
- "No data" 텍스트 0개
- **이번 목표 대상인 E2E Latency, Valkey CPU 두 카드 모두 숫자 표시** ✅

### [5] ✅ 10초 폴링 실시간성 (PASS)

`metrics-status` 텍스트 변경 시점을 브라우저 내부에서 1초 단위로 폴링:
```
elapsed=0ms      "Updated 5:56:05 PM"
elapsed=5005ms   "Updated 5:56:15 PM"   (Δ≈10s)
elapsed=15015ms  "Updated 5:56:25 PM"   (Δ≈10s)
elapsed=25023ms  "Updated 5:56:35 PM"   (Δ≈10s)
```

- 4회 관찰, 간격 평균 10.0초 ± 0.02초
- `POLL_INTERVAL_MS = 10_000` 정확히 준수

### [6] ✅ 회귀 테스트 (PASS)

| 항목 | 결과 |
|------|------|
| 리더보드 100행 로딩 | 100 rows, "1  user_212  51,226" 형식 |
| 게임 드롭다운 변경(arena-shooter → puzzle-01) | 첫 row가 user_212(51,226) → user_667(50,183)로 실제 변경 |
| Load 버튼 4종(`sustain_5k_5min`, `sustain_5k_1min`, `burst`, `ramp`) | 모두 enabled, 클릭 가능 |
| poll-status dot | `class="status-dot active"` (활성) |
| 콘솔 에러 | **0건** (Errors: 0, Warnings: 0) |

### [7] ✅ 반응형 (모바일 375px, PASS)

- viewport 375, document width 375 — **horizontal scroll 없음**
- 5개 카드 모두 단일 컬럼으로 세로 배치, overflowingCards = 0
- 스크린샷: `dashboard-mobile.png` (5개 카드 모두 숫자/스파크라인 정상)

### [8] ❌ 디자인 품질: 카드 헤더 밀림 (FAIL, MEDIUM)

**증거 (`browser_evaluate` 실측값, viewport 1440×900)**:

| 카드 ID | 카드 폭 | title 텍스트 | value 텍스트 | title-value **간격(px)** |
|---------|---------|--------------|--------------|-------------------------|
| sqs_depth | 127 | "SQS Depth" | "0" | 27 |
| **lambda_invocations** | **143** | **"Lambda Invocations"** | **"1.5K"** | **0** ❌ |
| **lambda_errors** | **127** | **"Lambda Errors"** | **"0"** | **0** ❌ (값이 짧을 때만 여유) |
| valkey_cpu | 143 | "Valkey CPU" | "4.9" | 17 |
| e2e_latency | 278 | "E2E Latency" | "97.0k" | 128 |

CSS 분석:
- `.metric-header { display: flex; justify-content: space-between; gap: normal; flex-wrap: nowrap; }`
- `gap`이 `normal`(=0)로 설정되어, 제목이 길고 카드 폭이 좁을 때 title과 value가 픽셀 단위로 맞닿음
- 특히 Lambda Invocations / Lambda Errors 두 카드는 **gap=0**, 즉 "LAMBDA INVOCATIONS12.0K"처럼 단어가 숫자와 붙어 보임 (스크린샷 `dashboard-post-load.png` 참조)

**심각도**: MEDIUM — 사용자가 대시보드 기본 viewport에서 "항상" 본다. "close enough"로 넘길 수 없음.

**수정 방향** (`web/src/style.css`의 `.metric-header` 규칙):
```css
.metric-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;          /* 최소한의 breathing room */
}
.metric-title {
  min-width: 0;      /* 긴 제목이 2줄로 wrap 되도록 허용 */
  white-space: normal;
}
```
또는 제목과 값을 수직 스택으로 전환 (Apple Design 스펙에 맞게 title 상단/value 하단 배치 + 대형 tabular number).

---

## 사용자 경험 평가

### API DX (5/5)
- 엔드포인트 경로(`/admin/metrics`, `/demo/start-load`) 및 HTTP 메서드 semantics 적절
- 응답 field naming 일관(snake_case)
- 5개 메트릭 모두 동일 shape `{timestamps, values, label}` — 프론트에서 동일 로직 재사용 가능
- CORS 헤더 정확

### 프론트 UX (3/5 — FAIL)
- ✅ 10초마다 자동 갱신으로 "live" 느낌 전달 성공
- ✅ "Updated HH:MM:SS" 상태 표시로 신선도 인지
- ✅ 부하 버튼 → SQS/Lambda/CPU 그래프 반응 → 즉각적 피드백 루프
- ❌ **메트릭 카드 2개에서 title과 value 텍스트가 0px 간격으로 붙음** — 읽기 불편, 신뢰도 하락

→ 이번 개선이 기능적으로는 "실시간성 문제 해결"이지만, 시각적 품질에서 MEDIUM 이슈를 남김. UX 기준(≥4) 미달.

---

## 디자인 품질 평가 (프론트엔드)

### Visual hierarchy — ⚠️
- 값(bold, large)과 라벨(regular, small)의 타이포 대비는 적절
- 그러나 위에서 언급한 heading/value 충돌이 hierarchy를 무너뜨림

### Layout & spacing — ❌
- 2×2 그리드(SQS/Lambda-inv/Lambda-err/Valkey) + 1 wide(E2E) 레이아웃 자체는 합리적
- 2열 카드가 너무 좁아 카드 내부 spacing이 부족 → `gap=0` 현상

### Typography — ✅
- tabular number 느낌의 bold 숫자 + 소문자 라벨은 계량 대시보드 컨벤션 부합
- 본문 텍스트 크기 읽기 가능

### Color usage — ✅
- 5개 메트릭 각각 의미 있는 색(orange=queue, blue=invocation, red=error, green=cpu, purple=latency)
- 어두운 배경 대비 텍스트 contrast 적절

### Component quality — ⚠️
- borderless dark cards, 미세 shadow — 의도적 디자인
- 다만 card header 내부 좁은 공간에서 컴포넌트가 "깨진" 느낌 줌

### Responsive — ✅
- 모바일 375px: 단일 컬럼으로 자연스럽게 전환, 스크롤 없음, 스파크라인 잘 렌더됨
- 모바일에서는 간격 문제 없음 (각 카드가 전체 폭을 차지하므로 space-between 여유가 충분)

### AI slop detection — ✅ Clean
- 보라-파랑 그라데이션 없음, shadcn/ui 기본 스타일 티 없음, 실제 제품 맥락(게임 리더보드)에 맞는 선택
- AI slop 패턴 탐지되지 않음 → Design Quality cap 2 적용 **안 됨**

→ **Design Quality 3/5** (Generic/functional, 레이아웃 결함으로 polished 수준 미도달). 기준(≥4) 미달.

---

## 수정 필요 항목 (dev 에이전트 전달)

- **[MEDIUM]** `.metric-header`에서 title과 value 사이 breathing room 부재로 2열 카드 폭(127~143px)에서 텍스트가 맞붙음.
  - 위치: `web/src/style.css` (`.metric-header` 규칙)
  - 입력: 대시보드를 viewport ≥ 1200px에서 부하 상태로 관찰. Lambda Invocations 카드 값이 "12.0K" 가 되는 시점.
  - 예상: title과 value 사이 최소 8px 여백, 시각적 분리 명확
  - 실제: `gap=0px` (bbox 측정). "LAMBDA INVOCATIONS12.0K"처럼 맞닿음
  - 수정 방향:
    ```css
    .metric-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
    }
    .metric-title { min-width: 0; }
    ```
    또는 title/value를 상하 수직 스택으로 전환 (Apple 대시보드 KPI 카드 스타일).

- **[LOW]** E2E Latency 단위 포맷이 `108.2k` 처럼 소문자 k로 표기되는데, 다른 카드(Lambda Invocations `12.0K`, SQS `105.0K`)는 대문자 K. 단위 일관성 결여.
  - 위치: `web/src/dashboard.ts` `formatValue` 함수, `unit === "ms"` 분기의 `(latest / 1000).toFixed(1)}k` 부분
  - 수정 방향: `"k"` → `"K"` 로 통일하거나, ms는 "s" (예: "108.2s")로 단위 변환 표기 권장 (ms 단위 100,000은 "100k ms"보다 "100s"가 인간 친화적)

---

## 검증 커맨드

```bash
# [1] API 스키마
curl -s https://pijtf5xn90.execute-api.us-east-1.amazonaws.com/admin/metrics \
  | jq '. | to_entries | map({k:.key, ts_len:(.value.timestamps|length), v_len:(.value.values|length), label:.value.label})'

# [2] CORS 확인
curl -sD - https://pijtf5xn90.execute-api.us-east-1.amazonaws.com/admin/metrics \
  -H "Origin: https://d1tuanzhhkc3z5.cloudfront.net" -o /dev/null \
  | grep -i access-control

# [3] 부하 인가 → 60~90초 대기 → /admin/metrics 재조회
curl -s -X POST https://pijtf5xn90.execute-api.us-east-1.amazonaws.com/demo/start-load \
  -H "Content-Type: application/json" \
  -d '{"pattern":"sustain_5k_1min"}' | jq .
sleep 75
curl -s https://pijtf5xn90.execute-api.us-east-1.amazonaws.com/admin/metrics \
  | jq '{vcpu:.valkey_cpu.values[-3:], e2e:.e2e_latency.values[-5:], e2e_label:.e2e_latency.label}'

# [4] UI 수동 확인
open "https://d1tuanzhhkc3z5.cloudfront.net/?t=$(date +%s)"
```

---

## 증거 스크린샷

- `metrics-before-load-desktop.png` — 부하 직전 대시보드 (배포/이전 부하 잔여로 카드 모두 숫자 표시 유지)
- `dashboard-pre-load.png` — `sustain_5k_1min` POST 직후 상태
- `dashboard-post-load.png` — 부하 60초 후 데스크톱 (1440px). **Lambda Invocations / Lambda Errors 카드에서 title-value 맞닿음 확인 가능**
- `dashboard-mobile.png` — 모바일 375px (카드 단일 컬럼, 레이아웃 문제 없음)

---

## 결론

**스펙 충실도 (이번 개선의 원 목적)**: ✅ 달성
- E2E Latency, Valkey CPU 포함 5개 카드 모두 실데이터 수신/표시
- 10초 폴링 정상, CloudWatch HighResolution(10s) 10개 이상 datapoint 확보
- 회귀 없음, 콘솔 에러 0

**전체 품질 (사용자 관점)**: ❌ 미달
- UX 3/5, Design 3/5 — 헤더 레이아웃 결함이 기본 viewport에서 상시 노출
- MEDIUM 1건 + LOW 1건 → 검증 기준상 FIX REQUIRED

**권장사항**: ❌ **FIX REQUIRED**

이번 작업의 목표였던 "메트릭 패널 실시간성 문제" 자체는 기술적으로 완전히 해결됐지만, 해결 결과를 담는 그릇(카드 레이아웃)에 시각적 결함이 남아 있어 "데모 신뢰도"라는 상위 목표는 완전히 달성되지 않았다. `.metric-header`의 gap/align 규칙만 수정하면 즉시 Design/UX 기준을 넘을 수 있는 작은 수정이므로 1 라운드 추가 반복을 권장한다.

---

# Re-verification (Round 2)

**날짜**: 2026-05-13 18:07 KST
**범위**: Round 1에서 지적한 2개 결함(`[MEDIUM]` header gap, `[LOW]` unit casing) 수정 확인 + 필수 회귀 1회 스모크
**상태**: ✅ PASS
**최종 권장사항**: ✅ **PROCEED**

## 재검증 점수

| 평가 축 | R1 점수 | R2 점수 | 기준 | 판정 |
|---------|:------:|:------:|:----:|:----:|
| Functionality | 5/5 | 5/5 | ≥ 4 | ✅ |
| Spec Fidelity | 5/5 | 5/5 | ≥ 4 | ✅ |
| User Experience | 3/5 | **5/5** | ≥ 4 | ✅ |
| Edge Cases | 4/5 | 4/5 | ≥ 3 | ✅ |
| Design Quality | 3/5 | **4/5** | ≥ 4 | ✅ |

## 수정 확인

### [MEDIUM 해결 확인] `.metric-header` 간격

DOM CSS 실측 (viewport 1440×900):
```
.metric-header { display:flex; justify-content:space-between; align-items:baseline; gap:8px; column-gap:8px; flex-wrap:nowrap }
.metric-title  { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
```

부하 중(`sustain_5k_1min` 인가 60초 후) 카드별 title↔value bbox 간격:

| 카드 | 카드 폭 | title | value | gap(px) | R1→R2 |
|------|---------|-------|-------|:-------:|:------:|
| sqs_depth          | 147 | "SQS Depth"          | "29.5K" | **8** | 27 → 8 (min floor 확보) |
| lambda_invocations | 203 | "Lambda Invocations" | "2.4K"  | **8** | **0 → 8** ✅ |
| lambda_errors      | 147 | "Lambda Errors"      | "0"     | **14** | **0 → 14** ✅ |
| valkey_cpu         | 203 | "Valkey CPU"         | "0.9"   | 77 | 17 → 77 |
| e2e_latency        | 357 | "E2E Latency"        | "35.4K" | 208 | 128 → 208 |

**모든 카드에서 horizOverlap=false, gap ≥ 8px 확보.** R1에서 0px였던 두 카드도 해결.

부가 관찰: 레이아웃 변경으로 4개 작은 카드 폭이 127→147 / 143→203으로 넓어져 시각적 breathing room이 추가로 확보됨(의도된 결과로 판단 — `align-items: baseline`과 `min-width: 0` 적용으로 flex 계산이 자연스럽게 재분배된 것).

### [LOW 해결 확인] 단위 대소문자 일관성

배포된 bundle `https://d1tuanzhhkc3z5.cloudfront.net/assets/index-Ct1AAdNM.js` (last-modified 2026-05-13 09:03:52 UTC, 오늘 재배포) 분석:
- 문자열 리터럴 내 `"k"` 발생: **0건**
- 문자열 리터럴 내 `"K"` 발생: **0건** (backtick 내부로 이동함)
- Template literal `` `...k` `` 발생: **0건**
- Template literal `` `...K` `` 발생: **2건** (`formatValue`의 ms 분기 + >1000 분기 모두 대문자)

DOM 실측:
- E2E Latency 라이브 값 샘플: `35.4K`, `53.6K`, `73.9K`, `88.3K` — **모두 대문자 K**
- SQS/Lambda 카드와 일관 (`29.5K`, `85.3K`, `10.3K` 등)

API 레벨 확인: `/admin/metrics` 조회 결과 `e2e_latency.values` 8개 모두 >1000ms, 최대 96,973ms (≈97초) — K 단위로 표기되는 조건 모두 충족.

## 스모크 회귀 (수정 범위 외 핵심만)

| 항목 | 결과 | 근거 |
|------|:----:|------|
| `/admin/metrics` HTTP 200, 5개 key 유지 | ✅ | `e2e_len=8`, valkey/lambda 정상 |
| 5개 카드 모두 숫자 표시(`--` 없음), 스파크라인 polyline 렌더 | ✅ | DOM 스냅샷 — 전 카드 값 존재 |
| 폴링 활성(`status-dot active`, "Updated HH:MM:SS" 갱신) | ✅ | `Updated 6:07:03 PM` 관찰 |
| 콘솔 에러 0 | ✅ | `Total messages: 0 (Errors: 0, Warnings: 0)` |
| 모바일 375px: horizontal scroll 없음 | ✅ | `hasHScroll: false` |

## 증거 스크린샷

- `dashboard-reverify-final-desktop.png` — 1440×900 부하 중 대시보드. 모든 metric card의 title과 value 사이 분명한 여백 확인 가능. `E2E LATENCY 88.3K` 표기.

## 결론

Round 1에서 지적한 2개 이슈 모두 깨끗하게 해결됨을 DOM 실측과 bundle 분석으로 교차 확인. 스펙 충실도는 Round 1부터 이미 달성된 상태였고, 이번 수정으로 UX/Design 품질 기준치(≥4)를 넘김.

**권장사항**: ✅ **PROCEED**
