"""
section_director.py — Gemini 텍스트 모델로 N개 섹션 프롬프트 JSON 생성

수정 이력:
  v2 (2026-06-11):
    - 기본값 변경: use_composite=False → 모든 섹션 ai_full 기본
    - 전 섹션 공통 스타일 앵커(조명·톤·배경 무드) 프롬프트 추가
    - composite 는 수동 ON 옵션으로만 남김

render_mode 분류:
  - "ai_full"   : (기본) Gemini가 배경+제품 전체 생성. 레퍼런스 사진 첨부.
  - "composite" : (옵션) Gemini가 배경만 생성, 실사진 PIL 합성. use_composite=True 시만.
"""
from __future__ import annotations

import json
import re
import textwrap
from typing import Optional


_SECTION_TYPES = [
    "hero",          # 제품 대표 이미지 + 핵심 한 줄         → ai_full
    "features",      # 주요 특징 3~4가지 (개별 품목 노출)     → composite
    "specs",         # 스펙/구성 (실제 사진 배치)             → composite
    "usage",         # 사용 장면 (실제 제품 + 배경 분위기)    → composite
    "callout",       # 강조 포인트 클로즈업                   → ai_full
    "closing",       # 마무리 / 브랜드 감성                   → ai_full
]

# 섹션 타입 → render_mode 매핑 (use_composite=True 일 때 적용)
_RENDER_MODE: dict[str, str] = {
    "hero":     "ai_full",
    "callout":  "ai_full",
    "closing":  "ai_full",
    "features": "composite",
    "specs":    "composite",
    "usage":    "composite",
}

# 전 섹션 공통 스타일 앵커 — 조명·톤·배경이 섹션 간 일관되게
_STYLE_ANCHOR = (
    "Consistent visual style across all sections: "
    "professional product photography, soft diffused studio lighting, "
    "neutral warm-white or light grey background, "
    "clean modern aesthetic, high-end commercial shoot quality, "
    "same color temperature and mood throughout. "
    "No text or typography in the image. "
    "9:16 vertical format optimized for Korean e-commerce detail page."
)


def build_section_prompts(
    product_name:  str,
    specs:         dict[str, str],
    section_count: int,
    api_key:       str,
    model:         str  = "gemini-2.5-flash",
    use_composite: bool = False,  # False(기본) = 모든 섹션 ai_full, True = composite 허용
) -> list[dict]:
    """
    Gemini 텍스트 모델로 N개 섹션 프롬프트 리스트를 생성.

    Returns:
        [
          {
            "section_type":  "hero",
            "render_mode":   "ai_full" | "composite",
            "image_prompt":  "...",   # 영문, Gemini 이미지 모델 입력용
                                       # composite 섹션은 배경/분위기 묘사만
            "overlay_texts": {
              "title":    "...",       # 한글 (3~8자)
              "subtitle": "...",       # 한글 (10~25자), 없으면 ""
              "body":     "..."        # 한글 본문, 없으면 ""
            },
            "layout": "full|split|grid|row"
          },
          ...
        ]
        실패 시 빈 리스트 반환.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[SectionDirector] google-genai 패키지 없음")
        return []

    section_count = max(1, section_count)   # 하한 1, 상한 없음

    # 섹션 타입 결정:
    #   N ≤ 6  → 고정 타입 목록에서 선택 (일관된 구조)
    #   N > 6  → Gemini가 N개 타입을 자유롭게 결정 (콘텐츠 기반)
    if section_count <= len(_SECTION_TYPES):
        chosen_types: list[str] | None = _SECTION_TYPES[:section_count]
    else:
        chosen_types = None   # 프롬프트에서 자유 생성 지시

    spec_lines = "\n".join(f"- {k}: {v}" for k, v in list(specs.items())[:20]) if specs else "없음"

    # render_mode 설명
    if use_composite:
        render_instruction = """
[render_mode 규칙]
- "ai_full"   → hero / callout / closing: Gemini가 배경+제품 전체 생성.
- "composite" → features / specs / usage: 배경만 생성, 실사진 PIL 합성.
                 image_prompt는 배경 환경만 묘사 (제품 묘사 금지).
"""
    else:
        render_instruction = f"""
[render_mode 규칙]
- 모든 섹션: "ai_full" 고정.
- 공통 스타일 앵커 (모든 섹션에 적용):
  {_STYLE_ANCHOR}

[image_prompt 작성 필수 항목 — 모든 섹션 동일 기준]
각 섹션의 image_prompt는 아래 5가지를 반드시 포함한 상세한 사진 촬영 브리프여야 합니다:
  1. CAMERA: 카메라 앵글/거리 (예: "eye-level front view", "45-degree 3/4 angle", "overhead flat-lay", "close-up macro")
  2. LIGHTING: 조명 세부 묘사 (예: "soft diffused overhead studio light with subtle left fill", "even shadowless lighting")
  3. BACKGROUND: 배경 정확한 묘사 (예: "seamless light grey gradient backdrop", "warm off-white paper background")
  4. COMPOSITION: 제품 배치·구도 (예: "product centered, surrounded by accessories", "product at lower-third rule")
  5. QUALITY: 화질/느낌 (예: "sharp focus, shallow DOF bokeh on background", "crisp commercial product photo")

