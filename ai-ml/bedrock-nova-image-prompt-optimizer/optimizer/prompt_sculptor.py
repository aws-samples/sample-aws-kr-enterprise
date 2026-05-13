"""PromptSculptor: Nova Pro + Nova Lite 2개가 각자 프롬프트 재창조 후 결합."""
import re
import boto3
from io import BytesIO
from PIL import Image
import numpy as np

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")
NOVA_PRO = "us.amazon.nova-pro-v1:0"
NOVA_LITE = "us.amazon.nova-lite-v1:0"


def _img_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _extract_prompt(text: str, fallback: str) -> str:
    m = re.search(r"<PROMPT>(.*?)</PROMPT>", text, re.DOTALL)
    return m.group(1).strip() if m else fallback


def _get_top_colors(image: Image.Image, n: int = 5) -> list[str]:
    """레퍼런스 이미지에서 상위 n개 hex 색상 추출."""
    img = image.convert("RGB").resize((100, 100))
    pixels = np.array(img).reshape(-1, 3).astype(float)
    np.random.seed(42)
    centers = pixels[np.random.choice(len(pixels), n, replace=False)]
    for _ in range(30):
        dists = np.linalg.norm(pixels[:, None] - centers[None], axis=2)
        labels = dists.argmin(axis=1)
        new_centers = np.array([
            pixels[labels == k].mean(axis=0) if (labels == k).any() else centers[k]
            for k in range(n)
        ])
        if np.allclose(centers, new_centers, atol=1):
            break
        centers = new_centers
    counts = [(labels == k).sum() for k in range(n)]
    sorted_centers = [c for _, c in sorted(zip(counts, centers), reverse=True)]
    return ["#{:02X}{:02X}{:02X}".format(int(r), int(g), int(b)) for r, g, b in sorted_centers]


# ── Nova Pro: 색상 강제 + 스타일 분석 전문가 ──────────────────────────────────
PRO_SYSTEM = """You are a color-strict prompt engineer for Amazon Nova Canvas.

MISSION: Force Nova Canvas to use ONLY the specified color palette. This is the HIGHEST priority rule.

STEP 1 — Color palette is ABSOLUTE:
- The user has specified exact hex colors. These override ALL default element colors.
- Halloween pumpkins are NOT orange — they are the specified palette color
- Ghosts are NOT white — they are the specified palette color
- Spider webs are NOT black/white — they are the specified palette color
- Christmas trees are NOT green — they are the specified palette color
- EVERY element must use ONLY the specified hex colors, no exceptions

STEP 2 — Expand user intent if vague:
If the user prompt is vague (e.g. "Halloween atmosphere"), expand to specific visual elements.
But describe each element using ONLY the palette colors.

STEP 3 — Compose the prompt:
- Start with: "Strict color palette: [hex codes]. Use ONLY these colors for ALL elements including backgrounds, icons, lines, and nodes."
- Background: geometric network with intersecting lines and circular nodes
- Each element: "[palette color] [element name] icon, no outline" — e.g. "#015270 pumpkin icon, no outline"
- Add: "NO orange, NO white, NO black, NO colors outside the specified palette"

STEP 4 — Element rendering:
- Small flat icons, filled with palette colors only, no outline, no border, no stroke
- Proportional size — not oversized

Under 900 chars.

Output ONLY:
<PROMPT>color-enforced prompt</PROMPT>"""


def _nova_pro_sculpt(user_prompt: str, ref_image: Image.Image | None, density_desc: str = "", element_desc: str = "", user_colors: list[str] | None = None) -> str:
    content = []
    if ref_image:
        colors = user_colors if user_colors else _get_top_colors(ref_image, n=5)
        color_str = ", ".join(colors)
        content.append({"text": f"MANDATORY color palette (use ONLY these colors): {color_str}\nReference image (for style/composition only — NOT for color):"})
        content.append({"image": {"format": "jpeg", "source": {"bytes": _img_bytes(ref_image)}}})
        content.append({"text": f"User content:\n{user_prompt}\n\nPattern density: {density_desc}\nElement style: {element_desc}\n\nWrite a color-strict prompt using ONLY the mandatory colors above."})
    else:
        colors = user_colors if user_colors else []
        color_str = ", ".join(colors) if colors else "no specific palette"
        content.append({"text": f"MANDATORY color palette: {color_str}\nUser prompt:\n{user_prompt}\nPattern density: {density_desc}\nElement style: {element_desc}"})

    resp = BEDROCK.converse(
        modelId=NOVA_PRO,
        system=[{"text": PRO_SYSTEM}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.2},
    )
    return _extract_prompt(resp["output"]["message"]["content"][0]["text"], user_prompt)


# ── Nova Lite: 핵심 요소 + 색상 적용 전문가 ───────────────────────────────────
LITE_SYSTEM = """You are a concise prompt engineer for Amazon Nova Canvas.

MISSION: Place the USER'S REQUESTED elements within a geometric network, using ONLY the specified color palette.

COLOR RULE (ABSOLUTE — highest priority):
- The specified hex colors override ALL default element colors
- "pumpkin" does NOT mean orange — use the palette color
- "ghost" does NOT mean white — use the palette color  
- "spider web" does NOT mean black — use the palette color
- Write each element as: "[palette hex] [element] icon, no outline"

RULES:
1. Background: geometric network with thin intersecting lines and circular nodes
2. Extract key elements from USER PROMPT only — do not invent elements
3. Each element: small flat icon in palette color, no outline, no border, proportional size
4. Add: "NO orange, NO white, NO black, NO colors outside palette, no outlines"
5. Under 600 chars

Output ONLY:
<PROMPT>concise color-strict prompt</PROMPT>"""


