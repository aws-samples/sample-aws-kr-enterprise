"""Nova Canvas Prompt Optimizer — 전체 파이프라인."""
from __future__ import annotations
import base64
import json
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable

import boto3
from PIL import Image

from . import color_extractor, dvq_evaluator, dvq_generator
from . import pairwise_comparator, prompt_editor, prompt_sculptor, self_verifier

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")
# 이미지 생성 모델은 eu-west-1 사용 (us-east-1 Legacy 차단 우회)
BEDROCK_IMG = boto3.client("bedrock-runtime", region_name="eu-west-1")
NOVA_MODEL = "amazon.nova-canvas-v1:0"
TITAN_MODEL = "amazon.titan-image-generator-v2:0"
NOVA_LITE = "us.amazon.nova-pro-v1:0"  # 프롬프트 재최적화 — Nova Pro 사용 (더 정교)


@dataclass
class IterationLog:
    iteration: int
    prompt: str
    image: Image.Image
    dvq_results: list[dict]
    dvq_score: float
    is_best: bool


@dataclass
class OptimizeResult:
    best_image: Image.Image
    best_prompt: str
    final_dvq_score: float
    dvq_results: list[dict]
    iterations: list[IterationLog]
    used_fallback: bool = False
    sculpted_prompt: dict = field(default_factory=dict)
    dvqs: list[str] = field(default_factory=list)


def _to_b64(image: Image.Image) -> str:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _decode_b64(b64: str) -> Image.Image:
    return Image.open(BytesIO(base64.b64decode(b64)))


