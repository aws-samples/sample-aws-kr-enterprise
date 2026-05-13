"""DVQ 평가: Nova Pro Vision으로 이미지 vs DVQ Yes/No 확률 반환."""
import boto3
from PIL import Image
from io import BytesIO

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")
MODEL = "us.amazon.nova-pro-v1:0"


def _img_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def evaluate(
    image: Image.Image,
    dvqs: list[str],
    ref_image: Image.Image | None = None,
) -> list[dict]:
    """
    Returns:
        [{"question": str, "answer": "Yes"|"No", "confidence": float}, ...]

    ref_image가 있으면 스타일/색상 관련 DVQ는 레퍼런스와 비교해서 평가.
    """
    img_data = _img_bytes(image)
    ref_data = _img_bytes(ref_image) if ref_image else None
    results = []

    for q in dvqs:
        is_style_q = any(kw in q.lower() for kw in [
            "style", "color", "palette", "reference", "consistent", "similar", "artistic"
        ])

        content = []
        if ref_data and is_style_q:
            # 스타일/색상 질문: 레퍼런스와 생성 이미지 둘 다 보여줌
            content.append({"text": "Reference image (target style/color):"})
            content.append({"image": {"format": "png", "source": {"bytes": ref_data}}})
            content.append({"text": "Generated image:"})
            content.append({"image": {"format": "png", "source": {"bytes": img_data}}})
            content.append({"text": f"Answer only with 'yes' or 'no'. Compare the generated image to the reference.\nQuestion: {q}"})
        else:
            content.append({"image": {"format": "png", "source": {"bytes": img_data}}})
            content.append({"text": f"Answer only with 'yes' or 'no'. Do not give other outputs.\nQuestion: {q}"})

        resp = BEDROCK.converse(
            modelId=MODEL,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 10, "temperature": 0.0},
        )
        answer_text = resp["output"]["message"]["content"][0]["text"].strip().lower()
        is_yes = answer_text.startswith("yes")
        results.append({
            "question": q,
            "answer": "Yes" if is_yes else "No",
            "confidence": 0.9 if is_yes else 0.1,
        })

    return results


def score(dvq_results: list[dict]) -> float:
    """DVQ 점수 = Yes 항목 수 / 전체."""
    if not dvq_results:
        return 0.0
    return sum(1 for r in dvq_results if r["answer"] == "Yes") / len(dvq_results)
