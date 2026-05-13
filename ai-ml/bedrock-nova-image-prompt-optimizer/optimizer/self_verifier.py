"""Self-Verification: 수정된 프롬프트가 원래 의도에서 벗어나지 않는지 검증."""
import re
import boto3

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")
MODEL = "us.amazon.nova-pro-v1:0"


def verify(prompt: str, dvqs: list[str], max_retries: int = 3) -> str:
    """
    DVQ를 constraints로 활용해 프롬프트 검증 및 수정.
    위반 없으면 원본 반환, 있으면 수정된 버전 반환.
    """
    constraints = "\n".join(f"- {q}" for q in dvqs)
    current = prompt

    for _ in range(max_retries):
        resp = BEDROCK.converse(
            modelId=MODEL,
            messages=[{
                "role": "user",
                "content": [{"text": f"""Verify whether this prompt satisfies all constraints.

Constraints (visual properties the image MUST have):
{constraints}

Prompt to verify:
"{current}"

For each constraint, check if the prompt would produce an image satisfying it.
If ALL constraints are satisfied: respond with <answer>NO_CHANGE</answer>
If any constraint is violated: fix the prompt and respond with <answer>fixed prompt here</answer>
Keep under 900 characters."""}],
            }],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        match = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)
        if not match:
            break
        result = match.group(1).strip()
        if result == "NO_CHANGE":
            break
        current = result

    return current
