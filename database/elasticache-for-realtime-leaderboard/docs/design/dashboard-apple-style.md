# 우측 메트릭 대시보드 Apple 스타일 재설계 스펙

> **대상 dev 에이전트**: 이 문서는 `web/index.html` + `web/src/style.css` + `web/src/dashboard.ts`를 Apple 디자인 언어로 리팩터링하기 위한 완전한 구현 스펙이다. 값은 모두 픽셀 단위까지 확정되어 있으므로 CSS 변수 블록부터 복사 적용하고 단계별로 진행하면 된다.

---

## 0. 현재 문제와 재설계 방향

### 진단
1. **가로 스크롤 발생**: `dashboard.ts:127` 의 `renderSparkline(values, color, 140, 32)` 에서 SVG `width`를 고정 140px로 렌더링. 사이드바 실폭(320 − padding 40 = 280px)에 2열 카드(각 약 136px) 내부에 140px SVG가 들어가 overflow 발생.
2. **Apple 스러움 부족**: 카드 radius 6px, 단조로운 flat tertiary 배경, 숫자 hierarchy 약함 (16px semibold만), unit이 별도 라인으로 분리되어 비율 깨짐, trend delta 없음.
3. **카드 배치 낭비**: 2열 × 2행 + 5번째 full-width 의 비대칭 구조. 좁은 폭 탓에 sparkline 여유가 없음.

### 결정
- **사이드바 폭을 320 → 360px로 확대**하고 `main` grid를 `1fr 360px`로 변경.
- **메트릭 카드를 1열 × 5 세로 스택**으로 재배치. 각 카드가 280~320px 내폭을 확보 → sparkline이 viewBox 반응형으로 안정적 렌더.
- **Apple App Store Connect Analytics 대시보드 KPI 카드 패턴**을 그대로 적용: label(caption upper) → 큰 숫자(title급) + 인라인 unit → trend delta → sparkline.
- **sparkline은 viewBox 기반 100% 폭 반응형 SVG**로 전환. 고정 `width`/`height` attribute 제거.
- 다크 테마 유지, 단 Apple 다크모드 surface elevation 스택(`#000` → `#1C1C1E` → `#2C2C2E`)을 정확히 따른다.

---

## 1. Design Tokens (CSS 변수)

`web/src/style.css` 의 기존 `:root { --bg-primary ... }` 블록을 아래 블록으로 **전면 교체**한다. 기존 변수 이름(`--bg-primary`, `--accent`, `--radius`, `--font-mono`)도 모두 새 토큰에 맞춰 리네이밍했으니 전역 replace 주의.

