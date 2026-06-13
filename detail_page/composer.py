"""
composer.py -상세페이지 이미지 자동 생성 진입점

공개 API:
    generate_detail_page(
        product_name:   str,
        specs:          dict,
        image_paths:    list[str],   # 최대 5장
        section_count:  int,
        api_key:        str,
        output_dir:     str | None,
        text_model:     str,
        use_composite:  bool,        # True=실사진 합성 모드 ON
        progress_cb:    Callable | None,
    ) -> str   # 완성된 긴 상세페이지 이미지 로컬 경로 (실패 시 "")

내부 흐름:
    1. section_director  -Gemini 텍스트 모델로 N섹션 프롬프트 + render_mode JSON 생성
    2. 섹션별 분기:
       render_mode == "ai_full":
           section_generator  → 레퍼런스 이미지 첨부 + Gemini 전체 생성
       render_mode == "composite":
           section_generator  → 배경/분위기만 생성
           photo_compositor   → 실사진 배경 제거 + AI 배경 위 합성
    3. text_overlay      -PIL로 한글 텍스트 오버레이
    4. 세로 이어붙이기   -N장 → 1장의 긴 상세페이지
    5. 로컬 저장         -output_dir / detail_page_{uid}.jpg
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image

from .section_director  import build_section_prompts
from .section_generator import generate_section_image, _CANVAS_W, _CANVAS_H
from .photo_compositor  import composite_product_photos
from .text_overlay      import apply_overlay, OverlayConfig


_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "images" / "detail_pages"


def generate_detail_page(
    product_name:  str,
    specs:         dict[str, str],
    image_paths:   list[str],        # 제품 사진 최대 5장
    section_count: int   = 4,
    api_key:       str   = "",
    output_dir:    Optional[str] = None,
    text_model:    str   = "gemini-2.5-flash",
    use_composite: bool  = False,    # False(기본) = 모든 섹션 ai_full, True = composite 허용
    progress_cb    = None,
) -> str:
    """
    상세페이지 이미지 1장 생성 → 로컬 경로 반환.

    Args:
        product_name:   상품명
        specs:          스펙/속성 dict (없으면 빈 dict)
        image_paths:    제품 이미지 로컬 경로 리스트 (최대 5장)
                        ai_full 섹션의 레퍼런스, composite 섹션의 합성 소스로 사용
        section_count:  생성할 섹션 수 (1 이상, 상한 없음)
        api_key:        Gemini API 키 (Settings.GEMINI_API_KEY)
        output_dir:     저장 디렉터리 (None이면 data/images/detail_pages/)
        text_model:     섹션 프롬프트 생성용 텍스트 모델
        use_composite:  True = composite 섹션에 실사진 합성 (기본값)
                        False = 모든 섹션을 Gemini 전체 생성
        progress_cb:    fn(current, total, message) 진행상황 콜백

    Returns:
        저장된 이미지 파일 경로. 실패 시 빈 문자열.
    """
    if not api_key:
        print("[DetailPage] Gemini API 키 없음 -생성 불가")
        return ""

    section_count = max(1, section_count)   # 상한 없음 — 분할 조각 수 그대로 사용
    out_dir       = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    valid_images  = [p for p in image_paths if p and os.path.isfile(p)]
    total_steps   = section_count + 2  # 디렉팅 1 + 섹션 N + 합성 1

    def _progress(step: int, msg: str):
        if progress_cb:
            try:
                progress_cb(step, total_steps, msg)
            except Exception:
                pass
        print(f"[DetailPage] ({step}/{total_steps}) {msg}")

    # ── Step 1: 섹션 프롬프트 생성 ──────────────────────────────────
    _progress(1, "섹션 구성 중 (Gemini 텍스트)...")
    section_plans = build_section_prompts(
        product_name  = product_name,
        specs         = specs,
        section_count = section_count,
        api_key       = api_key,
        model         = text_model,
        use_composite = use_composite,
    )

    if not section_plans:
        print("[DetailPage] 섹션 프롬프트 생성 실패 -기본 플레이스홀더로 대체")
        section_plans = _make_fallback_plans(product_name, section_count, use_composite)

    # ── Step 2~N+1: 섹션별 이미지 생성 ─────────────────────────────
    section_images: list[Image.Image] = []
    for i, plan in enumerate(section_plans):
        step_no     = i + 2
        stype       = plan.get("section_type", f"section_{i}")
        render_mode = plan.get("render_mode", "ai_full")

        _progress(
            step_no,
            f"섹션 {i+1}/{len(section_plans)} -{stype} "
            f"({'AI 전체 생성' if render_mode == 'ai_full' else '실사진 합성'})...",
        )

        # ── 이미지 생성 ──────────────────────────────────────────────
        img = generate_section_image(
            image_prompt        = plan.get("image_prompt", ""),
            product_image_paths = valid_images,
            api_key             = api_key,
            section_index       = i,
            render_mode         = render_mode,
        )

        if img is None:
            img = Image.new("RGB", (_CANVAS_W, _CANVAS_H), (245, 245, 245))

        # ── composite 모드: 실사진 배치 ─────────────────────────────
        if render_mode == "composite" and valid_images:
            _progress(step_no, f"섹션 {i+1}/{len(section_plans)} -제품 사진 합성 중...")
            img = composite_product_photos(
                background    = img,
                product_paths = valid_images,
                layout        = plan.get("layout", "auto") if plan.get("layout") != "full" else "auto",
                shadow        = True,
            )

        # ── 한글 텍스트 오버레이 (PIL) ───────────────────────────────
        ov_raw = plan.get("overlay_texts", {})
        cfg    = OverlayConfig(
            title    = str(ov_raw.get("title",    "") or ""),
            subtitle = str(ov_raw.get("subtitle", "") or ""),
            body     = str(ov_raw.get("body",     "") or ""),
            layout   = str(plan.get("layout",     "full")),
        )
        if cfg.title or cfg.subtitle or cfg.body:
            img = apply_overlay(img, cfg, section_type=stype)

        section_images.append(img)

    if not section_images:
        print("[DetailPage] 생성된 섹션 이미지 없음")
        return ""

    # ── Step N+2: 세로 이어붙이기 ────────────────────────────────────
    _progress(total_steps, "섹션 합성 중...")
    final_img = _stack_vertical(section_images)

    # ── 저장 ─────────────────────────────────────────────────────────
    uid   = uuid.uuid4().hex[:10]
    fname = f"detail_page_{uid}.jpg"
    fpath = out_dir / fname
    final_img.save(str(fpath), "JPEG", quality=92, subsampling=0)
    print(f"[DetailPage] 완성: {fpath}  ({final_img.size[0]}×{final_img.size[1]}px)")
    return str(fpath)


# ── 헬퍼 ─────────────────────────────────────────────────────────────

def _stack_vertical(images: list[Image.Image]) -> Image.Image:
    """여러 이미지를 세로로 이어붙여 하나의 긴 이미지로 합성."""
    W       = max(img.width  for img in images)
    H_total = sum(img.height for img in images)
    canvas  = Image.new("RGB", (W, H_total), (255, 255, 255))
    y_off   = 0
    for img in images:
        if img.width != W:
            img = img.resize((W, int(img.height * W / img.width)), Image.LANCZOS)
        canvas.paste(img, (0, y_off))
        y_off += img.height
    return canvas


def _make_fallback_plans(
    product_name: str, section_count: int, use_composite: bool
) -> list[dict]:
    """섹션 디렉팅 실패 시 사용할 기본 플레이스홀더 플랜."""
    types_   = ["hero", "features", "specs", "usage", "callout", "closing"]
    from .section_director import _RENDER_MODE
    return [
        {
            "section_type": types_[i % len(types_)],
            "render_mode":  (
                _RENDER_MODE.get(types_[i % len(types_)], "ai_full")
                if use_composite else "ai_full"
            ),
            "image_prompt": "Clean studio backdrop with soft natural light",
            "overlay_texts": {
                "title":    product_name[:8],
                "subtitle": "",
                "body":     "",
            },
            "layout": "auto",
        }
        for i in range(section_count)
    ]
