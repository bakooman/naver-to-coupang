"""
section_generator.py — Gemini 이미지 모델로 섹션별 이미지 생성

모델: gemini-3-pro-image  (GA, 나노바나나 프로)
폴백: gemini-3.1-flash-image  (API 실패 시에만)

render_mode 별 동작:
  ai_full   : 제품 레퍼런스 이미지(들)를 첨부해 full 이미지 생성
  composite : 배경·분위기만 생성 (제품 묘사 프롬프트 없음 + 레퍼런스 미첨부)
              → photo_compositor.py가 실사진을 합성

- 9:16 세로형 (쿠팡 상세페이지 권장: 780×1386)
- 생성 실패 시 흰 배경 플레이스홀더 반환 (파이프라인 중단 없음)
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from PIL import Image

_PRIMARY_MODEL  = "gemini-3-pro-image"
_FALLBACK_MODEL = "gemini-3.1-flash-image"

# 쿠팡 상세페이지 권장 비율 9:16, 너비 780px 기준
_CANVAS_W = 780
_CANVAS_H = 1386   # 780 × (16/9) ≈ 1386

# ai_full 모드에서 첨부할 최대 레퍼런스 이미지 수
_MAX_REF_IMAGES = 5


def generate_section_image(
    image_prompt:        str,
    product_image_paths: list[str],   # ← 단일 → 다중으로 변경
    api_key:             str,
    section_index:       int  = 0,
    render_mode:         str  = "ai_full",   # "ai_full" | "composite"
) -> Optional[Image.Image]:
    """
    Gemini 이미지 모델로 섹션 이미지 1장 생성.

    render_mode == "ai_full":
        - product_image_paths 의 이미지를 레퍼런스로 첨부 (최대 5장)
        - Gemini가 배경+제품 전체 생성

    render_mode == "composite":
        - product_image_paths 미사용 (레퍼런스 첨부 안 함)
        - image_prompt는 배경/분위기 묘사만
        - 반환된 이미지 위에 photo_compositor.py가 실사진을 합성

    Returns:
        PIL Image (RGB) 또는 None (완전 실패 시)
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[SectionGenerator] google-genai 패키지 없음")
        return _make_placeholder(section_index)

    if render_mode == "composite":
        return _generate_background_only(image_prompt, api_key, section_index)
    else:
        return _generate_full(image_prompt, product_image_paths, api_key, section_index)


# 전 섹션 공통 스타일 앵커 (section_director 와 동일 문구)
_STYLE_ANCHOR = (
    "Consistent visual style: professional product photography, "
    "soft diffused studio lighting, neutral warm-white or light grey background, "
    "clean modern aesthetic, high-end commercial shoot quality, "
    "same color temperature and mood as other sections. "
    "No text or typography in the image. "
    "9:16 vertical format."
)

# ai_full 에서 첨부할 최적 레퍼런스 이미지 수 (1~2장만)
_MAX_REF_IMAGES_AI = 2


def _select_best_refs(paths: list[str], n: int = _MAX_REF_IMAGES_AI) -> list[str]:
    """
    가장 '깨끗한' 레퍼런스 이미지 n장 선택.
    기준: 파일 크기 내림차순 (정보량이 많은 고화질 사진 우선).
    """
    valid = [p for p in paths if p and Path(p).is_file()]
    if not valid:
        return []
    valid.sort(key=lambda p: Path(p).stat().st_size, reverse=True)
    return valid[:n]


