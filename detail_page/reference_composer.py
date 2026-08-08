"""
reference_composer.py — 실제 스마트스토어 상세페이지 이미지를 참고해 완전히 새로 그린
고가상품 전용 상세페이지 생성 (레퍼런스 이미지 1장 = 섹션 1개, 순서대로 처리).

이전 버전은 "문구 추출 → 리라이트 → (섹션타입별 분기) 이미지 재생성 → PIL 오버레이"
4단계 파이프라인이었으나, 섹션타입 분류가 부정확해 정보그래픽 텍스트가 깨지거나
반대로 제품사진 없이 빈 배경만 나오는 문제가 있었다.

실제로 사용자가 Gemini 이미지 생성에 원본 섹션 이미지를 한 장씩 던지며 프롬프트를
다듬어본 결과, "문구를 한 글자씩 그대로 베끼지 말고 자연스럽게 다른 표현으로 바꿔서
이미지 모델이 직접 텍스트까지 그려 넣게" 하는 단일 호출 방식이 훨씬 안정적이었다.
이 모듈은 그 방식을 그대로 코드화한다 — 섹션마다 Gemini 이미지 모델 호출 1번으로
완결(별도 문구 추출/리라이트/오버레이 단계 없음).

지적재산권 리스크 방지: 제품은 같은 것으로 알아볼 수 있게 유지하되, 사진을 그대로
재사용하지 않고 글씨체·배경·각도·조명을 다르게 해서 매 섹션을 새 이미지로 생성한다.
"""
from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image

from .composer import _stack_vertical
from .section_generator import _CANVAS_W, _CANVAS_H
from .text_overlay import overlay_body_text, append_disclaimer

_DISCLAIMER_TEXT = "본 상세페이지의 제품 이미지는 이해를 돕기 위한 것으로 실제 상품과 다소 차이가 있을 수 있습니다."

_PRIMARY_MODEL  = "gemini-3-pro-image"
_FALLBACK_MODEL = "gemini-3.1-flash-image"

# 다른 스토어 원본처럼 여러 내용이 세로로 길게 뭉쳐있는 레퍼런스를 그대로
# 고정 9:16(780×1386) 캔버스에 욱여넣으면 내용이 크게 잘려나가던 문제 —
# 레퍼런스 자체의 세로 비율만큼 출력 캔버스도 키운다 (상한 있음).
_MAX_CANVAS_H = _CANVAS_H * 3


def _target_size(ref_path: str) -> tuple[int, int]:
    try:
        with Image.open(ref_path) as im:
            rw, rh = im.size
        if not rw:
            return _CANVAS_W, _CANVAS_H
        h = round(_CANVAS_W * (rh / rw))
        h = max(_CANVAS_H, min(h, _MAX_CANVAS_H))
        return _CANVAS_W, h
    except Exception:
        return _CANVAS_W, _CANVAS_H


