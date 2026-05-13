# Nova Canvas Prompt Optimizer

Amazon Nova Canvas 이미지 생성 품질을 자동으로 개선하는 멀티에이전트 프롬프트 최적화 시스템.

**PromptSculptor** (EMNLP 2025) + **Maestro** (arxiv:2509.10704) 논문 기법을 AWS Bedrock에 구현했습니다.

---

## How It Works

```
사용자 프롬프트 + 레퍼런스 이미지
        ↓
[PromptSculptor]  Nova Pro(스타일 분석) + Nova Lite(요소 추출) → Nova Pro(결합)
        ↓
[Maestro Loop]
  Nova Canvas 이미지 생성 (COLOR_GUIDED_GENERATION)
  → DVQ 평가 (Nova Pro Vision)
  → Pairwise 비교 → Best 선택 → 반복
```

---

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

AWS 자격증명은 `~/.aws/credentials` 또는 환경변수로 설정하세요.

---

## Stack

| 역할 | 모델 |
|------|------|
| 이미지 생성 | `amazon.nova-canvas-v1:0` |
| 프롬프트 최적화 | `us.amazon.nova-pro-v1:0` |
| 요소 추출 | `us.amazon.nova-lite-v1:0` |
| DVQ 평가 / Pairwise | `us.amazon.nova-pro-v1:0` (Vision) |

---

## References

- PromptSculptor: https://arxiv.org/abs/2509.12446
- Maestro: https://arxiv.org/abs/2509.10704
