"""
Module — Gemini 이미지 모델로 상품 사진을 새 배경에서 촬영한 것처럼 재생성.

누끼(rembg)로 처리하기 어려운 사진(재판매업체 워터마크, 공식 인증 배지,
마트/폰카 촬영 사진 등)을 위한 대안 경로. 상품 자체는 절대 다시 그리지 않고
배경·워터마크만 정리한다.

모델: gemini-3-pro-image  (GA, detail_page/section_generator.py 와 동일 조합)
폴백: gemini-3.1-flash-image  (1차 실패 시에만)

실패 시 항상 None 반환 — 호출부가 기존 누끼 파이프라인으로 자동 폴백하므로
파이프라인을 절대 중단시키지 않는다.
"""
from __future__ import annotations

import io
import os
from typing import Optional

from PIL import Image

_PRIMARY_MODEL  = "gemini-3-pro-image"
_FALLBACK_MODEL = "gemini-3.1-flash-image"

# 별도 Part로 분리한 강한 고정 지시문 — section_generator.py의 레퍼런스 앵커 패턴과 동일.
# "배경 교체" 태스크 설명보다 먼저 주입해 텍스트 보존을 최우선 순위로 각인시킨다.
_REFERENCE_ANCHOR = (
    "REFERENCE PHOTO: The attached image is the exact real product and its exact "
    "packaging. This is a background-replacement / photo-restoration task, NOT a "
    "redesign task. Every single element printed on the product packaging — product "
    "name, tagline, ingredient list, nutrition facts, manufacturing date, barcode, "
    "logos, fonts, colors, layout, even small print — must remain PIXEL-IDENTICAL to "
    "the reference. Do not redraw, retranslate, paraphrase, restyle, or reinterpret "
    "any text or graphic that is part of the product's own packaging design, even if "
    "it looks bold, decorative, or stylized. If you are not fully confident you can "
    "reproduce a piece of text with 100% accuracy, leave that exact region of the "
    "photo completely untouched rather than approximating it.\n"
    "DO NOT INVENT NEW CONTENT: only reproduce what is literally visible in the "
    "reference photo. Never add a nutrition facts table, ingredient panel, barcode, "
    "label, badge, or any other design element that is not already visible in the "
    "reference — even if it would look more 'complete' or more like a typical retail "
    "package. If part of the packaging is not shown or not legible in the reference "
    "photo, leave that area exactly as it appears (plain packaging surface), do not "
    "fill it in with fabricated content."
)

_PROMPT = """역할: 너는 쿠팡(오픈마켓) 상품 대표이미지 편집 전문가야. 첨부된 원본 상품 사진을
다른(새) 배경에서 찍은 것처럼 자연스럽게 다시 만들어줘.

[제거 대상 — 아래에 해당할 때만 제거]
- 상품 패키지 위에 별도로 얹혀진 재판매업체 워터마크·로고·행택, "공식/정품" 인증 배지
  스티커 (제품 디자인과 분리된, 매장/판매자가 나중에 붙인 요소만 해당)
- 그 외 패키지에 원래 인쇄되어 있는 모든 문구·그래픽·브랜드로고는 절대 건드리지 마
  (제품명, 카피 문구, 성분표, 영양정보, 제조일자, 바코드 포함 — 위 REFERENCE 규칙과 동일)

[사진 유형별 배경 처리]
- 마트·집 등 스튜디오가 아닌 곳에서 스마트폰으로 찍은 사진: 빛 반사 보정, 손이 나왔으면
  제거 가능. 배경은 제품과 어울리는 단조로운 새 배경으로 교체
- 이미 스튜디오에서 촬영된 사진: 배경만 새 배경으로 교체. 촬영 각도는 원본과 거의
  동일하게 유지하되 아주 미세하게(5~10도 이내) 살짝 틀어도 좋음 — 단, 그 상태에서도
  상품에 인쇄된 모든 텍스트는 왜곡 없이 정확하게 보여야 함. 조금이라도 자신 없으면
  각도는 그대로 유지해 (텍스트 정확도가 항상 최우선)

[원본 사진과 눈에 띄게 달라야 함 — 중요]
- 배경색·톤은 원본 사진과 명확히 다르게 생성해: 예를 들어 원본이 하늘색/파란 계열
  그라데이션이면 베이지·그레이·크림·웜톤 등 완전히 다른 색 계열로 바꿔.
- 워터마크·배지만 지우고 배경 색감·구도가 원본과 거의 똑같으면 안 돼 — 재판매업체
  사진을 살짝 편집한 것처럼 보이지 않고, 완전히 새로 촬영한 사진처럼 보여야 해.

[출력 형식]
- 1:1 정사각형 비율, 제품이 프레임 정중앙
- 배경은 단순하고 제품 톤과 어울리는 단색/그라데이션 (고급 스튜디오 상품사진 느낌)
- 이미지 안에 어떤 문구/텍스트도 새로 추가하지 마
- ⚠️ 원본 사진에 없는 요소는 절대 새로 만들어 넣지 마 — 영양성분표, 성분표, 바코드,
  라벨, 배지 등 원본에 실제로 안 보이는 내용을 "더 완성된 상품사진처럼 보이게" 임의로
  추가하면 안 됨. 원본에 없으면 없는 그대로(빈 포장 표면) 두는 게 정답
- 쿠팡 상품 대표이미지로 바로 쓸 수 있는 고품질 상업 사진 결과물로 생성"""


