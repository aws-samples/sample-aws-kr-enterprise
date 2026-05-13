"""DVQ 생성: 최종 생성 프롬프트 + 레퍼런스 이미지 기반으로 정확한 Yes/No 질문 생성."""
import re
import boto3
from io import BytesIO
from PIL import Image

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")
MODEL = "us.amazon.nova-pro-v1:0"

SYSTEM = """You are an expert in text-to-image evaluation.
Given the generation prompt (and optionally a reference image), generate 8-10 Decomposed Visual Questions (DVQs).

Categories to cover:
- Subject presence: are the key objects/elements from the prompt present?
- Color palette: based on the ACTUAL colors in the reference image (if provided) or the prompt
- Style: is the artistic style correct?
- Composition: is the layout correct?

CRITICAL RULES:
- Questions must be answerable by VISUALLY LOOKING at the image — no hex code matching
- For color: ask about general color families, NOT specific hex codes
  GOOD: "Is the color palette predominantly dark teal and navy blue?"
  BAD: "Is the Christmas tree colored in #267D90?"
- For elements: ask if they are PRESENT and VISIBLE, not about exact colors
  GOOD: "Is there a Christmas tree visible in the image?"
  BAD: "Is the Christmas tree colored in #267D90?"
- Use "Is/Are/Does" format, one property per question
- Be specific about presence and style, not about exact hex values

Output format:
<DVQ>question 1</DVQ>
<DVQ>question 2</DVQ>
...
"""


def _img_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.convert("RGB").resize((200, 200)).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def generate(
    user_prompt: str,
    has_reference: bool = False,
    final_prompt: str = "",
    ref_image: Image.Image | None = None,
) -> list[str]:
    """
    final_prompt + ref_image 기반으로 DVQ 생성.
    ref_image가 있으면 직접 보고 색상 기준 결정.
    """
    base = final_prompt if final_prompt else user_prompt

    content = []
    if ref_image:
        content.append({"text": "Reference image (use this to determine the correct color palette for DVQ questions):"})
        content.append({"image": {"format": "jpeg", "source": {"bytes": _img_bytes(ref_image)}}})

    content.append({"text": f"Generation prompt:\n{base}\n\nGenerate DVQs to evaluate whether the generated image matches this prompt."})
    if has_reference:
        content.append({"text": "Also include 2 questions about reference consistency based on what you see in the reference image above."})

    resp = BEDROCK.converse(
        modelId=MODEL,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.1},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    return re.findall(r"<DVQ>(.*?)</DVQ>", text, re.DOTALL)