def _resize_to_size(img: Image.Image, w: int, h: int) -> Image.Image:
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    img = img.copy()
    img.thumbnail((w, h), Image.LANCZOS)
    x = (w - img.width)  // 2
    y = (h - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


# 다른 스토어에서 올린 원본 중 극단적으로 세로가 긴 이미지(예: 848×20541,
# 24:1 비율)를 그대로 첨부하면 gemini-3-pro-image / gemini-3.1-flash-image
# 둘 다 400 INVALID_ARGUMENT로 거부하는 것을 실제로 확인함 — 참조용으로
# 보낼 때는 비율은 유지한 채 최장변만 안전한 크기로 눌러서 보낸다.
_MAX_REF_DIM = 5000


def _load_ref_bytes(ref_path: str) -> tuple[bytes, str]:
    with Image.open(ref_path) as im:
        im = im.convert("RGB")
        if max(im.size) > _MAX_REF_DIM:
            im.thumbnail((_MAX_REF_DIM, _MAX_REF_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue(), "image/jpeg"

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

# 사용자가 실제 Gemini 이미지 생성으로 검증한 프롬프트를 코드화. 핵심은 규칙 3 —
# "한 글자씩 그대로 베끼지 말 것". 처음엔 원본을 너무 충실히 따라 하려다 보니 오타가
# 심했고, 이 지시를 추가한 뒤로 품질이 크게 개선됨(사용자 확인).
#
# 규칙 2(짧은 텍스트만 이미지에 직접) — 실사용 테스트에서 제목/라벨/숫자(10자 이내)는
# 이미지 모델이 정확히 그려내지만, 후기 인용구·설명문처럼 문장이 길어지면 글자가
# 심하게 깨지는 것을 확인함(예: "균형"→"균혈", "필터를 수평으로"→"릴터를 수렁으로").
# 그래서 긴 본문은 이미지에 굽지 않고 별도 텍스트로 받아 오타 0% PIL 렌더러
# (text_overlay.overlay_body_text)로 얹는다.
_GENERATION_PROMPT_TMPL = """당신은 쿠팡 상세페이지 이미지를 새로 만드는 디자이너입니다.

첨부한 이미지는 실제 상품 상세페이지의 한 섹션입니다. 이 이미지를 참고해서 아래 조건에 맞는
새 이미지를 만들어 주세요.

[규칙]
1. 상품 자체는 원본과 같은 제품으로 알아볼 수 있게 유지하되, 사진을 그대로 베끼지 말고
   글씨체·배경·각도·조명을 원본과 다르게 해서 완전히 새로 그린 이미지를 만드세요.
   (지적재산권 문제 없는 원본 이미지여야 합니다)
2. 단, 레퍼런스에 실제로 보이는 제품의 형태·색상·라벨 디자인·비율은 최대한 정확하게
   유지하세요. 각도를 바꾸면서 레퍼런스에 안 보이는 부분(반대쪽 면 등)만 자연스럽게
   최소한으로 추정하고, 레퍼런스에 이미 보이는 부분까지 임의로 다른 디자인으로
   바꾸지 마세요.
3. 제목·라벨·숫자·짧은 캡션(10자 이내)은 이미지 안에 직접 그려 넣어도 됩니다. 단, 2문장
   이상 되는 긴 설명 문구나 후기 인용구처럼 문장이 긴 텍스트는 이미지 안에 그리지 마세요
   (글자 수가 많아질수록 이미지 생성 모델이 오타를 낼 위험이 커집니다). 그런 긴 문구가
   있다면 이미지에는 넣지 말고 아래 [텍스트 응답] 규칙에 따라 텍스트로만 반환하세요.
4. 원본 문구를 한 글자씩 그대로 따라 하지 마세요. 같은 내용을 자연스럽게 다른 표현으로
   바꾸세요.
5. 용량·중량·호환모델·수치 등 핵심 스펙 정보는 원본 값 그대로 유지하세요. 다른 숫자로
   바꾸거나 지어내지 마세요.
6. 이미지에 상품명이 등장한다면 반드시 "{product_name}"로 표기하세요 (원본에 다른 이름이
   있어도 이 이름으로 교체).
7. 유통사·수입사·판매처·소싱처 등 유통 관련 정보는 이미지에 넣지 마세요.
8. 레퍼런스는 원래 다른 판매자가 올린 것입니다. 그 판매자 개인·매장에만 해당하는 표현은
   절대 가져오지 마세요 — 배송 소요일/배송비 안내, "정식 수입/정품 보장", "가품을 팔지
   않습니다" 같은 신뢰 문구, "오직 OO스토어에서만 구매 가능"처럼 특정 스토어 한정·독점을
   암시하는 표현 등은 전부 무시하고 이미지에도, 텍스트 응답에도 넣지 마세요.
9. 레퍼런스에 담긴 정보량이 많다면(여러 포인트·설명이 한 이미지에 뭉쳐있는 경우)
   요약해서 줄이지 말고 최소 70% 이상은 살려서 재구성하세요. 필요하면 세로로 여러
   블록을 이어서 배치해 다 담아도 됩니다 — 정보를 잘라내는 것보다 이미지를 길게
   만드는 쪽을 우선하세요.
{banned}

[텍스트 응답 — 이미지와 별도로]
규칙 3에 따라 긴 설명 문구나 후기 인용구를 이미지에 넣지 않기로 했다면, 그 내용은
반드시 여기 텍스트 응답에 새로운 표현으로 재구성해서 포함하세요. 다른 설명·인사말 없이
본문 문구만 작성하세요(2~4문장, 줄바꿈 가능). 섹션 제목이나 구성상 후기·경험담·설명이
있어야 하는데 내용을 통째로 비워서 반환하는 것은 금지됩니다. 정말 긴 문구가 전혀 없는
섹션일 때만 텍스트 응답을 비워두세요.

세로 비율 약 {aspect_hint}, 고급스러운 커머스 상세페이지 품질로 이미지를 만들어주세요."""


def generate_detail_page_from_reference(
    reference_image_paths: list[str],
    product_name: str,
    api_key: str,
    output_dir: Optional[str] = None,
    progress_cb=None,
) -> str:
    """
    레퍼런스 이미지들(실제 스마트스토어 상세페이지 캡처)을 참고해 완전히 새로운
    상세페이지 이미지를 생성. 이미지 1장 = 섹션 1개, 받은 순서 그대로 처리.

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

    section_images: list[Image.Image] = []

    for i, ref_path in enumerate(valid_paths):
        step_no = i + 1
        _progress(step_no, f"섹션 {step_no}/{len(valid_paths)} — 생성 중...")
        img, body = _regenerate_section(ref_path, api_key, product_name)
        if img is None:
            img = Image.new("RGB", (_CANVAS_W, _CANVAS_H), (245, 245, 245))
        elif body:
            img = overlay_body_text(img, body)
        section_images.append(img)

    if not section_images:
        print("[RefComposer] 생성된 섹션 이미지 없음")
        return ""

    _progress(total_steps, "섹션 합성 중...")
    final_img = _stack_vertical(section_images)
    final_img = append_disclaimer(final_img, _DISCLAIMER_TEXT)

    uid   = uuid.uuid4().hex[:10]
    fname = f"detail_page_ref_{uid}.jpg"
    fpath = out_dir / fname
    final_img.save(str(fpath), "JPEG", quality=92, subsampling=0)
    print(f"[RefComposer] 완성: {fpath} ({final_img.size[0]}x{final_img.size[1]}px)")
    return str(fpath)


def _regenerate_section(
    ref_path: str, api_key: str, product_name: str
) -> tuple[Optional[Image.Image], str]:
    """레퍼런스 이미지 한 장을 참고해 새 섹션 이미지를 단일 호출로 생성.
    제목·라벨처럼 짧은 텍스트는 이미지 모델이 직접 그려 넣고, 긴 본문 문구는
    이미지에 굽지 않고 별도 텍스트로 함께 반환받는다 (호출부에서 PIL로 오버레이)."""
    try:
        from google.genai import types
    except ImportError:
        return None, ""

    target_w, target_h = _target_size(ref_path)
    prompt = _GENERATION_PROMPT_TMPL.format(
        product_name=product_name,
        banned=_BANNED_PHRASES_BLOCK,
        aspect_hint=f"{target_w}:{target_h}",
    )

    try:
        img_bytes, mime = _load_ref_bytes(ref_path)
        parts = [
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            types.Part.from_text(text=prompt),
        ]
    except Exception as e:
        print(f"[RefComposer] 레퍼런스 이미지 로드 실패: {e}")
        return None, ""

    img, body = _call_image_model(_PRIMARY_MODEL, parts, api_key, target_w, target_h)
    if img is None:
        print(f"[RefComposer] {_PRIMARY_MODEL} 실패 → {_FALLBACK_MODEL} 폴백")
        img, body = _call_image_model(_FALLBACK_MODEL, parts, api_key, target_w, target_h)
    return img, body


def _call_image_model(
    model: str, parts: list, api_key: str, target_w: int, target_h: int
) -> tuple[Optional[Image.Image], str]:
    try:
        from google import genai
        from google.genai import types

        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model    = model,
            contents = parts,
            config   = types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                # 어차피 _resize_to_size()로 다시 리사이즈되므로 2K/4K는
                # 비용만 더 들고 이득이 없음 — 명시적으로 1K 고정
                image_config=types.ImageConfig(image_size="1K"),
            ),
        )
        img: Optional[Image.Image] = None
        text_out = ""
        for part in (response.candidates[0].content.parts if response.candidates else []):
            if getattr(part, "inline_data", None):
                raw = Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
                img = _resize_to_size(raw, target_w, target_h)
            elif getattr(part, "text", None):
                text_out += part.text
        return img, text_out.strip()
    except Exception as e:
        print(f"[RefComposer] {model} 오류: {e}")
        return None, ""
