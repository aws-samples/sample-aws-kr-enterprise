"""Pairwise 비교: position bias 제거를 위해 2n번 비교."""
import re
import boto3
from PIL import Image
from io import BytesIO

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")
MODEL = "us.amazon.nova-pro-v1:0"


def _img_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def compare(
    image_a: Image.Image,
    image_b: Image.Image,
    user_prompt: str,
    ref_image: Image.Image | None = None,
    n: int = 3,
) -> str:
    """
    2n번 비교로 position bias 제거.
    ref_image가 있으면 레퍼런스 유사도를 주 기준으로 삼음.
    Returns: "A" | "B"  (A=image_a, B=image_b)
    """
    votes = {"A": 0, "B": 0}
    a_bytes = _img_bytes(image_a)
    b_bytes = _img_bytes(image_b)
    ref_bytes = _img_bytes(ref_image) if ref_image else None

    for i in range(2 * n):
        if i < n:
            first_bytes, second_bytes = a_bytes, b_bytes
            flip = False
        else:
            first_bytes, second_bytes = b_bytes, a_bytes
            flip = True

        content = []

        if ref_bytes:
            content.append({"text": (
                f'You are an expert image evaluator.\n'
                f'User prompt: "{user_prompt}"\n\n'
                f'A reference image is provided. Your PRIMARY criterion is: '
                f'which generated image is MORE SIMILAR to the reference image in terms of '
                f'style, color palette, mood, and composition?\n'
                f'Secondary criterion: which image better matches the prompt content.\n\n'
                f'Reference Image (target style/color to match):'
            )})
            content.append({"image": {"format": "png", "source": {"bytes": ref_bytes}}})
            content.append({"text": "Image A (generated):"})
            content.append({"image": {"format": "png", "source": {"bytes": first_bytes}}})
            content.append({"text": "Image B (generated):"})
            content.append({"image": {"format": "png", "source": {"bytes": second_bytes}}})
            content.append({"text": (
                "Compare A and B against the reference image. "
                "Which is closer to the reference in style and color? "
                "Explain briefly then end with <answer>A</answer> or <answer>B</answer>."
            )})
        else:
            content.append({"text": (
                f'You are an expert image evaluator.\n'
                f'User prompt: "{user_prompt}"\n'
                f'Which image better matches the prompt? '
                f'Explain briefly then end with <answer>A</answer> or <answer>B</answer>.'
            )})
            content.append({"text": "Image A:"})
            content.append({"image": {"format": "png", "source": {"bytes": first_bytes}}})
            content.append({"text": "Image B:"})
            content.append({"image": {"format": "png", "source": {"bytes": second_bytes}}})

        resp = BEDROCK.converse(
            modelId=MODEL,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 256, "temperature": 0.7},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        match = re.search(r"<answer>([AB])</answer>", text, re.IGNORECASE)
        if match:
            winner = match.group(1).upper()
            if flip:
                winner = "B" if winner == "A" else "A"
            votes[winner] += 1

    return "A" if votes["A"] >= votes["B"] else "B"
