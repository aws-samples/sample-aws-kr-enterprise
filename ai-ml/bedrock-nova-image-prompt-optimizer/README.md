# Nova Canvas Prompt Optimizer

Amazon Nova Canvas 이미지 생성 품질을 자동으로 개선하는 멀티에이전트 프롬프트 최적화 시스템입니다.

PromptSculptor (EMNLP 2025) + Maestro (arxiv:2509.10704) 논문 기법을 AWS Bedrock에 구현했습니다.


---

## 문제 상황

### 제약 조건
- 고객사는 보안 심의 상의 이유로 **Nova Canvas만 사용 가능** (타사 모델 사용 불가)
- Gemini, GPT-4o, Stable Diffusion 등으로 교체하는 방식은 선택지에 없음
- Nova Canvas의 한계 안에서 최대한 품질을 끌어올려야 하는 상황

### 핵심 문제
Nova Canvas에 프롬프트를 넣으면 레퍼런스 이미지의 스타일을 무시하고 기본 색상(할로윈이면 오렌지, 크리스마스면 빨강/초록)으로 생성하는 문제가 있었습니다. 프롬프트를 아무리 잘 써도 모델이 학습 데이터에 기반한 "기본 색상"을 강하게 따릅니다.

### 왜 자체 구현이 필요한가
Amazon Bedrock에는 [Prompt Optimization](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-optimize.html) 기능이 있지만, 이 기능은 **텍스트 출력 모델만 지원**하며 이미지 생성 모델(Nova Canvas)에는 적용할 수 없습니다. 따라서 이미지 생성 프롬프트를 자동 최적화하려면 별도의 파이프라인을 구축해야 합니다.

이 솔루션은 두 편의 학술 논문 기법을 결합하여 이 문제를 해결합니다.

---

## 기반 논문

### PromptSculptor (EMNLP 2025)

> 논문: https://arxiv.org/abs/2509.12446

짧고 모호한 사용자 프롬프트를 4개 전문 에이전트가 협업하여 고품질 프롬프트로 변환하는 프레임워크입니다. 핵심 아이디어:

- 프롬프트를 "스타일 분석", "요소 추출", "결합" 단계로 분리
- 각 단계를 전문화된 LLM 에이전트가 담당
- Chain-of-Thought 추론으로 숨겨진 맥락을 추론
- 모델에 구애받지 않는 설계로 어떤 T2I 모델에도 적용 가능

**본 솔루션에서의 구현:**
- Nova Pro → 레퍼런스 스타일 분석 + 색상 강제 프롬프트 생성
- Nova Lite → 핵심 시각 요소 추출
- Nova Pro → 두 결과를 결합하여 최종 프롬프트 생성

### Maestro (arxiv 2025)

> 논문: https://arxiv.org/abs/2509.10704

T2I 모델이 자율적으로 프롬프트를 반복 개선하는 자기 진화 시스템입니다. 핵심 아이디어:

- **DVQ (Decomposed Visual Questions)**: 프롬프트를 시각 속성별 Yes/No 질문으로 분해하여 구조화된 평가 수행
- **Self-Critique**: MLLM이 생성 이미지의 약점을 분석하고 해석 가능한 편집 신호 생성
- **Pairwise Comparison**: RLHF 방식의 쌍대 비교로 주관적 평가의 신뢰성 확보 (position bias 제거를 위해 2n번 순서 교체 비교)
- **Self-Verification**: 수정된 프롬프트가 원래 의도에서 벗어나지 않도록 검증

**본 솔루션에서의 구현:**
- DVQ 생성: Nova Pro Vision이 레퍼런스 이미지를 직접 보고 색상 기준 결정
- DVQ 평가: 생성 이미지 + 레퍼런스를 나란히 비교하여 스타일 일치 여부 판정
- Pairwise 비교: 6회 순서 교체 비교로 position bias 없이 best 선택
- Best 선택: DVQ 점수 차이 기반 + Pairwise 결합 로직

---

## Architecture

```
사용자 프롬프트 + 레퍼런스 이미지
        ↓
┌─── PromptSculptor ────────────────────────┐
│  [Nova Pro]  스타일 분석 + 색상 강제       │
│  [Nova Lite] 핵심 요소 추출               │
│  [Nova Pro]  두 프롬프트 결합 → 최종      │
└───────────────────────────────────────────┘
        ↓
┌─── Maestro Loop (반복) ───────────────────┐
│  Nova Canvas 이미지 생성                   │
│  (COLOR_GUIDED_GENERATION + 유저 색상)    │
│        ↓                                   │
│  DVQ 평가 (Nova Pro Vision)               │
│        ↓                                   │
│  Pairwise 비교 → Best 선택               │
│        ↓                                   │
│  점수 높아졌으면 → 다음 iteration         │
│  3회 연속 미개선 → 종료                   │
└───────────────────────────────────────────┘
        ↓
최종 Best 이미지 + 최적화된 프롬프트
```

