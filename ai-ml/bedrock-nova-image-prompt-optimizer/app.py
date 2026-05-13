import streamlit as st
import boto3
import json
import base64
from io import BytesIO
from PIL import Image

st.set_page_config(page_title="Bedrock Image Generator", layout="wide")
st.title("Bedrock Image Generator")

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
# 이미지 생성 모델은 eu-west-1 사용 (us-east-1 Legacy 차단 우회)
bedrock_img = boto3.client("bedrock-runtime", region_name="eu-west-1")

# ── Sidebar / Left Panel ─────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")

    mode = st.radio("Mode", ["Generation", "Variation", "Optimize (Nova)"])

    model = st.selectbox("Model", ["Nova Canvas", "Titan v2"])

    prompt = st.text_area("Prompt", height=200, placeholder="Enter your prompt here...")

    if mode == "Variation":
        ref_file = st.file_uploader("Reference Image", type=["png", "jpg", "jpeg"])
        similarity = st.slider("Similarity Strength", 0.1, 1.0, 0.7, 0.05)

    if mode == "Optimize (Nova)":
        ref_file_opt = st.file_uploader("Reference Image (optional)", type=["png", "jpg", "jpeg"])
        st.divider()
        st.subheader("Color Palette")
        st.caption("레퍼런스 이미지에서 자동 추출되거나 직접 입력하세요.")

        # 레퍼런스 이미지에서 자동 추출
        auto_colors = []
        if ref_file_opt:
            from PIL import Image as PILImage
            from optimizer.color_extractor import extract_colors as _extract
            _ref_preview = PILImage.open(ref_file_opt)
            ref_file_opt.seek(0)
            auto_colors = _extract(_ref_preview, n=5)

        default_colors = auto_colors if auto_colors else ["#015270", "#267D90", "#09627D", "#00506E", "#1B7188"]

        user_colors = []
        cols_c = st.columns(5)
        for idx, col in enumerate(cols_c):
            c = col.color_picker(f"#{idx+1}", value=default_colors[idx] if idx < len(default_colors) else "#015270")
            user_colors.append(c)

        st.divider()
        st.subheader("Optimizer Config")
        max_iter = st.slider("Max Iterations", 1, 8, 5)
        patience = st.slider("Patience", 1, 5, 3)
        pattern_density = st.select_slider(
            "패턴 밀도",
            options=["very sparse", "sparse", "moderate", "dense"],
            value="sparse",
            help="레퍼런스 기하학 패턴의 밀도 조절"
        )
        max_elements = st.slider(
            "요소 개수 (이모지/아이콘)",
            min_value=3, max_value=12, value=6,
            help="이미지에 들어갈 요소(이모지/아이콘) 최대 개수"
        )
        col1, col2 = st.columns(2)
        opt_width = col1.number_input("Width", value=1024, step=64)
        opt_height = col2.number_input("Height", value=1024, step=64)
        generate_btn = st.button("Optimize", type="primary", use_container_width=True)
    else:
        st.divider()
        st.subheader("Image Config")

        if model == "Nova Canvas":
            default_w, default_h = (2896, 1440) if mode == "Generation" else (1024, 512)
        else:
            default_w, default_h = (1408, 640) if mode == "Generation" else (1152, 640)

        col1, col2 = st.columns(2)
        width = col1.number_input("Width", value=default_w, step=64)
        height = col2.number_input("Height", value=default_h, step=64)
        cfg_scale = st.slider("Prompt Strength (CFG)", 1.0, 10.0, 8.0, 0.5)
        seed = st.number_input("Seed", value=0, min_value=0)
        num_images = st.slider("Number of Images", 1, 3, 3)

        generate_btn = st.button("Generate", type="primary", use_container_width=True)


# ── Helpers ──────────────────────────────────────────────────────────────────

def decode_images(b64_list):
    return [Image.open(BytesIO(base64.b64decode(b))) for b in b64_list]


def run_nova_generation(prompt, width, height, cfg, seed, n):
    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt},
        "imageGenerationConfig": {
            "width": int(width), "height": int(height),
            "cfgScale": cfg, "seed": int(seed), "numberOfImages": n,
        },
    }
    resp = bedrock_img.invoke_model(
        modelId="amazon.nova-canvas-v1:0",
        body=json.dumps(body),
        contentType="application/json", accept="application/json",
    )
    return decode_images(json.loads(resp["body"].read())["images"])


def run_titan_generation(prompt, width, height, cfg, seed, n):
    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt},
        "imageGenerationConfig": {
            "width": int(width), "height": int(height),
            "cfgScale": cfg, "seed": int(seed), "numberOfImages": n,
        },
    }
    resp = bedrock_img.invoke_model(
        modelId="amazon.titan-image-generator-v2:0",
        body=json.dumps(body),
        contentType="application/json", accept="application/json",
    )
    return decode_images(json.loads(resp["body"].read())["images"])


def run_nova_variation(prompt, ref_b64, similarity, width, height, cfg, seed, n):
    body = {
        "taskType": "IMAGE_VARIATION",
        "imageVariationParams": {
            "text": prompt,
            "images": [ref_b64],
            "similarityStrength": similarity,
        },
        "imageGenerationConfig": {
            "width": int(width), "height": int(height),
            "cfgScale": cfg, "seed": int(seed), "numberOfImages": n,
        },
    }
    resp = bedrock_img.invoke_model(
        modelId="amazon.nova-canvas-v1:0",
        body=json.dumps(body),
        contentType="application/json", accept="application/json",
    )
    return decode_images(json.loads(resp["body"].read())["images"])


