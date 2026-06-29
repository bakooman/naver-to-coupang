"""
Google Gemini (google-genai SDK) 를 이용한 쿠팡 상세페이지 판매 설명 자동 생성

흐름:
  1. Gemini가 상품명 + 이미지 분석 → 판매 설명 HTML 텍스트 생성
  2. 생성된 HTML을 Playwright로 780px 너비 이미지로 렌더링
  3. 렌더링 이미지를 R2에 업로드
  4. 최종 detail_html = [설명 이미지] + [상품 누끼 이미지]

작성 지침:
  1. 팩트에 기반해서만 작성 (추측·과장 금지)
  2. 성분·규격·용량·구성 정보 있으면 반드시 포함
  3. 판매자(스마트스토어) 개인정보, 쇼핑몰 홍보, 배송 안내 등 일절 제외
  4. 유통사·수입사·판매처·소싱처·공급원 등 유통 관련 정보는 절대 언급 금지
  5. 제품 자체의 특징·용도·사용법·주의사항만 작성
  6. 한국어, 간결하게
"""
from __future__ import annotations

import re
import tempfile
import textwrap
import urllib.request
from pathlib import Path
from typing import Optional


def _load_image_bytes(url: str, timeout: int = 8) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


_SPEC_BLOCK_NAMES = {
    "유통사", "판매자", "수입사", "수입원", "판매원", "공급원", "공급사",
    "판매처", "구매처", "소싱처", "수입업체", "수입자",
    "distributor", "seller", "vendor", "importer", "reseller",
}

def _extract_specs(raw_json: dict, depth: int = 0) -> dict[str, str]:
    """raw_json 에서 속성/스펙 키-값 재귀 추출. 유통사/판매자 정보는 제외."""
    if depth > 6 or not isinstance(raw_json, dict):
        return {}
    SPEC_KEYS = {
        "attributes", "attribute", "specs", "spec",
        "productAttributes", "detailAttributes",
        "ingredientInfo", "nutritionInfo", "componentInfo",
        "components", "volume", "capacity", "weight",
        "manufacturer", "origin", "brand", "contents",
    }
    result: dict[str, str] = {}
    for k, v in raw_json.items():
        kl = k.lower()
        if any(sk in kl for sk in SPEC_KEYS):
            if isinstance(v, str) and v.strip():
                result[k] = v.strip()[:300]
            elif isinstance(v, list):
                for item in v[:20]:
                    if isinstance(item, dict):
                        name = (item.get("attributeName") or item.get("name")
                                or item.get("key") or "")
                        val  = (item.get("attributeValue") or item.get("value")
                                or item.get("val") or "")
                        # 유통사/판매자 관련 속성명은 제외
                        if name and val and str(name).strip() not in _SPEC_BLOCK_NAMES:
                            result[str(name)] = str(val)[:200]
            elif isinstance(v, dict):
                result.update(_extract_specs(v, depth + 1))
        elif isinstance(v, dict):
            result.update(_extract_specs(v, depth + 1))
    return result