def regenerate_background(
    image_path: str,
    product_id: str,
    api_key: str,
    output_dir: str,
    spec_hint: str = "",
) -> Optional[str]:
    """
    원본 상품 사진 1장을 Gemini로 재생성 → 로컬에 저장하고 경로를 반환.

    spec_hint: 크롤링으로 확보한 정확한 중량/용량 (예: "280g"). 사진 속 숫자가
               접히거나 흐릿해도 이 값을 그대로 표시하도록 강제 — 모델이 비슷한
               숫자로 추측 인쇄하는 사고 방지 (예: 280g → 200g 오인식).

    실패 시 None (호출부가 기존 누끼/원본 파이프라인으로 폴백).
    """
    if not os.path.isfile(image_path):
        print(f"[AIBgRegen] 파일 없음: {image_path}")
        return None

    try:
        from google.genai import types
    except ImportError:
        print("[AIBgRegen] google-genai 패키지 없음")
        return None

    prompt_text = _PROMPT
    if spec_hint:
        prompt_text += (
            f"\n\n[정확한 중량/용량 — 반드시 이 값 그대로 표시]\n"
            f"이 상품의 정확한 중량/용량은 '{spec_hint}' 입니다. 패키지에 인쇄된 숫자가 "
            f"접히거나 가려지거나 흐릿하거나 이 값과 다르게 보이더라도, 절대 추측하지 말고 "
            f"반드시 위 값을 정확히 그대로 표시하세요."
        )

    try:
        mime = _mime_of(image_path)
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        parts = [
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            types.Part.from_text(text=_REFERENCE_ANCHOR),
            types.Part.from_text(text=prompt_text),
        ]
    except Exception as exc:
        print(f"[AIBgRegen] 원본 이미지 로드 실패: {exc}")
        return None

    img = _call_model(_PRIMARY_MODEL, parts, api_key)
    if img is None:
        print(f"[AIBgRegen] {_PRIMARY_MODEL} 실패 → {_FALLBACK_MODEL} 폴백")
        img = _call_model(_FALLBACK_MODEL, parts, api_key)
    if img is None:
        print("[AIBgRegen] AI 배경 재생성 완전 실패")
        return None

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{product_id}_aibg.jpg")
    img.save(path, "JPEG", quality=95, subsampling=0)
    print(f"[AIBgRegen] 저장 완료: {path}")
    return path


def _call_model(model: str, parts: list, api_key: str) -> Optional[Image.Image]:
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
                return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")

        print(f"[AIBgRegen] {model} 응답에 이미지 없음")
        return None

    except Exception as exc:
        print(f"[AIBgRegen] {model} 오류: {exc}")
        return None


def _mime_of(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):  return "image/png"
    if p.endswith(".webp"): return "image/webp"
    return "image/jpeg"