```css
:root {
  /* ─── Surface (Dark Mode Elevation Stack) ─────────────── */
  --color-bg-canvas:        #000000;  /* Level 0: page canvas (현 --bg-primary 대체) */
  --color-bg-surface:       #1C1C1E;  /* Level 1: sections, aside, header bg */
  --color-bg-elevated:      #2C2C2E;  /* Level 2: metric cards, status box, buttons */
  --color-bg-hover:         #3A3A3C;  /* Level 3: hover state */

  /* ─── Labels (텍스트 계층 2단계 + 보조) ───────────────── */
  --color-label-primary:    #F5F5F7;  /* 제목, 큰 숫자 */
  --color-label-secondary:  #A1A1A6;  /* caption, unit, metadata */
  --color-label-tertiary:   #8E8E93;  /* placeholder, 비활성 */
  --color-label-quaternary: #424245;  /* watermark 급 */

  /* ─── Separators (미묘한 헤어라인만 사용) ─────────────── */
  --color-separator:        rgba(84, 84, 88, 0.65);  /* 구분선 */
  --color-separator-subtle: rgba(255, 255, 255, 0.06);  /* 카드 hairline border */
  --color-border-hairline:  rgba(255, 255, 255, 0.08);  /* 조금 더 뚜렷한 경계 */

  /* ─── Accent / Semantic (Apple Dark 팔레트) ──────────── */
  --color-blue:    #0A84FF;   /* primary interactive, chart series 1 */
  --color-green:   #30D158;   /* success, positive delta, Valkey CPU 정상 */
  --color-orange:  #FF9F0A;   /* warning, SQS Depth */
  --color-red:     #FF453A;   /* error, Lambda Errors, negative delta */
  --color-purple:  #BF5AF2;   /* E2E Latency 등 accent */
  --color-teal:    #64D2FF;   /* info (예비) */

  /* ─── Typography ──────────────────────────────────────── */
  --font-system: "SF Pro Display", "SF Pro Text", -apple-system,
                 BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", Roboto, sans-serif;
  --font-mono:   "SF Mono", ui-monospace, "JetBrains Mono", Monaco, monospace;

  /* size scale (Apple dashboard tuned) */
  --text-micro:     11px;  /* axis label, fine print */
  --text-caption:   12px;  /* KPI label, table header, status text */
  --text-subhead:   13px;  /* body small, button label */
  --text-callout:   14px;  /* section title (sidebar) */
  --text-body:      15px;  /* 일반 본문 */
  --text-heading:   17px;  /* header h1 */
  --text-kpi:       24px;  /* KPI value (카드 width ~300px에 맞춘 값) */
  --text-kpi-large: 28px;  /* KPI value, 넓은 카드용 */

  --weight-regular:  400;
  --weight-medium:   500;
  --weight-semibold: 600;

  /* letter-spacing */
  --tracking-tight:  -0.022em;   /* body */
  --tracking-kpi:    -0.02em;    /* 큰 숫자 */
  --tracking-caption: 0.04em;    /* uppercase caption */

  /* ─── Spacing (8pt grid) ──────────────────────────────── */
  --space-1:  4px;
  --space-2:  8px;
  --space-3:  12px;
  --space-4:  16px;
  --space-5:  20px;
  --space-6:  24px;
  --space-8:  32px;
  --space-10: 40px;

  /* ─── Radius ──────────────────────────────────────────── */
  --radius-xs: 4px;   /* chip, inline pill */
  --radius-sm: 8px;   /* button, input */
  --radius-md: 10px;  /* nested element */
  --radius-lg: 12px;  /* metric card, status box */
  --radius-xl: 14px;  /* section container */

  /* ─── Shadows (dark mode는 border가 주력, shadow는 보조) ─ */
  --shadow-card:  0 1px 0 rgba(255, 255, 255, 0.04) inset,
                  0 4px 12px rgba(0, 0, 0, 0.32);
  --shadow-hover: 0 1px 0 rgba(255, 255, 255, 0.06) inset,
                  0 6px 16px rgba(0, 0, 0, 0.40);

  /* ─── Motion ──────────────────────────────────────────── */
  --transition-fast: 0.2s ease;
  --transition-nav:  0.15s ease;

  /* ─── Layout ──────────────────────────────────────────── */
  --sidebar-width: 360px;  /* 320 → 360 확대 */
  --content-max:   1400px;
  --nav-height:    52px;   /* 48→52, Apple 44와 데모 여유 타협치 */
}
```

### 1.1 토큰 네이밍 변경 요약 (dev replace 시 주의)

| 기존 | 신규 |
|---|---|
| `--bg-primary` | `--color-bg-canvas` |
| `--bg-secondary` | `--color-bg-surface` |
| `--bg-tertiary` | `--color-bg-elevated` |
| `--text-primary` | `--color-label-primary` |
| `--text-secondary` | `--color-label-secondary` |
| `--accent` | `--color-blue` |
| `--accent-hover` | (hover는 `--color-bg-hover` 또는 blue 그대로 0.9 opacity) |
| `--success` | `--color-green` |
| `--error` | `--color-red` |
| `--warning` | `--color-orange` |
| `--border` | `--color-separator` |
| `--radius` | `--radius-lg` (12px로 상향) |
| `--font-sans` | `--font-system` |

---

## 2. Layout 결정

### 2.1 `<main>` grid

```css
main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) var(--sidebar-width);
  gap: var(--space-6);           /* 24px */
  padding: var(--space-6);
  max-width: var(--content-max);
  margin: 0 auto;
}

@media (max-width: 1024px) {
  main {
    grid-template-columns: 1fr;
    gap: var(--space-4);
  }
}
```

