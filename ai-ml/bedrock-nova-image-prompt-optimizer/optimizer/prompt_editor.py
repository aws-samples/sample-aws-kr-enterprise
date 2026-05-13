"""Targeted Editing + Implicit Improvement."""
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


def targeted_editing(
    image: Image.Image,
    dvq_results: list[dict],
    current_prompt: str,
    user_prompt: str,
) -> str:
    """DVQ 'No' 항목 기반 프롬프트 수정. 변경 없으면 current_prompt 반환."""
    failed = [r for r in dvq_results if r["answer"] == "No"]
    if not failed:
        return current_prompt

    failed_text = "\n".join(
        f"- Question: {r['question']}" for r in failed
    )

    resp = BEDROCK.converse(
        modelId=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": "png", "source": {"bytes": _img_bytes(image)}}},
                {"text": f"""You are a prompt engineering expert for text-to-image models.

Current prompt: "{current_prompt}"
Original user intent: "{user_prompt}"

A reviewer answered "No" to these visual questions — these elements are MISSING from the image:
{failed_text}

Your task: rewrite the prompt so ALL missing elements are explicitly and prominently described.
- Place missing elements at the START of the prompt for emphasis
- Use strong descriptive language: "prominently featuring", "clearly visible", "in the foreground"
- Keep ALL existing elements that are already working
- Keep under 950 characters

Output format: <PROMPT>improved prompt here</PROMPT>"""},
            ],
        }],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.4},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    match = re.search(r"<PROMPT>(.*?)</PROMPT>", text, re.DOTALL)
    return match.group(1).strip() if match else current_prompt


def implicit_improvement(
    image: Image.Image,
    current_prompt: str,
    user_prompt: str,
) -> str:
    """전체적 개선 제안. 충분하면 current_prompt 반환."""
    resp = BEDROCK.converse(
        modelId=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": "png", "source": {"bytes": _img_bytes(image)}}},
                {"text": f"""You are a prompt engineering expert.
Original user intent: "{user_prompt}"
Current prompt: "{current_prompt}"

Look at the generated image broadly. Does it correctly reflect the user intent?
If YES and it looks good: respond with <PROMPT>NO_CHANGE</PROMPT>
If NO: suggest an improved prompt under 900 characters.
Output format: <PROMPT>improved prompt or NO_CHANGE</PROMPT>"""},
            ],
        }],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.5},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    match = re.search(r"<PROMPT>(.*?)</PROMPT>", text, re.DOTALL)
    if not match:
        return current_prompt
    result = match.group(1).strip()
    return current_prompt if result == "NO_CHANGE" else result