def _html_to_image_bytes(html_body: str, width: int = 780) -> Optional[bytes]:
    """
    PIL + BeautifulSoup 으로 HTML → PNG 이미지 bytes 렌더링.
    Playwright/Chromium 없이 서버에서 동작.
    """
    try:
        import io
        import textwrap as _tw
        from bs4 import BeautifulSoup
        from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font

        _FONT_PATHS = [
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
            "C:/Windows/Fonts/malgunsl.ttf",
            "C:/Windows/Fonts/malgun.ttf",
            "data/fonts/NotoSansKR-Light.otf",
        ]
        _FONT_BOLD_PATHS = [
            "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
            "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothicExtraBold.ttf",
            "C:/Windows/Fonts/malgunbd.ttf",
            "C:/Windows/Fonts/malgun.ttf",
        ]

        def _font(size: int, bold: bool = False) -> _Font.ImageFont:
            paths = _FONT_BOLD_PATHS if bold else _FONT_PATHS
            for p in paths:
                try:
                    return _Font.truetype(p, size)
                except Exception:
                    pass
            if bold:
                for p in _FONT_PATHS:
                    try:
                        return _Font.truetype(p, size)
                    except Exception:
                        pass
            return _Font.load_default()

        soup = BeautifulSoup(html_body, "html.parser")

        # ── 렌더링할 블록 수집 ────────────────────────────────
        # (tag, text) 리스트
        blocks: list[tuple[str, str]] = []
        TARGET_TAGS = {"h3", "p", "li"}
        for el in soup.find_all(["h3", "p", "li"]):
            # 부모 중에 같은 타겟 태그가 있으면 중첩 요소 → 건너뜀
            if any(p.name in TARGET_TAGS for p in el.parents):
                continue
            text = el.get_text(" ", strip=True)
            if text:
                blocks.append((el.name, text))

        if not blocks:
            return None

        PAD_X     = 32
        PAD_Y     = 28
        LINE_H_P  = 26   # p/li 행 간격
        LINE_H_H3 = 36   # h3 행 간격
        FONT_P    = _font(16)
        FONT_H3   = _font(21, bold=True)
        CHAR_W    = width - PAD_X * 2  # 텍스트 가용 픽셀 폭

        # 한글 문자 실제 폭을 PIL로 측정해서 줄바꿈 계산
        def _measure(text: str, font) -> int:
            try:
                return int(font.getlength(text))
            except Exception:
                try:
                    return font.getbbox(text)[2]
                except Exception:
                    return len(text) * 16

        def _wrap_text(text: str, font, max_w: int) -> list[str]:
            """픽셀 폭 기준 텍스트 줄바꿈."""
            if _measure(text, font) <= max_w:
                return [text]
            lines, cur = [], ""
            for ch in text:
                if _measure(cur + ch, font) > max_w:
                    if cur:
                        lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                lines.append(cur)
            return lines or [text]

        # ── 1패스: 총 높이 계산 ───────────────────────────────
        total_h = PAD_Y
        for tag, text in blocks:
            if tag == "h3":
                lines_h3 = _wrap_text(text, FONT_H3, CHAR_W)
                total_h += len(lines_h3) * LINE_H_H3 + 8 + 14
            else:
                prefix = "• " if tag == "li" else ""
                lines_p = _wrap_text(prefix + text, FONT_P, CHAR_W)
                total_h += len(lines_p) * LINE_H_P + 6
        total_h += PAD_Y

        # ── 2패스: 실제 그리기 ────────────────────────────────
        img = _Img.new("RGB", (width, max(total_h, 100)), (255, 255, 255))
        draw = _Draw.Draw(img)
        y = PAD_Y

        for tag, text in blocks:
            if tag == "h3":
                for line in _wrap_text(text, FONT_H3, CHAR_W):
                    draw.text((PAD_X, y), line, font=FONT_H3, fill=(26, 26, 26))
                    y += LINE_H_H3
                draw.line([(PAD_X, y), (width - PAD_X, y)], fill=(220, 220, 220), width=2)
                y += 8 + 14
            else:
                prefix = "• " if tag == "li" else ""
                for line in _wrap_text(prefix + text, FONT_P, CHAR_W):
                    draw.text((PAD_X, y), line, font=FONT_P, fill=(68, 68, 68))
                    y += LINE_H_P
                y += 6

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    except Exception as e:
        print(f"[Gemini] HTML→이미지 렌더링 실패: {e}")
        return None


def generate_detail_image_url(
    product_name: str,
    image_url: str,
    raw_json: dict,
    api_key: str,
    model: str = "gemini-2.0-flash",
    upload_fn=None,        # R2 업로드 함수: bytes → str(url) 또는 None
    product_id: str = "",  # R2 파일명용
) -> str:
    """
    Gemini로 판매 설명 생성 → 이미지 렌더링 → R2 업로드 → URL 반환.

    Returns:
        R2 이미지 URL 문자열. 실패 시 빈 문자열.
    """
    # ── 1. Gemini 텍스트 생성 ──────────────────────────────────────
    html_text = _generate_text(product_name, image_url, raw_json, api_key, model)
    if not html_text:
        return ""

    # ── 2. HTML → 이미지 렌더링 ───────────────────────────────────
    img_bytes = _html_to_image_bytes(html_text)
    if not img_bytes:
        return ""

    print(f"[Gemini] 설명 이미지 렌더링 완료 ({len(img_bytes):,} bytes)")

    # ── 3. R2 업로드 ──────────────────────────────────────────────
    if upload_fn is None:
        return ""

    fname = f"{product_id}_detail_desc.png" if product_id else "gemini_detail.png"
    try:
        url = upload_fn(img_bytes, fname)
        if url:
            print(f"[Gemini] 설명 이미지 R2 업로드: {url[:70]}...")
            return url
    except Exception as e:
        print(f"[Gemini] 설명 이미지 R2 업로드 실패: {e}")

    return ""