- **`1fr 360px`** (기존 320px에서 40px 확대). 이유: 메트릭 카드 1열 전환 시 카드 내폭이 300~320px을 확보해야 Apple의 KPI 여백 규칙(좌우 20px + 숫자 + delta + sparkline 여유)을 만족함.
- 좌측 `1fr`에 `minmax(0, 1fr)`를 붙여 내부 테이블이 flex 자식 overflow로 사이드바를 밀지 않도록 방어.
- **모바일 breakpoint 900px → 1024px로 상향**. 1024 이하에서는 1열 스택이 자연스럽다(768px 태블릿도 카드 한 줄이 더 읽기 쉬움).

### 2.2 메트릭 그리드

```css
.metrics-grid {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);   /* 12px */
}
/* 혹은 CSS Grid: grid-template-columns: 1fr; gap: 12px; */
```

- **5개 카드 세로 1열** 고정. 이전 `1fr 1fr` + `last-child:odd { grid-column: 1/-1 }` 트릭 제거.
- 카드 사이 gap은 12px (카드 내부 padding 16px와 구분되는 외부 리듬).

### 2.3 Sparkline 반응형 처리

`dashboard.ts` 의 `renderSparkline`을 아래 규칙으로 수정:

- SVG `width`/`height` attribute 제거. 대신 `viewBox="0 0 100 32"` + `preserveAspectRatio="none"` + CSS로 `width:100%; height:36px;` 지정.
- `points` 계산 시 `effectiveWidth`를 100(viewBox 단위)으로 고정, `effectiveHeight`를 32로 고정. 실제 렌더 크기는 CSS가 결정.
- `stroke-width`는 viewBox 단위 기준 `1.5` 가 실제 렌더에서 2~3px 처럼 보여 두꺼워짐 → `vectorEffect="non-scaling-stroke"` + `stroke-width="1.5"`로 실 픽셀 1.5px 고정.

```typescript
// dashboard.ts renderSparkline 교체 스니펫 (dev 참고)
function renderSparkline(values: number[], color: string): string {
  if (values.length === 0) {
    return `<svg viewBox="0 0 100 32" preserveAspectRatio="none" class="sparkline">
      <text x="50" y="18" text-anchor="middle" fill="var(--color-label-tertiary)"
            font-size="9" vector-effect="non-scaling-stroke">No data</text>
    </svg>`;
  }
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const pad = 1;
  const w = 100 - pad * 2;
  const h = 32 - pad * 2;
  const points = values.map((v, i) => {
    const x = pad + (i / Math.max(values.length - 1, 1)) * w;
    const y = pad + h - ((v - min) / range) * h;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const polyline = points.join(" ");
  const areaPoints = [
    `${pad},${pad + h}`,
    ...points,
    `${pad + w},${pad + h}`,
  ].join(" ");
  return `<svg viewBox="0 0 100 32" preserveAspectRatio="none" class="sparkline">
    <polygon points="${areaPoints}" fill="${color}" opacity="0.18"/>
    <polyline points="${polyline}" fill="none" stroke="${color}"
              stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
              vector-effect="non-scaling-stroke"/>
  </svg>`;
}
```

호출부(`updateCard`)도 인자 축소:

```typescript
sparkEl.innerHTML = renderSparkline(values, config.color);  // width/height 인자 제거
```

CSS:

```css
.metric-sparkline {
  margin-top: var(--space-2);   /* 8px */
  height: 36px;
  width: 100%;
  display: block;
}
.metric-sparkline svg.sparkline {
  width: 100%;
  height: 36px;
  display: block;
}
```

이렇게 하면 카드 폭이 bumpy하게 변해도 overflow 발생 불가능. 1440/1280/1024/768/375 모든 viewport에서 `documentElement.scrollWidth === clientWidth` 보장.

---

## 3. Metric Card 상세 스펙

### 3.1 카드 렌더 예시 (ASCII — 실제 비율 근사)

