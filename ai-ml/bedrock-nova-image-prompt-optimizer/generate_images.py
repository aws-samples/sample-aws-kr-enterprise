import boto3
import json
import base64
import os
from pathlib import Path

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")


# ── Generation ──────────────────────────────────────────────────────────────

def generate_nova(prompt: str, output_path: str):
    """Nova Canvas: TEXT_IMAGE"""
    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt},
        "imageGenerationConfig": {
            "width": 2896,
            "height": 1440,
            "cfgScale": 8,
            "seed": 0,
            "numberOfImages": 3,
        },
    }
    response = bedrock.invoke_model(
        modelId="amazon.nova-canvas-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    for i, img_b64 in enumerate(result["images"], 1):
        path = os.path.join(output_path, f"Nova {i}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(img_b64))
        print(f"  Saved: {path}")


def generate_titan(prompt: str, output_path: str):
    """Titan Image Generator v2: TEXT_IMAGE"""
    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt},
        "imageGenerationConfig": {
            "width": 1408,
            "height": 640,
            "cfgScale": 8,
            "seed": 0,
            "numberOfImages": 3,
        },
    }
    response = bedrock.invoke_model(
        modelId="amazon.titan-image-generator-v2:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    for i, img_b64 in enumerate(result["images"], 1):
        path = os.path.join(output_path, f"Titan {i}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(img_b64))
        print(f"  Saved: {path}")


# ── Variation ────────────────────────────────────────────────────────────────

def variation_nova(prompt: str, reference_path: str, output_path: str):
    """Nova Canvas: IMAGE_VARIATION"""
    with open(reference_path, "rb") as f:
        ref_b64 = base64.b64encode(f.read()).decode("utf-8")

    body = {
        "taskType": "IMAGE_VARIATION",
        "imageVariationParams": {
            "text": prompt,
            "images": [ref_b64],
            "similarityStrength": 0.7,
        },
        "imageGenerationConfig": {
            "width": 1024,
            "height": 512,
            "cfgScale": 8,
            "seed": 0,
            "numberOfImages": 3,
        },
    }
    response = bedrock.invoke_model(
        modelId="amazon.nova-canvas-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    for i, img_b64 in enumerate(result["images"], 1):
        path = os.path.join(output_path, f"Nova {i}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(img_b64))
        print(f"  Saved: {path}")


def variation_titan(prompt: str, reference_path: str, output_path: str):
    """Titan Image Generator v2: IMAGE_VARIATION"""
    with open(reference_path, "rb") as f:
        ref_b64 = base64.b64encode(f.read()).decode("utf-8")

    body = {
        "taskType": "IMAGE_VARIATION",
        "imageVariationParams": {
            "text": prompt,
            "images": [ref_b64],
            "similarityStrength": 0.7,
        },
        "imageGenerationConfig": {
            "width": 1152,
            "height": 640,
            "cfgScale": 8,
            "seed": 0,
            "numberOfImages": 3,
        },
    }
    response = bedrock.invoke_model(
        modelId="amazon.titan-image-generator-v2:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    for i, img_b64 in enumerate(result["images"], 1):
        path = os.path.join(output_path, f"Titan {i}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(img_b64))
        print(f"  Saved: {path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def read_prompt(path: str) -> str:
    with open(path, "r") as f:
        # 첫 번째 단락(빈 줄 전까지)만 프롬프트로 사용
        lines = []
        for line in f:
            if line.strip() == "":
                break
            lines.append(line.rstrip())
    return " ".join(lines)


if __name__ == "__main__":
    # Generation
    gen_prompt = read_prompt("Generation/Prompt.txt")
    print("=== Generation ===")
    print(f"Prompt: {gen_prompt[:80]}...")

    print("\n[Nova Canvas]")
    generate_nova(gen_prompt, "Generation")

    print("\n[Titan v2]")
    generate_titan(gen_prompt, "Generation")

    # Variation
    var_prompt = read_prompt("Variation/Prompt.txt")
    ref_image = "Variation/Reference.jpg"
    print("\n=== Variation ===")
    print(f"Prompt: {var_prompt[:80]}...")

    print("\n[Nova Canvas]")
    variation_nova(var_prompt, ref_image, "Variation")

    print("\n[Titan v2]")
    variation_titan(var_prompt, ref_image, "Variation")

    print("\nDone.")