---

## Quick Start

```bash
# 1. 환경 설정
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. AWS 자격증명 설정 (Bedrock 모델 접근 필요)
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_DEFAULT_REGION=us-east-1

# 3. 실행
streamlit run app.py
```

### 필수 조건
- Python 3.11+
- AWS 계정에서 Amazon Bedrock 모델 활성화:
  - `amazon.nova-canvas-v1:0` (이미지 생성)
  - `us.amazon.nova-pro-v1:0` (프롬프트 최적화 + 평가)
  - `us.amazon.nova-lite-v1:0` (요소 추출)

---

## UI 사용법

### Mode 선택
- **Generation**: 프롬프트만으로 이미지 생성
- **Variation**: 레퍼런스 + 프롬프트로 변형
- **Optimize (Nova)**: 자동 프롬프트 최적화 (이 솔루션의 핵심)

### Optimize 모드 설정

| 설정 | 설명 | 권장값 |
|------|------|--------|
| Reference Image | 스타일/색상 기준 이미지 | 업로드 필수 |
| Color Palette | 5개 색상 피커 (자동 추출됨, 수정 가능) | 레퍼런스 색상 유지 |
| 패턴 밀도 | 배경 기하학 패턴의 밀도 | sparse |
| 요소 개수 | 이미지에 들어갈 아이콘 수 | 5~7 |
| Max Iterations | 최적화 반복 횟수 | 3~5 |
| Patience | 연속 미개선 시 자동 종료 | 2~3 |

### 결과 화면
- 좌측: 프롬프트 진화 과정 (Nova Pro / Nova Lite / 결합), DVQ 평가 결과
- 우측: Best 이미지 + 다운로드, Iteration별 히스토리

---

## Project Structure

```
├── app.py                      # Streamlit UI
├── generate_images.py          # 기본 이미지 생성 스크립트
├── requirements.txt
└── optimizer/
    ├── pipeline.py             # 전체 파이프라인
    ├── prompt_sculptor.py      # Nova Pro + Lite 프롬프트 재창조
    ├── dvq_generator.py        # DVQ 질문 생성
    ├── dvq_evaluator.py        # VQA 평가
    ├── pairwise_comparator.py  # Pairwise 비교
    ├── color_extractor.py      # 레퍼런스 색상 추출 (k-means)
    ├── prompt_editor.py        # Targeted Editing
    └── self_verifier.py        # Self-Verification
```

---

## Tech Stack

| 역할 | 모델 / 서비스 |
|------|--------------|
| 이미지 생성 | `amazon.nova-canvas-v1:0` (eu-west-1) |
| 스타일 분석 + 결합 | `us.amazon.nova-pro-v1:0` |
| 핵심 요소 추출 | `us.amazon.nova-lite-v1:0` |
| DVQ 평가 / Pairwise 비교 | `us.amazon.nova-pro-v1:0` (Vision) |
| 색상 추출 | Pillow k-means clustering |
| UI | Streamlit |

---

## Key Design Decisions

1. **COLOR_GUIDED_GENERATION**: 유저 색상을 colors 배열로 강제 전달. referenceImage와 동시 사용 시 색상 충돌이 발생하므로 유저 직접 선택 시에는 colors만 사용.

2. **색상 override**: Nova Pro/Lite 시스템 프롬프트에 "Halloween pumpkin ≠ orange, use palette color" 규칙을 명시하여 모델의 기본 색상 편향을 극복.

3. **DVQ 질문 생성**: 레퍼런스 이미지를 직접 보여주고 "이 이미지의 실제 색상 기준으로 질문을 만들어라"로 지시. 고정된 색상 기준 없이 어떤 레퍼런스에도 적응.

4. **Best 선택**: DVQ 점수가 5%p 이상 낮아지면 Pairwise 비교 없이 기존 유지 — 품질 하락 방지.

---

## References

- PromptSculptor: https://arxiv.org/abs/2509.12446
- Maestro: https://arxiv.org/abs/2509.10704
