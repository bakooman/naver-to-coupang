"""
reference_composer.py — 실제 스마트스토어 상세페이지 이미지를 참고해 완전히 새로 그린
고가상품 전용 상세페이지 생성 (레퍼런스 이미지 1장 = 섹션 1개).

카처 상품 테스트 때 퀄리티가 낮고 "합성 티"가 났던 원인은 section_director.py가
상품명+스펙 텍스트만으로 섹션 구도를 상상해서 만드는 구조였기 때문 — 실제 레퍼런스
이미지를 구도/그래픽 재현에 쓰지 않았다.

이 모듈은 다르게 접근한다:
  1. image_reader.read_product_info_from_images() — 레퍼런스 이미지에서 실제로
     읽히는 문구/스펙만 추출 (할루시네이션 방지 이미 내장된 기존 함수 재사용)
  2. Gemini 텍스트 모델로 문구 리라이트 — 같은 정보, 다른 표현 ("말만 조금 바꿔서")
  3. Gemini 이미지 모델로 그 섹션을 완전히 새로 그림 — 레퍼런스 이미지를 구도/분위기
     앵커로 첨부하되, 문단 텍스트는 이미지 안에 절대 굽지 않도록 강제
     (새 한글 문단을 이미지 모델이 직접 렌더링하다 깨지는 리스크 원천 차단)
  4. text_overlay.apply_overlay() — 검증된 PIL 렌더러로 리라이트된 문구를 얹음
  5. 전체 섹션을 세로로 이어붙여 완성

지적재산권 리스크 방지: 실제 이미지를 그대로/거의 그대로 재사용하지 않고,
매 섹션을 새 이미지로 재생성한다 (Option B).
"""
from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image

from .composer import _stack_vertical
from .image_reader import read_product_info_from_images
from .section_generator import _CANVAS_W, _CANVAS_H, _resize_to_canvas, _mime_of
from .text_overlay import apply_overlay, OverlayConfig

_PRIMARY_MODEL  = "gemini-3-pro-image"
_FALLBACK_MODEL = "gemini-3.1-flash-image"

_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "images" / "detail_pages"

# 신규 카피에도 동일 적용하는 압축된 금지표현 목록
# (modules/gemini_writer.py의 전체 14개 항목 중 상세페이지 카피에 가장 흔히
#  걸리는 핵심 카테고리만 압축 — 전체 목록은 판매멘트 생성 시 별도 검수됨)
_BANNED_PHRASES_BLOCK = """[절대 금지 표현 — 신규 문구에도 동일 적용]
- 순위·과장: 1위, 최고, 최초, 최대, 가장, 유일한, 독보적, 혁신적, 완벽, 무조건, 특효
- 의학적 오인: 치료, 완치, 효과 보장, 부작용 없음, 임상 증명·완료, 질병명(당뇨/고혈압/암/아토피 등) 언급
- 인증 없는 표현: FDA 인증·승인, 특허(출원 중인데 "특허"로 표기), 친환경·무독성(인증 없이)
- 영양성분 강조: 무설탕·저칼로리·무첨가·저나트륨 등 식약처 인증 없이 강조 금지
- 유통사·수입사·판매처·소싱처·공급원 등 유통 관련 정보 절대 언급 금지"""