모든 섹션의 image_prompt 길이와 디테일 수준을 hero 섹션과 동일하게 맞추세요.
짧거나 기능 설명만 있는 image_prompt는 반드시 위 5항목으로 보강해야 합니다.

CRITICAL — 모든 섹션(specs 포함) image_prompt 절대 금지 표현:
- "space for text", "room for text overlay", "text area", "leave area", "empty space for labels"
- "small product", "product in corner", "product tucked", "minimal product"
- 텍스트 오버레이를 위한 공간 확보 지시 일체 금지.
- 텍스트는 코드가 PIL로 별도 합성하므로, image_prompt는 오직 사진 구도/조명/배경만 묘사.
- specs 섹션도 hero와 동등하게 제품을 크고 중앙에 배치하는 구도를 사용할 것.
"""

    system_prompt = textwrap.dedent(f"""
        당신은 쿠팡 상세페이지 전문 비주얼 디렉터입니다.
        아래 규칙을 반드시 준수하세요:

        [절대 금지]
        1. 제공되지 않은 수치·효능·스펙을 임의로 생성하지 마세요.
        2. "최고", "최초", "1위", "혁신적" 등 과장 표현 금지.
        3. 의학적 효과, 치료, 개선 효과 단정 금지.
        4. 이미지 프롬프트에 한글 텍스트를 넣지 마세요 — 한글은 overlay_texts에만 작성.
        {render_instruction}
        [overlay_texts 작성 방향]
        - 팩트 기반: 주어진 상품명·스펙만 사용, 모르면 해당 필드를 비워두세요.
        - title: 3~8자 핵심 문구 (한글).
        - subtitle: 10~25자 보조 문구 (한글, 없으면 "").
        - body: 스펙/설명 2~4줄 (한글, 없으면 "").
    """).strip()

    # N ≤ 6: 타입 목록 명시 / N > 6: 자유 생성 지시
    if chosen_types is not None:
        section_request = (
            f"[요청 섹션 목록 — 순서대로 JSON 배열로 반환]\n"
            f"{json.dumps(chosen_types, ensure_ascii=False)}\n"
            f"section_type 값은 반드시 위 목록 중 하나를 사용하세요."
        )
    else:
        section_request = (
            f"[요청 섹션 수: {section_count}개]\n"
            f"section_type 이름은 제품 콘텐츠에 맞게 자유롭게 정하세요 (영문 snake_case).\n"
            f"첫 섹션은 'hero', 마지막 섹션은 'closing'을 권장합니다.\n"
            f"중간 섹션들은 제품 특성을 가장 잘 보여주는 이름으로 구성하세요."
        )

    user_prompt = textwrap.dedent(f"""
        [상품명]
        {product_name}

        [스펙/속성]
        {spec_lines}

        {section_request}

        [출력 형식 — JSON 배열만, 다른 텍스트 없음]
        [
          {{
            "section_type":  "<섹션 타입명>",
            "render_mode":   "ai_full",
            "image_prompt":  "<영문 이미지 생성 프롬프트 — 카메라·조명·배경·구도·화질 모두 포함>",
            "overlay_texts": {{
              "title":    "<3~8자 한글 핵심 문구>",
              "subtitle": "<10~25자 한글 보조 문구 또는 빈 문자열>",
              "body":     "<한글 본문 2~4줄 또는 빈 문자열>"
            }},
            "layout": "<full|split|grid|row>"
          }},
          ...
        ]
    """).strip()

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=[types.Part.from_text(text=f"{system_prompt}\n\n{user_prompt}")],
        )
        raw = (response.text or "").strip()

        # 코드블록 제거
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)

        sections = json.loads(raw)
        if not isinstance(sections, list):
            raise ValueError("응답이 리스트가 아님")

        # render_mode 누락 시 section_type 기반으로 보완
        for s in sections:
            if "render_mode" not in s or s["render_mode"] not in ("ai_full", "composite"):
                stype = s.get("section_type", "")
                s["render_mode"] = (
                    _RENDER_MODE.get(stype, "ai_full")
                    if use_composite else "ai_full"
                )

        print(f"[SectionDirector] {len(sections)}개 섹션 프롬프트 생성 완료")
        for s in sections:
            print(f"  [{s.get('render_mode','?'):9s}] {s.get('section_type','?')}")
        return sections

    except Exception as e:
        print(f"[SectionDirector] 섹션 프롬프트 생성 실패: {e}")
        return []