```
┌─────────────────────────────────────────────────────────┐
│  ● SQS DEPTH                                   ↑ +12%   │  ← Row 1: icon+label (11px) / delta (11px)
│                                                          │
│  1,247 msgs                                              │  ← Row 2: 28px value + 14px unit
│                                                          │
│  ╭╮    ╱╲    ╱╲___╱╲__╱╲                                 │  ← Row 3: sparkline (36px, full width)
│ ╱  ╲__╱  ╲__╱                                            │
│                                                          │
└─────────────────────────────────────────────────────────┘
  padding: 16px 20px 18px 20px
  background: #1C1C1E
  border: 1px solid rgba(255,255,255,0.06)
  border-radius: 12px
```

### 3.2 요소별 정확한 스펙

| 요소 | size | weight | color | 기타 |
|---|---|---|---|---|
| 카드 컨테이너 | padding `16px 20px 18px` | — | bg `--color-bg-surface` | radius `--radius-lg` (12px), border `1px solid --color-separator-subtle` |
| 카드 (hover) | — | — | bg `--color-bg-elevated` | transition `background var(--transition-fast)` |
| 색상 dot | 8×8px | — | 메트릭별 accent color | `border-radius: 50%`, `--space-2` gap to label |
| 라벨 (title) | `--text-micro` (11px) | `--weight-semibold` (600) | `--color-label-secondary` | `text-transform: uppercase`, `letter-spacing: --tracking-caption` (0.04em) |
| Delta (trend) | `--text-micro` (11px) | `--weight-medium` (500) | 양수 → `--color-green`, 음수 → `--color-red`, neutral → `--color-label-tertiary` | 화살표 `↑`/`↓` + 숫자 + `%`. P1은 옵셔널, 2차 과제로 남김 |
| KPI 숫자 | `--text-kpi` (24px) | `--weight-semibold` (600) | `--color-label-primary` | `font-variant-numeric: tabular-nums`, `letter-spacing: --tracking-kpi` (-0.02em) |
| Unit (숫자 옆 inline) | `--text-subhead` (13px) | `--weight-regular` (400) | `--color-label-secondary` | 숫자와 `--space-1` (4px) gap, baseline 정렬 |
| Sparkline | height 36px | — | 메트릭별 color, `polygon` 18% opacity, `polyline` 1.5px stroke | viewBox 100×32, width:100% |
| Row 1 → 숫자 gap | — | — | — | `margin-top: var(--space-2)` (8px) |
| 숫자 → sparkline gap | — | — | — | `margin-top: var(--space-3)` (12px) |

### 3.3 CSS 구현 블록

```css
.metric-card {
  background: var(--color-bg-surface);
  border: 1px solid var(--color-separator-subtle);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5) 18px;   /* 16 20 18 */
  display: flex;
  flex-direction: column;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.metric-card:hover {
  background: var(--color-bg-elevated);
  border-color: var(--color-border-hairline);
}

.metric-card__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
}

.metric-card__label {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-micro);
  font-weight: var(--weight-semibold);
  color: var(--color-label-secondary);
  text-transform: uppercase;
  letter-spacing: var(--tracking-caption);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.metric-card__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.metric-card__delta {
  font-size: var(--text-micro);
  font-weight: var(--weight-medium);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.metric-card__delta--up   { color: var(--color-green); }
.metric-card__delta--down { color: var(--color-red); }
.metric-card__delta--flat { color: var(--color-label-tertiary); }

.metric-card__value-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  margin-top: var(--space-2);
}
.metric-card__value {
  font-family: var(--font-system);        /* mono 아님 — Apple은 SF Pro tabular-nums */
  font-size: var(--text-kpi);             /* 24px */
  font-weight: var(--weight-semibold);
  font-variant-numeric: tabular-nums;
  letter-spacing: var(--tracking-kpi);
  color: var(--color-label-primary);
  line-height: 1.1;
}
.metric-card__unit {
  font-size: var(--text-subhead);         /* 13px */
  font-weight: var(--weight-regular);
  color: var(--color-label-secondary);
}

.metric-card__sparkline {
  margin-top: var(--space-3);             /* 12px */
  height: 36px;
  width: 100%;
}
```