def generate_detail_page_from_reference(
    reference_image_paths: list[str],
    product_name: str,
    api_key: str,
    output_dir: Optional[str] = None,
    text_model: str = "gemini-2.5-flash",
    progress_cb=None,
) -> str:
    """
    레퍼런스 이미지들(실제 스마트스토어 상세페이지 캡처)을 참고해 완전히 새로운
    상세페이지 이미지를 생성. 이미지 1장 = 섹션 1개로 그대로 매칭.

    Returns:
        저장된 이미지 파일 경로. 실패 시 빈 문자열.
    """
    if not api_key:
        print("[RefComposer] Gemini API 키 없음 — 생성 불가")
        return ""

    valid_paths = [p for p in reference_image_paths if p and Path(p).is_file()]
    if not valid_paths:
        print("[RefComposer] 유효한 레퍼런스 이미지 없음")
        return ""

    out_dir = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    total_steps = len(valid_paths) + 1

    def _progress(step: int, msg: str):
        if progress_cb:
            try:
                progress_cb(step, total_steps, msg)
            except Exception:
                pass
        print(f"[RefComposer] ({step}/{total_steps}) {msg}")

    # 상품 성격에 맞는 디자인 스타일(폰트체·포인트컬러) 1회 결정 —
    # 전 섹션에 동일 적용해 한 페이지 안에서 통일감 유지
    style = _pick_style(product_name, api_key, text_model)
    print(f"[RefComposer] 디자인 스타일: {style}")

    section_images: list[Image.Image] = []

    for i, ref_path in enumerate(valid_paths):
        step_no = i + 1
        _progress(step_no, f"섹션 {step_no}/{len(valid_paths)} — 원본 내용 확인 중...")

        # ── 1. 이 섹션의 실제 문구/스펙 추출 (기존 함수, 할루시네이션 방지 내장) ──
        info = read_product_info_from_images([ref_path], api_key, model=text_model)
        source_facts = _format_facts(info)

        # ── 2. 문구 리라이트 (같은 정보, 다른 표현) — section_type도 함께 판단 ──
        _progress(step_no, f"섹션 {step_no}/{len(valid_paths)} — 문구 재구성 중...")
        rewritten = _rewrite_copy(product_name, source_facts, api_key, text_model)
        stype = rewritten.get("section_type", "hero")

        # ── 3. 이미지 재생성 (레퍼런스 앵커, 문단 텍스트 금지) ──────────
        # specs/features(아이콘·다이어그램·표 위주)는 이미지 모델이 새로 그리다
        # 텍스트가 깨지므로, 배경만 단순하게 생성하고 정보는 전부 PIL로만 얹는다.
        _progress(step_no, f"섹션 {step_no}/{len(valid_paths)} — 이미지 재생성 중...")
        img = _regenerate_section_image(ref_path, api_key, stype)
        if img is None:
            img = Image.new("RGB", (_CANVAS_W, _CANVAS_H), (245, 245, 245))

        # ── 4. 리라이트된 문구를 검증된 PIL 렌더러로 얹기 ───────────────
        cfg = OverlayConfig(
            title    = rewritten.get("title", ""),
            subtitle = rewritten.get("subtitle", ""),
            body     = rewritten.get("body", ""),
            layout   = "full",
            style    = style,
        )
        if cfg.title or cfg.subtitle or cfg.body:
            img = apply_overlay(img, cfg, section_type=stype)

        section_images.append(img)

    if not section_images:
        print("[RefComposer] 생성된 섹션 이미지 없음")
        return ""

    _progress(total_steps, "섹션 합성 중...")
    final_img = _stack_vertical(section_images)

    uid   = uuid.uuid4().hex[:10]
    fname = f"detail_page_ref_{uid}.jpg"
    fpath = out_dir / fname
    final_img.save(str(fpath), "JPEG", quality=92, subsampling=0)
    print(f"[RefComposer] 완성: {fpath} ({final_img.size[0]}x{final_img.size[1]}px)")
    return str(fpath)


def _format_facts(info: dict) -> str:
    """image_reader 추출 결과를 프롬프트용 텍스트로 정리."""
    lines = []
    if info.get("product_name"):
        lines.append(f"상품명: {info['product_name']}")
    for k, v in (info.get("specs") or {}).items():
        lines.append(f"- {k}: {v}")
    for d in info.get("descriptions") or []:
        lines.append(f"- {d}")
    return "\n".join(lines) if lines else "(이 섹션에서 읽힌 텍스트 없음)"


_VALID_STYLES = ("premium", "modern", "playful", "tech")