def _generate_full(
    image_prompt:        str,
    product_image_paths: list[str],
    api_key:             str,
    section_index:       int,
) -> Optional[Image.Image]:
    """
    ai_full 모드: 가장 깨끗한 레퍼런스 1~2장 첨부 + 전체 이미지 생성.
    전 섹션 공통 스타일 앵커를 프롬프트 앞에 삽입해 톤/조명 일관성 유지.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None

    # 최적 레퍼런스 1~2장 선별 (파일 크기 기준)
    best_refs = _select_best_refs(product_image_paths)
    attached  = 0
    parts     = []

    for path in best_refs:
        try:
            mime = _mime_of(path)
            with open(path, "rb") as f:
                img_bytes = f.read()
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
            attached += 1
        except Exception as e:
            print(f"[SectionGenerator] 레퍼런스 이미지 로드 실패 ({path}): {e}")

    if attached:
        parts.append(types.Part.from_text(
            text=(
                f"REFERENCE: The above {attached} image(s) show the EXACT product to generate. "
                "You MUST reproduce the product's precise shape, color, logo, and design in every detail. "
                "Do NOT simplify, redesign, or alter the product appearance in any way."
            )
        ))

    full_prompt = (
        f"[QUALITY STANDARD] {_STYLE_ANCHOR}\n\n"
        "[TASK] Create a professional 9:16 vertical product detail page section image "
        "for Korean e-commerce at the same quality level as the finest hero section images. "
        "This is NOT a lower-priority section — apply identical rendering quality, "
        "lighting precision, and background detail regardless of section type.\n\n"
        "[PRODUCT ACCURACY] The product must appear exactly as in the reference photo(s): "
        "same shape, color, proportions, and logo. No simplification allowed.\n\n"
        f"[SECTION DIRECTION] {image_prompt}"
    )

    parts.append(types.Part.from_text(text=full_prompt))

    result = _call_image_model(_PRIMARY_MODEL, parts, api_key, section_index)
    if result is not None:
        return result

    print(f"[SectionGenerator] 섹션 {section_index}: {_PRIMARY_MODEL} 실패 → {_FALLBACK_MODEL} 폴백")
    result = _call_image_model(_FALLBACK_MODEL, parts, api_key, section_index)
    if result is not None:
        return result

    print(f"[SectionGenerator] 섹션 {section_index}: 이미지 생성 완전 실패 → 플레이스홀더 사용")
    return _make_placeholder(section_index)


def _generate_background_only(
    image_prompt: str,
    api_key:      str,
    section_index: int,
) -> Optional[Image.Image]:
    """
    composite 모드: 배경/분위기만 생성, 제품 없음.
    레퍼런스 이미지를 첨부하지 않아 AI가 임의로 제품을 그리지 않음.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None

    bg_prompt = (
        "Create a clean studio-style background for a Korean e-commerce product detail page section. "
        "STRICT RULES: "
        "1. NO products, objects, props, or any items — completely empty scene. "
        "2. Use a simple studio backdrop: soft gradient, neutral solid color, "
        "   or very subtle texture (linen, concrete, paper). "
        "3. Neutral tones: white, off-white, light grey, cream, or muted pastel. "
        "4. Soft even lighting — no dramatic shadows or highlights. "
        "5. Leave the center 60% of the image as open empty space where "
        "   product photos will be composited later. "
        "6. Do NOT generate a lifestyle, outdoor, kitchen, or any realistic scene. "
        "7. No text. 9:16 vertical format. "
        f"Color mood hint: {image_prompt}"
    )

    parts = [types.Part.from_text(text=bg_prompt)]

    result = _call_image_model(_PRIMARY_MODEL, parts, api_key, section_index)
    if result is not None:
        return result

    print(f"[SectionGenerator] 섹션 {section_index} (배경): {_PRIMARY_MODEL} 실패 → {_FALLBACK_MODEL} 폴백")
    result = _call_image_model(_FALLBACK_MODEL, parts, api_key, section_index)
    if result is not None:
        return result

    # 배경 생성 실패 시 → 그라디언트 플레이스홀더
    print(f"[SectionGenerator] 섹션 {section_index}: 배경 생성 실패 → 플레이스홀더")
    return _make_gradient_bg(section_index)


def _call_image_model(
    model:         str,
    parts:         list,
    api_key:       str,
    section_index: int,
) -> Optional[Image.Image]:
    """Gemini 이미지 모델 API 호출 → PIL Image 반환. 실패 시 None."""
    try:
        from google import genai
        from google.genai import types

        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model    = model,
            contents = parts,
            config   = types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        for part in (
            response.candidates[0].content.parts
            if response.candidates else []
        ):
            if hasattr(part, "inline_data") and part.inline_data:
                img  = Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
                img  = _resize_to_canvas(img)
                print(f"[SectionGenerator] 섹션 {section_index}: {model} 완료 {img.size}")
                return img

        print(f"[SectionGenerator] 섹션 {section_index}: {model} 응답에 이미지 없음")
        return None

    except Exception as e:
        print(f"[SectionGenerator] 섹션 {section_index}: {model} 오류 — {e}")
        return None


def _resize_to_canvas(img: Image.Image) -> Image.Image:
    """이미지를 780×1386 (9:16) 캔버스에 비율 유지하며 맞춤 (레터박스)."""
    canvas = Image.new("RGB", (_CANVAS_W, _CANVAS_H), (255, 255, 255))
    img.thumbnail((_CANVAS_W, _CANVAS_H), Image.LANCZOS)
    x = (_CANVAS_W - img.width)  // 2
    y = (_CANVAS_H - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


def _make_placeholder(section_index: int) -> Image.Image:
    """이미지 생성 실패 시 밝은 회색 플레이스홀더."""
    return Image.new("RGB", (_CANVAS_W, _CANVAS_H), (245, 245, 245))


def _make_gradient_bg(section_index: int) -> Image.Image:
    """배경 생성 실패 시 부드러운 그라디언트 배경."""
    # 밝은 크림→흰색 그라디언트
    palettes = [
        ((245, 240, 230), (255, 255, 255)),   # 따뜻한 크림
        ((230, 240, 245), (255, 255, 255)),   # 차가운 스카이
        ((235, 245, 235), (255, 255, 255)),   # 민트 그린
    ]
    top_c, bot_c = palettes[section_index % len(palettes)]
    img  = Image.new("RGB", (_CANVAS_W, _CANVAS_H))
    data = []
    for y in range(_CANVAS_H):
        r = int(top_c[0] + (bot_c[0] - top_c[0]) * y / _CANVAS_H)
        g = int(top_c[1] + (bot_c[1] - top_c[1]) * y / _CANVAS_H)
        b = int(top_c[2] + (bot_c[2] - top_c[2]) * y / _CANVAS_H)
        data.extend([(r, g, b)] * _CANVAS_W)
    img.putdata(data)
    return img


def _mime_of(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):  return "image/png"
    if p.endswith(".webp"): return "image/webp"
    return "image/jpeg"