### 3.4 HTML 템플릿 (dashboard.ts `buildDashboardHTML` 교체용)

```typescript
// METRIC_CARDS 설정은 그대로 유지 (color: #FF9F0A → --color-orange 매핑은 그대로 hex로)
// 단, 각 카드에 delta 자리를 예약해두고 초기 렌더는 숨김 처리
const cards = METRIC_CARDS.map(card => `
  <div class="metric-card" id="metric-${card.id}">
    <div class="metric-card__header">
      <span class="metric-card__label">
        <span class="metric-card__dot" style="background:${card.color}"></span>
        ${card.title}
      </span>
      <span class="metric-card__delta metric-card__delta--flat" id="delta-${card.id}" hidden></span>
    </div>
    <div class="metric-card__value-row">
      <span class="metric-card__value" id="value-${card.id}">--</span>
      <span class="metric-card__unit">${card.unit}</span>
    </div>
    <div class="metric-card__sparkline" id="spark-${card.id}"></div>
  </div>
`).join("");

return `<div class="metrics-grid">${cards}</div>
  <div class="metrics-footer">
    <span class="metrics-status" id="metrics-status">Fetching…</span>
  </div>`;
```

Delta 계산은 P2로 명시 (values 배열 맨 끝 vs 앞부분 평균 대비 % — dev가 필요 시 간단히 추가). 지금은 `hidden`으로 비워두고 레이아웃만 예약.

---

## 4. 다른 섹션 영향도 (일관성 유지를 위한 가이드)

새 토큰 체계를 적용하면 헤더/리더보드/버튼/상태박스도 자동으로 Apple 톤에 맞아들어가야 한다. dev가 놓치지 않도록 각 섹션별 체크리스트 제공.

### 4.1 Header (`header`)

| 항목 | 변경 |
|---|---|
| 배경 | `var(--color-bg-surface)` (`#1C1C1E`) |
| 높이 | `--nav-height` (52px) — 패딩으로 조정 `padding: 0 var(--space-6)` |
| border-bottom | `1px solid var(--color-separator)` |
| h1 | size `--text-heading` (17px), weight 600, color `--color-label-primary`, tracking `-0.01em` |
| game-select | bg `--color-bg-elevated`, border `--color-border-hairline`, radius `--radius-sm` (8px), focus border `--color-blue` |
| status-dot | 그대로 8px, 단 색상만 토큰으로 (`--color-green`, `--color-blue`, `--color-red`). Apple 원칙: "normal이면 pulse 하지 않는다" — 현재 pulse 있으면 제거 검토 |

### 4.2 Leaderboard 섹션 / 테이블

| 항목 | 변경 |
|---|---|
| section 배경 | `--color-bg-surface`, border `1px solid --color-separator-subtle`, radius `--radius-xl` (14px) |
| section padding | `var(--space-5)` (20px) → `var(--space-6)` (24px)로 확대 |
| h2 (Top 100…) | size `--text-caption` (12px) → `--text-callout` (14px) 상향, weight 600, color `--color-label-secondary`, uppercase, tracking `--tracking-caption`. Apple 대시보드 내부 header 톤과 일치 |
| `thead th` | font-size `--text-micro` (11px), weight 600, uppercase, tracking 0.04em, color `--color-label-secondary`, **배경 투명** (Apple 원칙: 헤더 셀 배경색 금지) |
| `tbody td` | font-size `--text-subhead` (13px), padding `10px var(--space-4)` (10px 16px), border-bottom `1px solid --color-separator-subtle` |
| hover row | `background: var(--color-bg-elevated)` (색상 shift만, scale/border 변화 금지) |
| **zebra striping 금지** | Apple 원칙. 기존 구현에 없지만 추가하지 말 것 |
| `.col-score` | 오른쪽 정렬 유지, color `--color-label-primary` (파란색 강조 대신 primary로 — Apple은 숫자에 색 잘 안 씀), weight 600, tabular-nums |