def _pick_style(product_name: str, api_key: str, model: str) -> str:
    """
    상품 성격에 맞는 디자인 스타일(폰트체·포인트컬러 조합) 선택.
    실패 시 "modern"(무난한 기본값) 반환 — 파이프라인 안 죽음.
    """
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        prompt = f"""아래 상품에 가장 잘 어울리는 상세페이지 디자인 스타일을 하나만 고르세요.

- premium: 고급스러운 느낌 (명조체·골드 포인트) — 프리미엄 가전, 명품, 건강기능식품 등
- modern: 모던하고 심플한 느낌 (둥근 산세리프·블루 포인트) — IT기기, 생활가전, 일반 잡화
- playful: 발랄하고 귀여운 느낌 (굵은 산세리프·코랄 포인트) — 캐릭터상품, 간식, 유아동 용품
- tech: 파워풀하고 테크니컬한 느낌 (고딕·레드 포인트) — 공구, 전자기기, 스펙 강조 상품

[상품명]
{product_name}

스타일 이름 하나만 답하세요 (premium/modern/playful/tech). 설명 금지."""
        resp = client.models.generate_content(model=model, contents=prompt)
        picked = (resp.text or "").strip().lower().split()[0] if (resp.text or "").strip() else ""
        picked = "".join(ch for ch in picked if ch.isalpha())
        return picked if picked in _VALID_STYLES else "modern"
    except Exception as e:
        print(f"[RefComposer] 스타일 선택 실패 (기본값 modern 사용): {e}")
        return "modern"