def _nova_lite_sculpt(user_prompt: str, ref_image: Image.Image | None, density_desc: str = "", element_desc: str = "", user_colors: list[str] | None = None) -> str:
    content = []
    if ref_image:
        colors = user_colors if user_colors else _get_top_colors(ref_image, n=3)
        color_str = ", ".join(colors)
        content.append({"text": f"MANDATORY color palette (use ONLY these): {color_str}"})
        content.append({"image": {"format": "jpeg", "source": {"bytes": _img_bytes(ref_image)}}})
        content.append({"text": f"User content:\n{user_prompt}\nPattern density: {density_desc}\nElement style: {element_desc}\n\nWrite a concise prompt using ONLY the mandatory colors."})
    else:
        colors = user_colors if user_colors else []
        color_str = ", ".join(colors) if colors else "no specific palette"
        content.append({"text": f"MANDATORY color palette: {color_str}\nUser prompt:\n{user_prompt}\nPattern density: {density_desc}\nElement style: {element_desc}"})

    resp = BEDROCK.converse(
        modelId=NOVA_LITE,
        system=[{"text": LITE_SYSTEM}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 500, "temperature": 0.3},
    )
    return _extract_prompt(resp["output"]["message"]["content"][0]["text"], user_prompt)


# ── Nova Pro: 두 프롬프트 결합 ────────────────────────────────────────────────
MERGE_SYSTEM = """You are a prompt synthesis expert for Amazon Nova Canvas.

Merge two prompt candidates into ONE final prompt.
Candidate A: color-strict + composition-faithful (background pattern first)
Candidate B: concise + elements integrated into network

Merged prompt must:
1. START with background: sparse geometric network background with thin intersecting lines and circular nodes (use exact hex colors from candidates)
2. Add the USER'S REQUESTED elements as small-to-medium icons within the network — proportional, not oversized
3. CRITICAL rendering rules (must include verbatim):
   - "NO black outlines, NO dark borders, NO stroke around elements"
   - "elements filled with palette colors only, transparent-style flat icons"
   - "each element appears only once, evenly distributed"
4. Keep color constraints (hex codes from reference)
5. Under 950 chars

Output ONLY:
<PROMPT>merged final prompt</PROMPT>
<NEGATIVE>black outlines, dark borders, stroke, orange, yellow, red, cyan, lime green, olive, diagonal splits, dense patterns, cluttered, photorealistic, 3D</NEGATIVE>"""


def sculpt(
    user_prompt: str,
    ref_image: Image.Image | None = None,
    pattern_density: str = "sparse",
    icon_style: str = "silhouette (실루엣)",
    user_colors: list[str] | None = None,
) -> dict:
    """
    Nova Pro(색상 강제) + Nova Lite(핵심 요소) → Nova Pro 결합.
    """
    use_silhouette = "silhouette" in icon_style
    # icon_style이 "max N elements" 형태면 개수 파싱
    import re as _re
    _m = _re.search(r"max (\d+)", icon_style)
    max_elem = int(_m.group(1)) if _m else 6

    density_desc = {
        "very sparse": "very few lines (3-5 total), large open areas, minimal nodes",
        "sparse": "few intersecting lines, small circular nodes, visible open space",
        "moderate": "moderate number of lines covering most of the image",
        "dense": "many intersecting lines covering the entire image",
    }.get(pattern_density, "few intersecting lines, small circular nodes, visible open space")

    element_desc = (
        f"exactly {max_elem} distinct elements/icons integrated into the network nodes — "
        f"no more than {max_elem} elements total, spread evenly across the image, "
        f"each element appears only once"
    )

    pro_prompt = _nova_pro_sculpt(user_prompt, ref_image, density_desc, element_desc, user_colors)
    lite_prompt = _nova_lite_sculpt(user_prompt, ref_image, density_desc, element_desc, user_colors)

    merge_content = []
    if ref_image:
        colors = _get_top_colors(ref_image, n=5)
        merge_content.append({"text": f"Reference image (colors: {', '.join(colors)}):"})
        merge_content.append({"image": {"format": "jpeg", "source": {"bytes": _img_bytes(ref_image)}}})

    merge_content.append({"text": (
        f"Candidate A (color-strict):\n{pro_prompt}\n\n"
        f"Candidate B (concise elements):\n{lite_prompt}\n\n"
        f"Original intent: {user_prompt}"
    )})

    resp = BEDROCK.converse(
        modelId=NOVA_PRO,
        system=[{"text": MERGE_SYSTEM}],
        messages=[{"role": "user", "content": merge_content}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.1},
    )
    merged_text = resp["output"]["message"]["content"][0]["text"]

    prompt_match = re.search(r"<PROMPT>(.*?)</PROMPT>", merged_text, re.DOTALL)
    neg_match = re.search(r"<NEGATIVE>(.*?)</NEGATIVE>", merged_text, re.DOTALL)

    return {
        "prompt": prompt_match.group(1).strip() if prompt_match else pro_prompt,
        "negative": "orange, yellow, red, cyan, lime green, olive green, bright colors, photorealistic, 3D, gradients, white background" + (", " + neg_match.group(1).strip() if neg_match else ""),        "style": None,
        "pro_prompt": pro_prompt,
        "lite_prompt": lite_prompt,
        "style_analysis": "",
    }