> **판단 포인트**: 기존 코드는 점수를 `--accent` 파란색으로 강조. Apple 스타일에서는 큰 숫자에 색을 쓰지 않는 게 원칙이다. 그러나 "리더보드"라는 도메인 특성상 점수가 핵심이라면 **primary(흰색) + 1위만 파란색**이 더 Apple답다. dev 재량.

### 4.3 Load Generator 버튼 (`.load-btn`)

| 항목 | 변경 |
|---|---|
| 배경 | `var(--color-bg-elevated)` |
| border | `1px solid var(--color-border-hairline)` |
| radius | `--radius-sm` (8px) — Apple dashboard 내 액션 버튼은 pill이 아니고 8px rect. pill은 marketing CTA용 |
| padding | `10px 14px` 유지 또는 `var(--space-3) var(--space-4)` |
| font | size `--text-subhead` (13px), weight `--weight-medium` (500) |
| color | `--color-label-primary` |
| hover | bg `--color-bg-hover`, border color 유지 (blue로 바꾸지 말 것 — 너무 anxious) |
| disabled | opacity 0.35 |

> **중요**: 기존 hover가 배경을 통째로 파란색으로 바꿨는데 (`hover { background: var(--accent); color: #fff; }`), 이건 Apple 스타일이 아님. Apple hover는 "slightly lighter surface" 1단계만 올린다.

### 4.4 Load Status 박스 (`#load-status`)

| 항목 | 변경 |
|---|---|
| 배경 | `var(--color-bg-elevated)` |
| border | `1px solid var(--color-separator-subtle)` |
| radius | `--radius-lg` (12px) |
| padding | `var(--space-3) var(--space-4)` (12px 16px) |
| font-size | `--text-subhead` (13px) |
| margin-bottom | `var(--space-5)` (20px) — 대시보드 섹션과 20px 간격 |
| `.status-label` | color `--color-label-secondary` |
| `#load-status-text` | mono 유지, color `--color-label-primary`, running 상태만 `--color-orange` |

### 4.5 Aside 자체

기존 `aside { background: var(--bg-secondary); border: 1px solid; padding: 20px }` 구조는 유지하되:
- radius `--radius-xl` (14px)
- padding `var(--space-6)` (24px)
- 내부 h2 ("Load Generator", "Metrics")은 위 4.2 section h2 규칙과 동일 적용
- Metrics 섹션 h2 위에 `margin-top: var(--space-5)` (20px) 추가해서 status box와 분리

### 4.6 Metrics footer ("Updated 14:23:45")

| 항목 | 변경 |
|---|---|
| font-size | `--text-micro` (11px) |
| font-family | `--font-mono` (SF Mono, 숫자용) |
| color | `--color-label-tertiary` |
| align | 우측 정렬 유지 |
| margin-top | `var(--space-3)` (12px) |
| error 상태 | color `--color-red`, weight 500 |

---

## 5. "지키면 안 되는 것" 체크리스트

dev가 구현하다 무의식적으로 일탈할 수 있는 패턴 5선. 코드 리뷰 시 이것들이 들어갔는지 확인한다.

1. **Sparkline에 고정 px width/height attribute 금지.**
   - `<svg width="140" height="32">` 처럼 attribute로 고정하면 반응형 깨짐. viewBox + CSS width:100%만 허용. 이게 이번 문제의 근본 원인이다.

2. **버튼 hover 시 통배경 파란색으로 바꾸지 말 것.**
   - Apple hover = 같은 계열 surface를 1단계 올리는 것 (`#2C2C2E` → `#3A3A3C`). 파란색 fill hover는 generic SaaS 룩이다. primary action이 아니라면 색을 도입하지 않는다.

3. **큰 숫자에 accent color 남용 금지.**
   - KPI 숫자는 `--color-label-primary` (흰색). 파랑/초록/빨강은 semantic (delta, error, success)에만. "모든 숫자를 파랗게"는 anxious dashboard의 상징.