def _rewrite_copy(product_name: str, source_facts: str, api_key: str, model: str) -> dict:
    """
    원본 섹션에서 뽑은 실제 문구/스펙을 바탕으로, 같은 정보를 다른 표현으로
    재구성. 섹션 레이아웃 타입도 함께 추론.

    실패 시 안전한 기본값(빈 문구, hero 레이아웃) 반환 — 파이프라인 안 죽음.
    """
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        prompt = f"""당신은 쿠팡 상세페이지 카피라이터입니다.
아래는 원본 상세페이지 한 섹션에서 실제로 읽힌 내용입니다. 이 정보(사실관계)는
절대 바꾸지 말고, 표현·문장 구조만 다르게 재구성해 새 문구를 만드세요.

[규칙]
1. 원본에 없는 수치·효능·기능을 새로 지어내지 마세요.
2. 원본에 있는 사실(수치, 스펙, 기능 설명)은 반드시 그대로 유지하되 문장만 새로 쓰세요.
3. 원본 내용이 비어있거나 의미없으면 title/subtitle/body를 빈 문자열로 반환하세요.
{_BANNED_PHRASES_BLOCK}

[section_type 판단 기준 — 하나만 선택]
- "hero": 제품 대표 이미지 + 핵심 한 줄 소개
- "features": 주요 특징 여러 개 나열
- "specs": 스펙/수치/구성 정보 (표 형태 등)
- "callout": 강조 포인트 클로즈업, 짧고 임팩트 있는 문구
- "closing": 마무리, 브랜드 감성

[원본 상품명]
{product_name}

[이 섹션 원본 내용]
{source_facts}

[출력 — JSON만, 다른 텍스트 없음]
{{
  "section_type": "hero|features|specs|callout|closing",
  "title": "3~10자 핵심 문구 (한글, 없으면 빈 문자열)",
  "subtitle": "10~25자 보조 문구 (한글, 없으면 빈 문자열)",
  "body": "본문 2~4줄 (한글, 줄바꿈은 \\n, 없으면 빈 문자열)"
}}"""
        response = client.models.generate_content(
            model=model, contents=[types.Part.from_text(text=prompt)]
        )
        raw = (response.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        return {
            "section_type": str(data.get("section_type", "hero") or "hero"),
            "title":        str(data.get("title", "") or ""),
            "subtitle":     str(data.get("subtitle", "") or ""),
            "body":         str(data.get("body", "") or ""),
        }
    except Exception as e:
        print(f"[RefComposer] 문구 리라이트 실패: {e}")
        return {"section_type": "hero", "title": "", "subtitle": "", "body": ""}


# 사진 위주 섹션(hero/callout/closing) — 장면 전체를 새로 그림.
# 실제로 텍스트 없는 제품사진 재현은 이미지 모델이 안정적으로 잘 해낸다(검증됨).
_REFERENCE_STYLE_ANCHOR = (
    "REFERENCE IMAGE: The attached image is a section from a real product detail "
    "page. Use it ONLY as a style/composition/mood reference — recreate a brand-new "
    "image inspired by its layout, color palette, and photographic style, but do NOT "
    "reproduce it verbatim (this must be an original image, not a copy or lightly "
    "edited version of the reference). "
    "CRITICAL: Do NOT render any paragraph text, bullet points, or captions into the "
    "image — the image must be completely free of body text. All wording will be "
    "added separately afterward. If the reference contains diagrams or icons, you may "
    "redraw similar (not identical) supporting graphics, but no readable paragraph "
    "text of any kind. "
    "The background/tone should look visibly different from the reference (different "
    "color scheme or lighting), not like a lightly retouched copy of the original."
)

# 정보 위주 섹션(features/specs) — 아이콘·다이어그램·표를 이미지 모델이 다시
# 그리게 하면 텍스트가 깨져서 아예 배경만 생성하고, 실제 정보는 전부 PIL
# text_overlay로만 얹는다 (신뢰도 100%). 제품/도표/아이콘/텍스트 전부 금지.
_BACKGROUND_ONLY_PROMPT = (
    "Create a clean, elegant abstract background for a Korean e-commerce product "
    "detail page section (9:16 vertical). "
    "STRICT RULES:\n"
    "1. NO products, objects, icons, diagrams, charts, tables, or any recognizable "
    "shapes — completely empty scene.\n"
    "2. NO text, letters, numbers, or symbols of any kind.\n"
    "3. Simple studio backdrop: soft gradient, neutral solid color, or very subtle "
    "texture (paper, fabric, soft light).\n"
    "4. Tone should complement (but look distinct from) the reference image's color "
    "palette — do not copy it exactly.\n"
    "5. Soft even lighting, high-end commercial aesthetic, no dramatic shadows.\n"
    "This background will have text and graphics added separately afterward — it "
    "must stay completely empty and uncluttered."
)


def _regenerate_section_image(
    ref_path: str, api_key: str, section_type: str = "hero"
) -> Optional[Image.Image]:
    """
    레퍼런스 이미지를 앵커로 삼아 새 섹션 이미지를 생성.

    section_type이 features/specs(정보·다이어그램 위주)면 배경만 단순하게
    생성 — 이미지 모델이 표·아이콘·텍스트를 재현하려다 깨지는 것을 원천 차단.
    나머지(hero/callout/closing, 사진 위주)는 장면 전체를 새로 그림.
    """
    try:
        from google.genai import types
    except ImportError:
        return None

    info_heavy = section_type in ("features", "specs")

    try:
        mime = _mime_of(ref_path)
        with open(ref_path, "rb") as f:
            img_bytes = f.read()
        if info_heavy:
            parts = [
                types.Part.from_bytes(data=img_bytes, mime_type=mime),
                types.Part.from_text(text=_BACKGROUND_ONLY_PROMPT),
            ]
        else:
            parts = [
                types.Part.from_bytes(data=img_bytes, mime_type=mime),
                types.Part.from_text(text=_REFERENCE_STYLE_ANCHOR),
                types.Part.from_text(text=(
                    "Create a professional 9:16 vertical product detail page section "
                    "image for Korean e-commerce, high-end commercial photography "
                    "quality, soft studio lighting. No text of any kind in the image."
                )),
            ]
    except Exception as e:
        print(f"[RefComposer] 레퍼런스 이미지 로드 실패: {e}")
        return None

    img = _call_image_model(_PRIMARY_MODEL, parts, api_key)
    if img is None:
        print(f"[RefComposer] {_PRIMARY_MODEL} 실패 → {_FALLBACK_MODEL} 폴백")
        img = _call_image_model(_FALLBACK_MODEL, parts, api_key)
    return img


def _call_image_model(model: str, parts: list, api_key: str) -> Optional[Image.Image]:
    try:
        from google import genai
        from google.genai import types

        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model    = model,
            contents = parts,
            config   = types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
        )
        for part in (response.candidates[0].content.parts if response.candidates else []):
            if hasattr(part, "inline_data") and part.inline_data:
                img = Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
                return _resize_to_canvas(img)
        return None
    except Exception as e:
        print(f"[RefComposer] {model} 오류: {e}")
        return None