def _nova_generate(
    prompt: str,
    negative: str,
    style: str | None,
    colors: list[str],
    ref_image: Image.Image | None,
    width: int,
    height: int,
    seed: int,
    similarity_strength: float = 0.7,
    user_colors: list[str] | None = None,
    log_fn: Callable[[str], None] = lambda _: None,
) -> Image.Image:
    """Nova Canvas 호출 — COLOR_GUIDED(색상) + TEXT_IMAGE(내용) 방식."""
    body: dict = {
        "imageGenerationConfig": {
            "width": width,
            "height": height,
            "cfgScale": 9.0,
            "seed": seed,
            "numberOfImages": 1,
            "quality": "premium",
        },
    }

    if colors:
        # 유저가 색상 직접 선택한 경우: referenceImage 없이 colors만 사용
        # (referenceImage + colors 동시 사용 시 레퍼런스 색상이 유저 색상을 덮어버림)
        if user_colors:
            task = f"COLOR_GUIDED_GENERATION (user colors={len(colors)}, no referenceImage)"
            params: dict = {
                "text": prompt[:1024],
                "colors": colors[:10],
            }
            if negative:
                params["negativeText"] = negative[:1024]
            body["taskType"] = "COLOR_GUIDED_GENERATION"
            body["colorGuidedGenerationParams"] = params
        else:
            # 자동 추출 색상: referenceImage + colors 동시 사용
            task = f"COLOR_GUIDED_GENERATION (auto colors + referenceImage)"
            params = {
                "text": prompt[:1024],
                "colors": colors[:10],
            }
            if negative:
                params["negativeText"] = negative[:1024]
            if ref_image:
                params["referenceImage"] = _to_b64(ref_image)
            body["taskType"] = "COLOR_GUIDED_GENERATION"
            body["colorGuidedGenerationParams"] = params
    else:
        task = "TEXT_IMAGE"
        params = {"text": prompt[:1024]}
        if negative:
            params["negativeText"] = negative[:1024]
        if style and style != "none":
            params["style"] = style
        body["taskType"] = "TEXT_IMAGE"
        body["textToImageParams"] = params

    log_fn(f"  📌 taskType: {task}")
    log_fn(f"  📌 색상 팔레트: {colors[:3] if colors else '없음'}")
    log_fn(f"  📌 프롬프트 ({len(prompt)}자): {prompt[:150]}{'...' if len(prompt)>150 else ''}")

    resp = BEDROCK_IMG.invoke_model(
        modelId=NOVA_MODEL,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    return _decode_b64(result["images"][0])


def _nova_lite_reoptimize(
    current_prompt: str,
    user_prompt: str,
    failed_dvqs: list[str],
    ref_image: Image.Image | None,
    current_image: Image.Image,
) -> str:
    """Nova Lite로 프롬프트 재최적화 — iteration 2회차부터 사용."""
    failed_text = "\n".join(f"- {q}" for q in failed_dvqs)

    content = []
    # 레퍼런스 이미지가 있으면 함께 전달
    if ref_image:
        ref_buf = BytesIO()
        ref_image.save(ref_buf, format="PNG")
        content.append({"text": "[Reference Image - style/color to match:]"})
        content.append({"image": {"format": "png", "source": {"bytes": ref_buf.getvalue()}}})

    # 현재 생성된 이미지
    cur_buf = BytesIO()
    current_image.save(cur_buf, format="PNG")
    content.append({"text": "[Current Generated Image:]"})
    content.append({"image": {"format": "png", "source": {"bytes": cur_buf.getvalue()}}})

    content.append({"text": f"""You are a Nova Canvas TEXT_IMAGE prompt expert using Nova Pro.

The image is generated with COLOR_GUIDED_GENERATION — color palette from reference is locked.
Your job: rewrite the prompt so missing content elements appear, described in the reference's visual style.

Original intent: "{user_prompt}"
Current prompt: "{current_prompt}"

Missing elements:
{failed_text}

{"Reference style context: describe missing elements using the same visual language as the reference (same art style, same color tones, same level of abstraction)." if ref_image else ""}

Rules:
1. Describe each missing element in the reference's visual style
2. Keep all working elements
3. Be specific about visual appearance of each element
4. Keep under 950 characters

Output ONLY the new prompt, no explanation."""})

    resp = BEDROCK.converse(
        modelId=NOVA_LITE,
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 512, "temperature": 0.3},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def _titan_generate(    prompt: str,
    ref_image: Image.Image | None,
    width: int,
    height: int,
    seed: int,
) -> Image.Image:
    """Titan v2 fallback."""
    if ref_image:
        body = {
            "taskType": "IMAGE_VARIATION",
            "imageVariationParams": {
                "text": prompt[:1024],
                "images": [_to_b64(ref_image)],
                "similarityStrength": 0.7,
            },
            "imageGenerationConfig": {
                "width": width, "height": height,
                "cfgScale": 8.0, "seed": seed, "numberOfImages": 1,
            },
        }
    else:
        body = {
            "taskType": "TEXT_IMAGE",
            "textToImageParams": {"text": prompt[:1024]},
            "imageGenerationConfig": {
                "width": width, "height": height,
                "cfgScale": 8.0, "seed": seed, "numberOfImages": 1,
            },
        }
    resp = BEDROCK_IMG.invoke_model(
        modelId=TITAN_MODEL,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    return _decode_b64(result["images"][0])


class NovaOptimizer:
    def __init__(
        self,
        max_iterations: int = 5,
        patience: int = 3,
        dvq_early_stop: float = 0.9,
        fallback_threshold: float = 0.6,
        width: int = 1024,
        height: int = 1024,
        similarity_strength: float = 0.8,  # 레퍼런스 스타일 강하게 유지
        on_progress: Callable[[str], None] | None = None,
    ):
        self.max_iterations = max_iterations
        self.patience = patience
        self.dvq_early_stop = dvq_early_stop
        self.fallback_threshold = fallback_threshold
        self.width = width
        self.height = height
        self.similarity_strength = similarity_strength
        self.on_progress = on_progress or (lambda msg: None)

    def _log(self, msg: str):
        self.on_progress(msg)

    def run(
        self,
        user_prompt: str,
        ref_image: Image.Image | None = None,
        user_colors: list[str] | None = None,
        pattern_density: str = "sparse",
        icon_style: str = "silhouette (실루엣)",
    ) -> OptimizeResult:
        logs: list[IterationLog] = []

        # STEP 1: 색상 — 유저가 직접 선택한 색상 우선, 없으면 레퍼런스에서 추출
        if user_colors:
            colors = user_colors
            self._log(f"🎨 유저 선택 색상 사용: {colors}")
        elif ref_image:
            colors = color_extractor.extract_colors(ref_image)
            self._log(f"🎨 레퍼런스에서 색상 추출: {colors[:3]}...")
        else:
            colors = []
            self._log("🎨 색상 없음 — TEXT_IMAGE 모드")

        # STEP 2: DVQ 생성 (1회, 재사용) — 첫 iteration 프롬프트 생성 후 DVQ 만들기
        self._log("✍️ Iteration 1: Nova Pro + Nova Lite 프롬프트 재창조 중...")
        sculpted = prompt_sculptor.sculpt(
            user_prompt, ref_image=ref_image,
            pattern_density=pattern_density, icon_style=icon_style,
            user_colors=user_colors,
        )
        first_prompt = sculpted["prompt"]
        self._log(f"  🔵 Nova Pro: {sculpted['pro_prompt'][:100]}...")
        self._log(f"  🟢 Nova Lite: {sculpted['lite_prompt'][:100]}...")
        self._log(f"  🔀 결합 결과: {first_prompt[:120]}...")

        self._log("🔍 DVQ 생성 중 (최종 프롬프트 + 레퍼런스 이미지 기반)...")
        dvqs = dvq_generator.generate(
            user_prompt,
            has_reference=ref_image is not None,
            final_prompt=first_prompt,
            ref_image=ref_image,
        )
        self._log(f"  📋 DVQ {len(dvqs)}개 생성됨")

        best_image: Image.Image | None = None
        best_prompt = ""
        best_dvq_results: list[dict] = []
        best_dvq_score = 0.0
        no_improve_count = 0

        for i in range(self.max_iterations):
            # iteration 1은 이미 생성된 프롬프트 사용, 이후는 재창조
            if i == 0:
                current_prompt = first_prompt
                negative = sculpted["negative"]
                style = sculpted["style"]
            else:
                self._log(f"✍️ Iteration {i+1}: Nova Pro + Nova Lite 프롬프트 재창조 중...")
                sculpted = prompt_sculptor.sculpt(
                    user_prompt, ref_image=ref_image,
                    pattern_density=pattern_density, icon_style=icon_style,
                    user_colors=user_colors,
                )
                current_prompt = sculpted["prompt"]
                negative = sculpted["negative"]
                style = sculpted["style"]
                self._log(f"  🔵 Nova Pro: {sculpted['pro_prompt'][:100]}...")
                self._log(f"  🟢 Nova Lite: {sculpted['lite_prompt'][:100]}...")
                self._log(f"  🔀 결합 결과: {current_prompt[:120]}...")

            # ── 이미지 생성 ───────────────────────────────────────────────────
            self._log(f"🖼️ Iteration {i+1}: 이미지 생성 중...")
            image = _nova_generate(
                prompt=current_prompt,
                negative=negative,
                style=style,
                colors=colors,
                ref_image=ref_image,
                width=self.width,
                height=self.height,
                seed=i * 137 + 42,
                similarity_strength=self.similarity_strength,
                user_colors=user_colors,
                log_fn=self._log,
            )

            # ── DVQ 평가 ──────────────────────────────────────────────────────
            self._log(f"📊 Iteration {i+1}: DVQ 평가 중...")
            dvq_results = dvq_evaluator.evaluate(image, dvqs, ref_image=ref_image)
            current_score = dvq_evaluator.score(dvq_results)
            failed_dvqs = [r["question"] for r in dvq_results if r["answer"] == "No"]
            self._log(f"  📊 DVQ 점수: {current_score:.0%} ({len(dvqs)-len(failed_dvqs)}/{len(dvqs)} 통과)")
            if failed_dvqs:
                self._log(f"  ❌ 미충족: {', '.join(q[:50] for q in failed_dvqs[:3])}{'...' if len(failed_dvqs)>3 else ''}")

            # ── Pairwise 비교 + DVQ 점수 결합으로 best 선택 ──────────────────
            is_best = False
            if best_image is None:
                best_image = image
                best_prompt = current_prompt
                best_dvq_results = dvq_results
                best_dvq_score = current_score
                is_best = True
            else:
                dvq_diff = current_score - best_dvq_score
                if dvq_diff >= 0.05:
                    # DVQ 점수가 5%p 이상 높으면 무조건 업데이트
                    best_image = image
                    best_prompt = current_prompt
                    best_dvq_results = dvq_results
                    best_dvq_score = current_score
                    no_improve_count = 0
                    is_best = True
                    self._log(f"  ✅ DVQ 우선 업데이트 ({current_score:.0%}, +{dvq_diff:.0%})")
                elif dvq_diff >= -0.05:
                    # DVQ 점수 비슷할 때만 Pairwise로 결정
                    self._log(f"⚖️ Iteration {i+1}: Pairwise 비교 중...")
                    winner = pairwise_comparator.compare(best_image, image, user_prompt, ref_image=ref_image)
                    if winner == "B":
                        best_image = image
                        best_prompt = current_prompt
                        best_dvq_results = dvq_results
                        best_dvq_score = current_score
                        no_improve_count = 0
                        is_best = True
                        self._log(f"  ✅ Pairwise 업데이트 ({current_score:.0%})")
                    else:
                        no_improve_count += 1
                        self._log(f"  ↩️ 이전 유지 (미개선 {no_improve_count}회)")
                else:
                    # DVQ 점수가 5%p 이상 낮으면 무조건 기존 유지
                    no_improve_count += 1
                    self._log(f"  ↩️ DVQ 낮아짐 — 이전 유지 ({current_score:.0%} < {best_dvq_score:.0%})")

            logs.append(IterationLog(
                iteration=i + 1,
                prompt=current_prompt,
                image=image,
                dvq_results=dvq_results,
                dvq_score=current_score,
                is_best=is_best,
            ))

            # ── 종료 판단 ─────────────────────────────────────────────────────
            if best_dvq_score >= self.dvq_early_stop:
                self._log(f"✅ DVQ {best_dvq_score:.0%} 달성 — 조기 종료")
                break
            if no_improve_count >= self.patience:
                self._log(f"⏹️ {self.patience}회 연속 미개선 — 종료")
                break

        used_fallback = False

        return OptimizeResult(
            best_image=best_image,
            best_prompt=best_prompt,
            final_dvq_score=best_dvq_score,
            dvq_results=best_dvq_results,
            iterations=logs,
            used_fallback=used_fallback,
            sculpted_prompt=sculpted,
            dvqs=dvqs,
        )