4. **Card shadow를 진하게 쌓지 말 것.**
   - 다크 모드에서 shadow는 거의 안 보이는 게 맞음. 시각적 elevation은 hairline border(`rgba(255,255,255,0.06)`) + surface level 차이로 표현. `0 4px 20px rgba(0,0,0,0.6)` 같은 "무거운 카드" 금지.

5. **라벨 uppercase + tracking 일관성 깨지 말 것.**
   - 모든 caption 급 텍스트(KPI label, 섹션 h2, 테이블 헤더)는 동일하게 `11~12px / weight 600 / uppercase / letter-spacing 0.04em / --color-label-secondary`. 어떤 건 uppercase, 어떤 건 title case로 섞이면 즉시 non-Apple 느낌. "Metrics" h2도 "METRICS"로 렌더되어야 일관적.

### 추가: sparkline 색상과 delta 로직에 대한 주의

- sparkline polygon fill opacity는 18% 고정. 더 진하면 (25%+) 탁해짐, 더 옅으면 (10% 이하) 안 보임.
- Apple App Store Connect는 **KPI 카드에 sparkline을 넣지 않는다** (별도 chart section으로 분리). 이번 디자인은 사이드바 공간 제약상 sparkline을 유지하지만, "진짜 Apple"을 원한다면 P2에서 sparkline을 빼고 delta %만 남기는 옵션도 고려. 우선은 sparkline 유지가 현실적이다.

---

## 6. 구현 우선순위 (dev 가이드)

1. **P0 — 가로 스크롤 해결** (이번 세션 필수)
   - `renderSparkline` viewBox 반응형 전환
   - 사이드바 360px 확대 + 메트릭 1열 전환
   - `metric-sparkline` CSS 폭 100% 고정

2. **P1 — Apple 토큰 전면 적용** (연쇄 작업)
   - `:root` 변수 블록 교체
   - 전역 변수 이름 replace (`--bg-secondary` → `--color-bg-surface` 등)
   - Section/card radius 12→14, padding 재조정

3. **P2 — 카드 구조 리디자인**
   - `.metric-card__*` BEM 클래스로 전환
   - HTML 템플릿 교체 (color dot + delta placeholder 추가)
   - 숫자 + unit inline 배치

4. **P3 — 다른 섹션 일관성**
   - Header, Leaderboard table, Button, Status box를 Apple 체크리스트대로 업데이트

5. **P4 (옵션)** — delta % 로직 구현, status dot pulse 제거 검토

각 우선순위 단계 후 dev는 1440/1280/1024/768/375 5개 viewport에서 `document.documentElement.scrollWidth === document.documentElement.clientWidth` 를 Playwright로 검증해야 한다 (QA 에이전트에게 위임 가능).

---

## 7. 산출물 체크리스트 (이 문서를 덮은 후 dev가 최종 확인)

- [ ] `:root` CSS 변수 블록 1장(§1) 완전 교체 완료
- [ ] `main` grid `1fr 360px` + `minmax(0, 1fr)` 적용
- [ ] `.metrics-grid` 세로 1열 flex, gap 12px
- [ ] `renderSparkline` viewBox 반응형 (width/height attribute 제거)
- [ ] `.metric-sparkline` CSS `width: 100%; height: 36px`
- [ ] `.metric-card` BEM 구조 + 색상 dot + KPI 숫자 24px + inline unit
- [ ] Header h1, select, status-dot 토큰화
- [ ] Leaderboard table 헤더 배경 투명, zebra 없음, hover는 surface shift만
- [ ] `.load-btn` hover가 파란 fill이 아닌 surface-elevated → surface-hover로 변경
- [ ] 1440/1280/1024/768/375 viewport 가로 스크롤 없음 검증

---

## 8. 참고

- Apple 디자인 토큰 원전: `apple-design` 스킬 `references/tokens.md`
- Apple KPI 카드 패턴: 동 스킬 `references/patterns.md` §2.1
- 적용 대상 파일:
  - `/Users/anhyobin/dev/real-time-leaderboard/web/index.html`
  - `/Users/anhyobin/dev/real-time-leaderboard/web/src/style.css`
  - `/Users/anhyobin/dev/real-time-leaderboard/web/src/dashboard.ts`
