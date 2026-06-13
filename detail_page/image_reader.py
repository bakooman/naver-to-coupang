"""
image_reader.py — 분할된 이미지 조각 분류 + 제품 정보 텍스트 추출

원칙:
  - 긴 원본 이미지는 절대 API 전송 금지 → 항상 split_long_image() 통과한 조각만 전송
  - 이미지에서 실제로 읽힌 텍스트만 사용 (추측·지어내기·과장 금지)
  - 불확실한 정보는 빈 문자열 반환 (hallucination 방지)
  - 기존 section_director 안티-할루시네이션 규칙과 동일하게 적용

공개 API:
    classify_image_chunks(image_paths, api_key, model)
        → list[str]   # 각 조각의 타입: "product" | "text_graphic"

    read_product_info_from_images(image_paths, api_key, model)
        → dict   # {product_name, specs, descriptions}
"""
from __future__ import annotations

import json
import re
from pathlib import Path


# 한 번에 Gemini Vision에 보낼 최대 조각 수
# (Gemini 이미지 입력 제한 및 컨텍스트 효율 고려)
_MAX_PIECES_PER_CALL = 5


def read_product_info_from_images(
    image_paths: list[str],
    api_key:     str,
    model:       str = "gemini-2.5-flash",
) -> dict:
    """
    분할된 이미지 조각들에서 제품 정보(상품명, 스펙, 설명)를 추출.

    Args:
        image_paths: 분할된 이미지 로컬 경로 리스트 (이미 split_long_image() 완료)
        api_key:     Gemini API 키
        model:       텍스트/비전 모델 (기본 gemini-2.5-flash)

    Returns:
        {
            "product_name": str,          # 상품명 (못 찾으면 "")
            "specs": dict[str, str],      # 스펙 키-값 (확실한 것만)
            "descriptions": list[str],    # 특징·설명 문장들
            "raw_texts": list[str],       # 각 조각에서 읽은 원본 텍스트
        }
        실패 시 빈 구조 반환.
    """
    if not api_key:
        print("[ImageReader] API 키 없음")
        return _empty_result()

    valid_paths = [p for p in image_paths if p and Path(p).is_file()]
    if not valid_paths:
        print("[ImageReader] 유효한 이미지 없음")
        return _empty_result()

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[ImageReader] google-genai 패키지 없음")
        return _empty_result()

    # 조각이 너무 많으면 대표 조각만 선택 (균등 분포)
    pieces = _select_representative(valid_paths, _MAX_PIECES_PER_CALL)
    print(f"[ImageReader] {len(valid_paths)}장 중 {len(pieces)}장으로 텍스트 추출")

    # 이미지 파트 구성
    parts = []
    for path in pieces:
        try:
            mime = _mime_of(path)
            with open(path, "rb") as f:
                data = f.read()
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        except Exception as e:
            print(f"[ImageReader] 이미지 로드 실패 ({path}): {e}")

    if not parts:
        return _empty_result()

    # 엄격한 팩트 추출 프롬프트
    extract_prompt = """이 이미지들은 한국 쇼핑몰 상세페이지의 일부입니다.
이미지에 실제로 보이는 텍스트를 읽어서 아래 JSON 형식으로 정리해주세요.

[절대 규칙]
1. 이미지에서 실제로 읽히는 텍스트만 사용하세요.
2. 보이지 않는 정보를 추측하거나 만들어내지 마세요.
3. 불확실한 수치·효능·효과는 포함하지 마세요.
4. "최고", "최초", "1위" 등 과장 표현은 그대로 옮기되, 없는 것은 추가하지 마세요.
5. 의학적 효과 주장은 포함하지 마세요.

[출력 형식 — JSON만, 다른 텍스트 없음]
{
  "product_name": "<이미지에서 읽힌 상품명, 없으면 빈 문자열>",
  "specs": {
    "<속성명>": "<값>"
  },
  "descriptions": [
    "<이미지에서 읽힌 특징·설명 문장 (한국어)"
  ]
}

specs 예시: {"브랜드": "Karcher", "최대압력": "110bar", "모터출력": "1400W"}
descriptions 예시: ["케이블이 본체에 내장되어 보관이 편리합니다", "3m 고압 호스 포함"]"""

    parts.append(types.Part.from_text(text=extract_prompt))

    try:
        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model    = model,
            contents = parts,
        )
        raw = (response.text or "").strip()

        # 코드블록 제거
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$",           "", raw)

        data = json.loads(raw)
        result = {
            "product_name": str(data.get("product_name", "") or ""),
            "specs":        {str(k): str(v) for k, v in (data.get("specs") or {}).items()},
            "descriptions": [str(d) for d in (data.get("descriptions") or []) if d],
            "raw_texts":    [],
        }
        print(
            f"[ImageReader] 추출 완료 — 상품명: '{result['product_name']}' "
            f"| 스펙 {len(result['specs'])}개 | 설명 {len(result['descriptions'])}줄"
        )
        return result

    except json.JSONDecodeError as e:
        print(f"[ImageReader] JSON 파싱 실패: {e}\n원본: {raw[:200]}")
        return _empty_result()
    except Exception as e:
        print(f"[ImageReader] 추출 오류: {e}")
        return _empty_result()