def _generate_text(
    product_name: str,
    image_url: str,
    raw_json: dict,
    api_key: str,
    model: str,
) -> str:
    """Gemini API 호출 → 판매 설명 HTML 텍스트 반환."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        specs = _extract_specs(raw_json)
        spec_text = ""
        if specs:
            lines = [f"- {k}: {v}" for k, v in list(specs.items())[:15]]
            spec_text = "\n".join(lines)

        prompt = textwrap.dedent(f"""
            당신은 쿠팡 정책을 완벽하게 준수하면서 매출을 일으키는 전문 상품 기획자(MD) 겸 카피라이터입니다.
            소비자가 실제로 얻을 수 있는 이점을 팩트 기반으로 설득력 있게 전달하는 것이 당신의 역할입니다.

            ══════════════════════════════════════════
            [절대 금지 — 네거티브 프롬프트]
            ══════════════════════════════════════════

            ① 패키지 외형 1차원 묘사 절대 금지
               용기 색상, 뚜껑 모양, 라벨 글씨 위치, 문양 등 눈에 보이는 디자인을
               그대로 나열하는 행위 금지. 이미지는 제품 확인 참고용으로만 활용하고
               외형 묘사는 일절 출력하지 마세요.

            ② 기계적·당연한 AI 문장 절대 금지
               "이 제품은 피부에 바릅니다", "사용 후 씻어내세요" 등
               누구나 아는 당연한 사실을 AI처럼 나열하는 문장 금지.

            ③ 쿠팡 정책 위반 키워드 원천 배제 (위반 시 계정 정지)
               아래 표현은 어떠한 변형·유사 표현으로도 절대 사용하지 마세요.
               - 순위·우위: 1위, 최고, 최초, 최대, 가장, 유일한, 독보적
               - 의학적 오인: 피부과(추천·테스트·인증 등), 임상(완료·증명·실험 등),
                 효과 보장, 부작용 없음, 치료, 완치, 개선 효과 입증
               - 과장·허위: 기적, 혁신적, 놀라운, 완벽, 무조건, 특효, 전문가 추천,
                 세계 최고, 인생 제품

            ══════════════════════════════════════════
            [식품표시광고법 — 식품 카테고리 필수 준수]
            (식품·건강기능식품·농수축산물 해당 시 아래 전 항목 반드시 적용)
            ══════════════════════════════════════════

            ④ 질병 예방·치료 효능 암시 표현 완전 금지 (식품표시광고법 제8조 1항 1호)
               - 금지 질병명 예시: 당뇨, 고혈압, 암, 아토피, 생리통, 생리불순, 수족냉증,
                 빈혈, 고지혈증, 골다공증, 치매, 우울증, 위염, 심혈관질환, 탈모방지,
                 혈당강하, 혈압강하, 항암, 항염, 면역력 증가, 장트러블, 변비, 쾌변,
                 소화성궤양, 안티에이징, 시력감퇴, 관절염, 갱년기 등 모든 질병·증상명
               - "~에 좋은", "~에 도움", "~완화", "~예방" + 질병명 조합도 금지

            ⑤ 의약품·한약 처방명 오인 표현 완전 금지 (식품표시광고법 제8조 1항 2호)
               - 금지 한약 처방명: 공진단, 경옥고, 쌍화탕, 십전대보탕, 사군자탕,
                 사물탕, 녹용대보탕, 총명탕, 귀비탕, 육미지황탕, 우황청심원,
                 익수영진고, 오자연종환 등 한약 처방명 및 유사 명칭 일체

            ⑥ 건강기능식품 기능성 오인 표현 금지 (식품표시광고법 제8조 1항 3호)
               - 다이어트, 체중감량, 지방분해, 식욕억제, 살빠지는, 키가 크는,
                 면역력(증진·향상·강화), 항산화, 혈액순환 개선, 간기능 개선,
                 간건강, 피로회복, 집중력 향상, 피부 수분 보충, 노화 방지,
                 체지방 감소 등 건강기능식품 기능성 표현 일체 금지
               - 건강기능식품 인증을 받지 않은 일반 가공식품에 위 표현 절대 불가

            ⑦ 거짓·과장·소비자 기만 표현 금지 (식품표시광고법 제8조 1항 4·5호)
               - before/after 비교, 체험기, 전후 사진 설명, 체중변화 수치 언급 금지
               - "3일에 5kg 감량", "15일 섭취 후 4.1kg 감량" 등 구체적 수치 효과 금지
               - 의사·한의사·전문가 추천·개발 표현 금지
               - 슈퍼푸드(Super food), GI지수, 당부하지수 등 기준 불명확 표현 금지

            ⑧ 부당 비교·최상급 표현 금지 (식품표시광고법 제8조 1항 7호)
               - 유일, 최고, 최상, 최적, 최대(기능성 관련), 고단위, 고순도, 고농도,
                 새로운 패러다임, 청정지역(증빙 없이) 등 근거 없는 절대적 표현 금지
               - 타 제품 성분 함량과 자사 제품 비교 금지

            ⑨ 소비자 기만 "무첨가" 표현 금지
               - 해당 식품에 원래 사용 불가한 원재료·첨가물에 "무첨가" 표현 금지
               - 무MSG, MSG 무첨가, 무방부제, 방부제 무첨가 표현 금지
               - 제품에 포함된 성분에 대해 "무첨가" 표현 금지

            ⑩ 친환경 표현 제한
               - 친환경 인증(유기농·무항생제 등) 없이 "친환경", "자연친화적",
                 "무독성", "100% 천연" 등 표현 금지
               - 무항생제 인증 축산물이라도 "친환경" 표현은 절대 금지

            ══════════════════════════════════════════
            [표시광고법 — 공산품 카테고리 필수 준수]
            (생활용품·전자제품·의류·가구 등 비식품 상품 해당 시 적용)
            ══════════════════════════════════════════

            ⑪ 일반 공산품에 의학적 효능·의료기기 효과 표현 금지
               - 치석 제거, 족저근막염, 평발 교정, 목디스크, 거북목, 일자목,
                 불면증, 혈액순환, 코골이 개선, 수면무호흡증, 자세 교정,
                 통증 제거·완화(파스 류 제외), 염증 치료, 탈모 방지,
                 주름 개선, 안면 리프팅, 피부질환 치료·완화 등 의료기기·의약품
                 효능에 해당하는 표현 일체 금지
               - "의료기기 아님" 명시만으로는 의료기기 오인 광고 면책 불가

            ⑫ 특허·인증·수상 과장 표현 금지
               - 특허 출원 중인 제품에 "특허" 표현 금지
               - 실용신안을 "특허"로 표기 금지
               - FDA 승인·인증 없이 "FDA 인증", "FDA 승인" 표현 금지
               - 인증·수상 사실 없이 획득한 것처럼 표현 금지

            ⑬ 환경성 관련 기만 표현 금지
               - 일부 성분만 불검출 사실로 완제품 전체가 "친환경", "무독성"인 것처럼 광고 금지
               - 법적 기준치 이하를 근거로 "친환경" 표현 금지
               - 환경표지 인증 없이 환경표지 마크·"친환경" 표현 금지
               - "유해물질 無", "안심사용" 등 포괄적 안전성 표현은 공인 성적서 근거 없이 금지

            ⑭ 부당 비교·비방 광고 금지 (표시광고법 제3조)
               - 근거 없는 "최고", "최초", "최상급" 등 배타적 절대 표현 금지
               - 경쟁사 제품 직접·간접 비방 금지
               - 비교 기준·근거 없는 타사 대비 우위 표현 금지

            ══════════════════════════════════════════
            [핵심 작성 방향]
            ══════════════════════════════════════════

            1. 소비자 이점 중심: 소비자가 이 제품을 쓰면 무엇을 얻는지(보습 지속 시간,
               향기, 성분 특징, 편의성 등) 구체적으로 서술하세요.
            2. 팩트 기반: 제공된 스펙·성분·구성 정보를 반드시 활용하고,
               확인되지 않은 효능·효과는 단정하지 마세요.
            3. 자연스러운 한국어 문체(~합니다, ~해요)로 설득력 있는 세일즈 카피 작성.
            4. 성분·규격·용량·구성 정보가 있으면 ul/li로 깔끔하게 정리하세요.
            5. 판매자 정보, 쇼핑몰 홍보, 배송 안내는 일절 쓰지 마세요.
            6. 유통사·수입사·판매처·소싱처·공급원·판매원·유통업체명은 절대로 언급하지 마세요.
               스펙 정보에 있더라도 제외하세요.
            7. 애매하면 쓰지 마세요: 금지 여부가 불확실한 표현은 과감히 제거하고
               제품의 실제 사용감·기능·구성 설명으로 대체하세요.

            ══════════════════════════════════════════
            [입력 정보]
            ══════════════════════════════════════════

            [상품명]
            {product_name}

            {f"[상품 속성/스펙]{chr(10)}{spec_text}" if spec_text else ""}

            ══════════════════════════════════════════
            [출력 형식]
            ══════════════════════════════════════════
            - h3 태그(섹션 헤딩) + p 태그(단락) + ul/li(목록)으로 구성
            - 마크다운 금지, HTML 태그만 사용
            - 전체 1,000자 이상 (풍부하고 설득력 있게)

            [이모티콘 지침]
            - 각 h3 헤딩 맨 앞에 반드시 관련 이모티콘 1개 포함
              예) ☕ 제품 소개 / 🎯 이런 분들께 추천합니다 / 📝 테이스팅 노트 / 🌱 상품 필수 정보
            - li 항목 끝에 맥락에 맞는 이모티콘 1개 배치 (과하지 않게, 내용당 1개)
            - 첫 소개 단락 끝에도 분위기에 맞는 이모티콘 1~2개 포함

            [글씨 굵기 지침]
            - 단락(p) 안에서 핵심 특징·성분·용량·브랜드명 등 강조할 단어는 <strong> 태그로 감싸기
              예) <p>엄선된 <strong>100% 아라비카</strong> 원두를 정교하게 블렌딩하여...</p>

            [필수 섹션 구조]
            1. 제품 핵심 소개 (h3 + p 1~2단락 — 제품의 정체성과 핵심 매력)
            2. 이런 분들께 추천합니다 (h3 + ul/li 3~5개 — 추천 대상, 각 항목 끝 이모티콘)
            3. 카테고리에 맞는 특징 섹션
               예) 커피 → 테이스팅 노트 | 식품 → 원재료 특징 | 뷰티 → 주요 성분·사용감
            4. 상품 필수 정보 (h3 + ul/li — 용량·원산지·포장 등 팩트 정보)
        """).strip()

        parts = [types.Part.from_text(text=prompt)]

        response = client.models.generate_content(model=model, contents=parts)
        raw_text = (response.text or "").strip()

        # 코드블록 제거
        raw_text = re.sub(r"^```(?:html)?\s*", "", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"\s*```$", "", raw_text)

        return raw_text if len(raw_text) >= 20 else ""

    except Exception as e:
        print(f"[Gemini] 텍스트 생성 실패: {e}")
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# 상품명 정책 준수 정제 함수
# ──────────────────────────────────────────────────────────────────────────────

def generate_compliant_product_name(
    raw_name: str,
    api_key: str,
    model: str = "gemini-2.0-flash",
) -> str:
    """
    네이버에서 가져온 원본 상품명을 쿠팡 정책·식품표시광고법·표시광고법에
    위반되지 않는 상품명으로 정제하여 반환.

    - 금지 표현 제거·대체 후 원래 의미를 유지하는 자연스러운 상품명 반환
    - 변경 불필요 시 원본 반환
    - 실패 시 원본 반환
    """
    if not raw_name or not api_key:
        return raw_name

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = textwrap.dedent(f"""
            당신은 쿠팡 Wing 상품 등록 전문가입니다.
            아래 원본 상품명에서 법령·쿠팡 정책 위반 표현을 제거하고
            판매 가능한 적법한 상품명으로 정제해 주세요.

            ══════════════════════════════════════════
            [제거·수정해야 할 금지 표현]
            ══════════════════════════════════════════

            [식품표시광고법 위반]
            - 질병명·증상: 당뇨, 고혈압, 암, 아토피, 생리통완화, 탈모방지, 혈당강하,
              혈압강하, 항암, 변비, 쾌변, 관절염, 치매예방 등 모든 질병·증상 관련 표현
            - 한약 처방명: 공진단, 경옥고, 쌍화탕, 십전대보탕, 총명탕, 귀비탕,
              육미지황탕, 우황청심원 등 한약 처방명 및 유사 명칭
            - 건강기능 표현: 다이어트, 체중감량, 지방분해, 식욕억제, 면역력,
              항산화, 혈액순환, 간기능, 피로회복, 집중력 향상 등
            - 부당 비교: 1위, 최고, 최상, 최초, 유일한, 고단위, 고순도, 고농도
            - 소비자 기만: 무MSG, 무방부제, 친환경(인증 없이), 무독성(인증 없이)

            [표시광고법 위반 — 공산품]
            - 의료기기 오인: 족저근막염, 목디스크, 거북목, 일자목, 불면증,
              교정, 혈액순환, 코골이 개선, 탈모 방지, 주름 개선, 피부질환 치료 등
            - 인증 없는 표현: FDA 인증, FDA 승인, 특허(출원 중 표기 시) 등

            ══════════════════════════════════════════
            [정제 규칙]
            ══════════════════════════════════════════
            1. 금지 표현이 없으면 원본 상품명을 그대로 반환하세요.
            2. 금지 표현이 있으면 해당 부분만 제거하거나 중립적 표현으로 대체하세요.
               예) "당뇨에 좋은 여주즙" → "여주즙"
               예) "족저근막염 깔창" → "아치 지지 깔창"
               예) "탈모방지 샴푸" → "두피 케어 샴푸"
            3. 상품명 자체(브랜드명, 용량, 수량, 모델명)는 변경하지 마세요.
            4. 상품명만 단독으로 반환하세요. 설명이나 이유는 출력하지 마세요.
            5. 최대 100자 이내로 작성하세요.

            ══════════════════════════════════════════
            [원본 상품명]
            {raw_name}
        """).strip()

        resp = client.models.generate_content(
            model=model,
            contents=[types.Part.from_text(text=prompt)],
        )
        result = (resp.text or "").strip().splitlines()[0].strip()
        if len(result) >= 2:
            if result != raw_name:
                print(f"[Gemini] 상품명 정제: '{raw_name[:50]}' → '{result[:50]}'")
            return result
    except Exception as e:
        print(f"[Gemini] 상품명 정제 실패: {e}")

    return raw_name


# ──────────────────────────────────────────────────────────────────────────────
# 브랜드 매칭 함수
# ──────────────────────────────────────────────────────────────────────────────

def match_brand_with_gemini(
    naver_brand: str,
    coupang_candidates: list[dict],
    api_key: str,
    model: str = "gemini-2.0-flash",
) -> Optional[dict]:
    """
    네이버 브랜드명과 쿠팡 브랜드 후보 목록을 비교해 가장 적합한 쿠팡 브랜드 반환.

    coupang_candidates 형식:
      [{"brandId": 12345, "brandName": "Nike"}, ...]

    반환값:
      일치하는 브랜드 dict (brandId, brandName) 또는 None (억지 매칭 금지)
    """
    if not naver_brand or not coupang_candidates or not api_key:
        return None

    try:
        import json
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        candidates_text = "\n".join(
            f"  - brandId={c['brandId']}, brandName={c['brandName']}"
            for c in coupang_candidates[:30]
        )

        prompt = textwrap.dedent(f"""
            당신은 브랜드 매칭 전문가입니다.
            네이버 브랜드명과 쿠팡 공식 브랜드 후보 목록을 비교해 가장 적합한 브랜드를 선택하세요.

            [규칙]
            1. 한글/영문 혼용, 약어, 띄어쓰기 차이, 대소문자 차이를 모두 감안하세요.
               예) "나이키" = "Nike" = "NIKE"
               예) "삼성전자" = "Samsung" = "SAMSUNG Electronics"
            2. 확실하게 같은 브랜드라고 판단될 때만 선택하세요.
            3. 확신이 없으면 반드시 null을 반환하세요. 억지로 매칭하지 마세요.
            4. 오직 JSON만 출력하세요. 설명 없이.

            [네이버 브랜드명]
            {naver_brand}

            [쿠팡 브랜드 후보]
            {candidates_text}

            [출력 형식]
            일치하는 브랜드가 있으면:
            {{"brandId": 12345, "brandName": "BrandName"}}

            일치하는 브랜드가 없으면:
            null
        """).strip()

        resp = client.models.generate_content(
            model=model,
            contents=[types.Part.from_text(text=prompt)],
        )
        raw = (resp.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)

        if raw.lower() == "null" or not raw:
            return None

        result = json.loads(raw)
        if isinstance(result, dict) and result.get("brandId") and result.get("brandName"):
            return result
        return None

    except Exception as e:
        print(f"[Gemini] 브랜드 매칭 실패: {e}")
        return None