def run_titan_variation(prompt, ref_b64, similarity, width, height, cfg, seed, n):
    body = {
        "taskType": "IMAGE_VARIATION",
        "imageVariationParams": {
            "text": prompt,
            "images": [ref_b64],
            "similarityStrength": similarity,
        },
        "imageGenerationConfig": {
            "width": int(width), "height": int(height),
            "cfgScale": cfg, "seed": int(seed), "numberOfImages": n,
        },
    }
    resp = bedrock_img.invoke_model(
        modelId="amazon.titan-image-generator-v2:0",
        body=json.dumps(body),
        contentType="application/json", accept="application/json",
    )
    return decode_images(json.loads(resp["body"].read())["images"])


# ── Right Panel (Main Area) ──────────────────────────────────────────────────

if generate_btn:
    if not prompt.strip():
        st.warning("Please enter a prompt.")
    elif mode == "Variation" and not ref_file:
        st.warning("Please upload a reference image for Variation mode.")

    # ── Optimize Mode ──────────────────────────────────────────────────────
    elif mode == "Optimize (Nova)":
        from optimizer import NovaOptimizer

        ref_image = Image.open(ref_file_opt) if ref_file_opt else None

        log_box = st.empty()
        progress_msgs = []

        def on_progress(msg):
            progress_msgs.append(msg)
            log_box.info("\n\n".join(progress_msgs))

        # 레퍼런스 이미지 전달 여부 즉시 확인
        if ref_image:
            st.success(f"✅ 레퍼런스 이미지 로드됨: {ref_image.size}")
        else:
            st.warning("⚠️ 레퍼런스 이미지 없음 — TEXT_IMAGE 모드로 실행")

        optimizer = NovaOptimizer(
            max_iterations=max_iter,
            patience=patience,
            width=int(opt_width),
            height=int(opt_height),
            similarity_strength=0.8,
            on_progress=on_progress,
        )

        with st.spinner("Optimizing..."):
            result = optimizer.run(
                user_prompt=prompt,
                ref_image=ref_image,
                user_colors=user_colors,
                pattern_density=pattern_density,
                icon_style=f"max {max_elements} elements",
            )

        log_box.empty()

        # ── 결과 표시 ──────────────────────────────────────────────────────
        left, right = st.columns([1, 1])

        with left:
            st.subheader("Prompt Evolution")

            with st.expander("Original Prompt", expanded=False):
                st.write(prompt)

            with st.expander("PromptSculptor Output", expanded=True):
                if result.sculpted_prompt.get("pro_prompt"):
                    st.caption("🔵 Nova Pro (스타일 분석)")
                    st.write(result.sculpted_prompt.get("pro_prompt", ""))
                if result.sculpted_prompt.get("lite_prompt"):
                    st.caption("🟢 Nova Lite (핵심 요소)")
                    st.write(result.sculpted_prompt.get("lite_prompt", ""))
                st.caption("🔀 결합 최종 프롬프트")
                st.write(result.sculpted_prompt.get("prompt", ""))
                if result.sculpted_prompt.get("negative"):
                    st.caption("Negative")
                    st.write(result.sculpted_prompt.get("negative", ""))

            with st.expander("Final Prompt", expanded=True):
                st.code(result.best_prompt)

            st.subheader("DVQs")
            for dvq in result.dvqs:
                st.write(f"• {dvq}")

            st.subheader("Final DVQ Evaluation")
            for r in result.dvq_results:
                icon = "✅" if r["answer"] == "Yes" else "❌"
                st.write(f"{icon} {r['question']}")

            score_pct = f"{result.final_dvq_score:.0%}"
            if result.used_fallback:
                st.warning(f"Titan fallback 사용됨 — DVQ Score: {score_pct}")
            else:
                st.metric("DVQ Score", score_pct)
        with right:
            st.subheader("Best Result")
            buf = BytesIO()
            result.best_image.save(buf, format="PNG")
            st.image(result.best_image, use_container_width=True)
            st.download_button(
                "Download Best Image",
                data=buf.getvalue(),
                file_name="optimized.png",
                mime="image/png",
                use_container_width=True,
            )

            st.subheader("Iteration History")
            for log in result.iterations:
                label = f"Iter {log.iteration} — DVQ {log.dvq_score:.0%}"
                if log.is_best:
                    label += " ⭐ best"
                with st.expander(label):
                    st.image(log.image, use_container_width=True)
                    st.caption(log.prompt[:200])

    # ── Generation / Variation Mode ────────────────────────────────────────
    else:
        with st.spinner("Generating images..."):
            try:
                if mode == "Generation":
                    if model == "Nova Canvas":
                        images = run_nova_generation(prompt, width, height, cfg_scale, seed, num_images)
                    else:
                        images = run_titan_generation(prompt, width, height, cfg_scale, seed, num_images)
                else:
                    ref_b64 = base64.b64encode(ref_file.read()).decode("utf-8")
                    if model == "Nova Canvas":
                        images = run_nova_variation(prompt, ref_b64, similarity, width, height, cfg_scale, seed, num_images)
                    else:
                        images = run_titan_variation(prompt, ref_b64, similarity, width, height, cfg_scale, seed, num_images)

                st.success(f"Generated {len(images)} image(s)")
                cols = st.columns(len(images))
                for col, img in zip(cols, images):
                    col.image(img, use_container_width=True)

                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    col.download_button(
                        "Download",
                        data=buf.getvalue(),
                        file_name=f"generated_{images.index(img)+1}.png",
                        mime="image/png",
                        use_container_width=True,
                    )

            except Exception as e:
                st.error(f"Error: {e}")
else:
    st.info("Configure settings in the sidebar and click **Generate** or **Optimize**.")