# ── 조각 분류 ────────────────────────────────────────────────────────

# 한 번 호출에 보낼 최대 이미지 수 (분류용 배치 크기)
_MAX_CLASSIFY_BATCH = 8


def classify_image_chunks(
    image_paths: list[str],
    api_key:     str,
    model:       str = "gemini-2.5-flash",
) -> list[str]:
    """
    분할된 이미지 조각들을 'product' 또는 'text_graphic'으로 분류.

    - "product"      : 실제 제품이 주요 내용인 조각 (제품 사진, 제품 클로즈업 등)
    - "text_graphic" : 텍스트, 도표, 지도, 스펙표, 설명 그래픽이 주요 내용인 조각

    Args:
        image_paths: 분할된 조각 경로 리스트 (이미 split_long_image() 완료)
        api_key:     Gemini API 키
        model:       비전+텍스트 모델 (기본 gemini-2.5-flash)

    Returns:
        image_paths 와 동일 길이의 분류 결과 리스트.
        API 실패 시 전부 "product"로 반환 (안전 폴백).
    """
    valid_paths = [p for p in image_paths if p and Path(p).is_file()]
    if not valid_paths:
        return []

    if not api_key:
        print("[ImageReader] classify: API 키 없음 — 전부 product 처리")
        return ["product"] * len(valid_paths)

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return ["product"] * len(valid_paths)

    results: list[str] = []

    # 배치로 나눠서 처리 (조각이 많을 때 한 번에 너무 많이 보내지 않음)
    for batch_start in range(0, len(valid_paths), _MAX_CLASSIFY_BATCH):
        batch = valid_paths[batch_start: batch_start + _MAX_CLASSIFY_BATCH]
        batch_results = _classify_batch(batch, api_key, model)
        results.extend(batch_results)

    print(
        f"[ImageReader] 분류 결과: "
        f"제품 {results.count('product')}개 / "
        f"텍스트·그래픽 {results.count('text_graphic')}개"
    )
    return results


def _classify_batch(paths: list[str], api_key: str, model: str) -> list[str]:
    """배치 단위 분류 — 실패 시 전부 'product' 반환."""
    try:
        from google import genai
        from google.genai import types

        parts = []
        loaded_count = 0
        for i, path in enumerate(paths, 1):
            try:
                mime = _mime_of(path)
                with open(path, "rb") as f:
                    data = f.read()
                parts.append(types.Part.from_bytes(data=data, mime_type=mime))
                parts.append(types.Part.from_text(text=f"[이미지 {i}]"))
                loaded_count += 1
            except Exception as e:
                print(f"[ImageReader] 분류 이미지 로드 실패 ({path}): {e}")
                parts.append(types.Part.from_text(text=f"[이미지 {i}: 로드 실패]"))

        classify_prompt = f"""위 {len(paths)}개의 이미지를 분석하세요.
각 이미지를 다음 두 가지 중 하나로 분류하세요:
- "product"      : 실제 제품이 주요 내용 (제품 사진, 제품 클로즈업, 패키지 등)
- "text_graphic" : 텍스트·도표·지도·설명 그래픽이 주요 내용 (제품 없이 텍스트만, 인포그래픽, 스펙표 등)

혼합인 경우: 제품이 화면의 40% 이상이면 "product", 텍스트/그래픽이 지배적이면 "text_graphic"

이미지 1부터 {len(paths)}까지 순서대로 JSON 배열만 반환 (다른 텍스트 없음):
["product", "text_graphic", ...]"""

        parts.append(types.Part.from_text(text=classify_prompt))

        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=parts)
        raw      = (response.text or "").strip()
        raw      = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw      = re.sub(r"\s*```$",           "", raw)

        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != len(paths):
            raise ValueError(f"분류 결과 길이 불일치: {len(parsed)} != {len(paths)}")

        # "product" / "text_graphic" 이외 값 정규화
        normalized = []
        for v in parsed:
            v = str(v).lower().strip()
            normalized.append("product" if "product" in v else "text_graphic")
        return normalized

    except Exception as e:
        print(f"[ImageReader] 배치 분류 실패: {e} — 전부 product 처리")
        return ["product"] * len(paths)


# ── 헬퍼 ─────────────────────────────────────────────────────────────

def _select_representative(paths: list[str], n: int) -> list[str]:
    """긴 리스트에서 균등 분포로 n개 선택 (첫·끝 포함)."""
    if len(paths) <= n:
        return paths
    if n <= 1:
        return [paths[0]]
    step    = (len(paths) - 1) / (n - 1)
    indices = [round(i * step) for i in range(n)]
    return [paths[i] for i in indices]


def _mime_of(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"):  return "image/png"
    if p.endswith(".webp"): return "image/webp"
    return "image/jpeg"


def _empty_result() -> dict:
    return {
        "product_name": "",
        "specs":        {},
        "descriptions": [],
        "raw_texts":    [],
    }
