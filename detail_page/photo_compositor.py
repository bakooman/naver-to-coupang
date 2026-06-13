"""
photo_compositor.py — 실제 제품 사진을 스튜디오 배경 위에 합성

수정 이력:
  v2 (2026-06-11):
    - rembg 성공 여부 검증 + alpha matting 엣지 페더 처리
    - 타원형 ground shadow (바닥에 자연스러운 그림자) 추가
    - composite 섹션은 스튜디오 배경 전용 (section_generator에서 보장)
    - 제품 스케일·여백 일관성 개선
    - rembg 폴백 시 콘솔 경고 출력
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFilter, ImageDraw


# ── 공개 API ──────────────────────────────────────────────────────────

def composite_product_photos(
    background:    Image.Image,
    product_paths: list[str],
    layout:        str  = "auto",
    shadow:        bool = True,
) -> Image.Image:
    """AI 생성 스튜디오 배경 위에 실제 제품 사진(들)을 PIL로 합성."""
    valid_paths = [p for p in product_paths if p and os.path.isfile(p)]
    if not valid_paths:
        print("[Compositor] 유효한 제품 사진 없음 — 배경만 반환")
        return background.copy().convert("RGB")

    W, H   = background.size
    canvas = background.convert("RGBA")

    # 배경 제거 (rembg + 페더링)
    nobg_images: list[Image.Image] = []
    for path in valid_paths[:5]:
        nobg = _remove_background(path)
        if nobg is not None:
            nobg_images.append(nobg)

    if not nobg_images:
        return background.copy().convert("RGB")

    n              = len(nobg_images)
    chosen_layout  = layout if layout != "auto" else _choose_layout(n)
    slots          = _compute_slots(chosen_layout, n, W, H)

    for img_nobg, (sx, sy, sw, sh) in zip(nobg_images, slots):
        img_fit = _fit_into(img_nobg, sw, sh)
        px = sx + (sw - img_fit.width)  // 2
        py = sy + (sh - img_fit.height) // 2

        if shadow:
            # ground shadow (타원형, 바닥에 자연스러운)
            _paste_ground_shadow(canvas, img_fit, px, py, W, H)
        # 제품 합성
        canvas.paste(img_fit, (px, py), img_fit)

    return canvas.convert("RGB")


# ── 배경 제거 ─────────────────────────────────────────────────────────

def _remove_background(image_path: str) -> Optional[Image.Image]:
    """
    배경 제거 3단계 폴백:
      1. rembg AI  → 성공 여부 검증 → alpha matting 엣지 페더
      2. 엣지 플러드필 BFS (흰/회색 배경 전용)
      3. 원본 그대로 (alpha 포함)
    """
    # 1순위: rembg
    try:
        from rembg import remove as rembg_remove
        with open(image_path, "rb") as f:
            raw = f.read()
        out  = rembg_remove(raw)
        nobg = Image.open(io.BytesIO(out)).convert("RGBA")

        # 성공 여부: 실제로 알파 채널이 생겼는지 확인
        alpha_arr = nobg.split()[3]
        pixels    = list(alpha_arr.getdata())
        transparent_px = sum(1 for p in pixels if p < 128)
        total_px       = len(pixels)

        if transparent_px < total_px * 0.05:
            # 알파가 거의 없음 = rembg 가 배경 제거 못한 것
            print(f"[Compositor] rembg 배경 제거 미흡 ({transparent_px}/{total_px} 투명) → 플러드필 시도")
            raise ValueError("rembg insufficient alpha")

        # 엣지 페더링 (거친 가장자리 스무딩)
        nobg = _feather_edges(nobg, radius=2)
        print(f"[Compositor] rembg 성공: {Path(image_path).name} (투명 {transparent_px/total_px*100:.0f}%)")
        return nobg

    except Exception as e:
        if "insufficient" not in str(e):
            print(f"[Compositor] rembg 오류, 플러드필 시도: {e}")

    # 2순위: 플러드필
    try:
        from modules.image_processor import ImageProcessor
        img  = Image.open(image_path).convert("RGBA")
        nobg = ImageProcessor._flood_fill_background(img)
        nobg = _feather_edges(nobg, radius=2)
        print(f"[Compositor] 플러드필 배경 제거: {Path(image_path).name}")
        return nobg
    except Exception as e:
        print(f"[Compositor] 플러드필 실패, 원본 사용: {e}")

    # 3순위: 원본
    try:
        return Image.open(image_path).convert("RGBA")
    except Exception as e:
        print(f"[Compositor] 이미지 열기 실패: {e}")
        return None


def _feather_edges(img: Image.Image, radius: int = 2) -> Image.Image:
    """알파 채널 가장자리를 가우시안 블러로 부드럽게 처리."""
    try:
        r, g, b, a = img.split()
        a_smooth   = a.filter(ImageFilter.GaussianBlur(radius=radius))
        # 완전 불투명 영역은 그대로, 가장자리만 부드럽게
        # → 원본 alpha와 blur alpha의 최솟값 (침식 방지)
        import PIL.ImageChops as IC
        a_final = IC.darker(a, a_smooth)
        img2    = Image.merge("RGBA", (r, g, b, a_final))
        return img2
    except Exception:
        return img


# ── 그림자 ────────────────────────────────────────────────────────────

def _paste_ground_shadow(
    canvas:  Image.Image,  # RGBA
    product: Image.Image,  # RGBA
    px: int, py: int,
    W: int, H: int,
) -> None:
    """
    제품 바닥에 타원형 ground shadow를 그려 바닥에 붙어 있는 느낌을 줌.
    드롭 섀도우(상단 offset)와 달리 바로 아래에 납작한 타원으로 표현.
    """
    pw, ph     = product.size
    bot_y      = py + ph                         # 제품 하단 y
    ell_w      = int(pw * 0.80)
    ell_h      = int(ph * 0.06)
    ell_x      = px + (pw - ell_w) // 2
    ell_y      = bot_y - int(ell_h * 0.5)        # 제품과 살짝 겹치게

    # 타원형 마스크 레이어
    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sdraw        = ImageDraw.Draw(shadow_layer)
    sdraw.ellipse(
        [ell_x, ell_y, ell_x + ell_w, ell_y + ell_h],
        fill=(0, 0, 0, 90),
    )
    # 블러로 퍼지게
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=10))
    canvas.alpha_composite(shadow_layer)


# ── 레이아웃 계산 ─────────────────────────────────────────────────────

def _choose_layout(n: int) -> str:
    return {1: "single", 2: "row", 3: "triangle", 4: "grid2x2", 5: "grid2x3"}.get(n, "single")


def _compute_slots(layout: str, n: int, W: int, H: int) -> list[tuple[int, int, int, int]]:
    """
    (x, y, width, height) 슬롯 리스트.
    배치 영역: 상단 30% 여백 + 하단 60% 활용 (상단은 배경 노출).
    제품 스케일은 슬롯의 85%까지만 채움 (여백 일관성).
    """
    zone_top = int(H * 0.28)
    zone_h   = int(H * 0.62)
    margin   = int(W * 0.05)
    usable_w = W - margin * 2
    usable_h = zone_h

    if layout == "single":
        sw = int(usable_w * 0.70)
        sh = int(usable_h * 0.80)
        sx = margin + (usable_w - sw) // 2
        sy = zone_top + (usable_h - sh) // 2
        return [(sx, sy, sw, sh)]

    elif layout == "row":
        gap = int(usable_w * 0.04)
        sw  = (usable_w - gap) // 2
        sh  = int(usable_h * 0.80)
        sy  = zone_top + (usable_h - sh) // 2
        return [
            (margin,             sy, sw, sh),
            (margin + sw + gap,  sy, sw, sh),
        ]

    elif layout == "triangle":
        top_sw = int(usable_w * 0.52)
        top_sh = int(usable_h * 0.48)
        bot_sw = int(usable_w * 0.44)
        bot_sh = int(usable_h * 0.44)
        gap    = int(usable_w * 0.04)
        top_y  = zone_top
        bot_y  = zone_top + top_sh + int(H * 0.02)
        return [
            (margin + (usable_w - top_sw) // 2, top_y, top_sw, top_sh),
            (margin,                             bot_y, bot_sw, bot_sh),
            (margin + bot_sw + gap,              bot_y, bot_sw, bot_sh),
        ][:n]

    elif layout == "grid2x2":
        gap = int(usable_w * 0.03)
        sw  = (usable_w - gap) // 2
        sh  = (usable_h - gap) // 2
        return [
            (margin,             zone_top,              sw, sh),
            (margin + sw + gap,  zone_top,              sw, sh),
            (margin,             zone_top + sh + gap,   sw, sh),
            (margin + sw + gap,  zone_top + sh + gap,   sw, sh),
        ][:n]

    elif layout == "grid2x3":
        gap   = int(usable_w * 0.025)
        sw    = (usable_w - gap) // 2
        row_h = (usable_h - gap * 2) // 3
        slots = []
        for row in range(3):
            for col in range(2):
                sx = margin + col * (sw + gap)
                sy = zone_top + row * (row_h + gap)
                slots.append((sx, sy, sw, row_h))
        return slots[:n]

    return _compute_slots("single", 1, W, H)


# ── 합성 헬퍼 ─────────────────────────────────────────────────────────

def _fit_into(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """RGBA 이미지를 최대 크기 안에 비율 유지하며 축소 (슬롯의 85% 상한)."""
    target_w = int(max_w * 0.85)
    target_h = int(max_h * 0.85)
    copy = img.copy()
    copy.thumbnail((target_w, target_h), Image.LANCZOS)
    return copy
