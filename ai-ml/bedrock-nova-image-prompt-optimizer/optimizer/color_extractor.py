"""레퍼런스 이미지에서 dominant hex 색상 최대 10개 추출 (k-means)."""
from PIL import Image
import numpy as np


def extract_colors(image: Image.Image, n: int = 8) -> list[str]:
    """PIL Image → hex 색상 리스트."""
    img = image.convert("RGB").resize((150, 150))
    pixels = np.array(img).reshape(-1, 3).astype(float)

    # k-means (순수 numpy, sklearn 의존성 없음)
    np.random.seed(42)
    centers = pixels[np.random.choice(len(pixels), n, replace=False)]

    for _ in range(20):
        dists = np.linalg.norm(pixels[:, None] - centers[None], axis=2)
        labels = dists.argmin(axis=1)
        new_centers = np.array([
            pixels[labels == k].mean(axis=0) if (labels == k).any() else centers[k]
            for k in range(n)
        ])
        if np.allclose(centers, new_centers, atol=1):
            break
        centers = new_centers

    # 클러스터 크기 기준 정렬 → 상위 n개
    counts = [(labels == k).sum() for k in range(n)]
    sorted_centers = [c for _, c in sorted(zip(counts, centers), reverse=True)]

    return [
        "#{:02X}{:02X}{:02X}".format(int(r), int(g), int(b))
        for r, g, b in sorted_centers[:10]
    ]
