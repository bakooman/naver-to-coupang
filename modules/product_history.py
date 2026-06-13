"""
등록 이력 관리 + 중복 상품 감지 모듈

저장 시점: Wing 자동판매요청 성공 시
비교 흐름:
  1단계 — 브랜드 + 카테고리 일치 필터
  2단계 — 텍스트 유사도 70% 이상 필터 (Jaccard + 공통 토큰 비율)
  3단계 — Gemini 판별 (DUPLICATE / VARIANT / UNKNOWN)
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

_HISTORY_PATH = Path(__file__).parent.parent / "data" / "registered_history.json"


# ── 텍스트 정규화 ─────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """공백·특수문자 제거, 소문자 변환."""
    return re.sub(r"[^\w가-힣]", "", text).lower()


def _tokenize(text: str) -> set[str]:
    """2자 이상 토큰 분리 (공백·구두점 기준)."""
    return {t for t in re.split(r"[\s,·/·]+", text.lower()) if len(t) >= 2}


def _text_similarity(a: str, b: str) -> float:
    """
    Jaccard 유사도 (토큰 기반) + 정규화 문자열 공통 부분 비율 중 최댓값.

    두 방법을 모두 쓰는 이유:
    - Jaccard: 단어 순서 무관, 토큰 단위 비교
    - 공통 부분 비율: 토큰 분리가 안 되는 붙여쓴 상품명 처리
    """
    if not a or not b:
        return 0.0

    # Jaccard
    ta, tb = _tokenize(a), _tokenize(b)
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
    else:
        jaccard = 0.0

    # 정규화 공통 부분
    na, nb = _normalize(a), _normalize(b)
    longer  = na if len(na) >= len(nb) else nb
    shorter = nb if len(na) >= len(nb) else na
    overlap = len(shorter) / len(longer) if (shorter in longer and longer) else 0.0

    return max(jaccard, overlap)


# ── 이력 파일 I/O ────────────────────────────────────────────────

def _load_history() -> list[dict]:
    try:
        if _HISTORY_PATH.exists():
            raw = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
    except Exception as e:
        print(f"[ProductHistory] 이력 파일 로드 오류: {e}")
    return []


def _save_history(products: list[dict]) -> None:
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_PATH.write_text(
            json.dumps(products, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[ProductHistory] 이력 파일 저장 오류: {e}")


# ── Public API ────────────────────────────────────────────────────

def add_to_history(
    brand: str,
    product_name: str,
    volume: str,
    category_id: str,
    naver_url: str,
    wing_inv_id: str = "",
    gtin: str = "",
) -> None:
    """
    판매요청 성공 시 이력에 추가.
    동일 naver_url이 이미 있으면 갱신(재등록 케이스).
    """
    products = _load_history()
    # 기존 URL 중복 제거
    products = [p for p in products if p.get("naver_url") != naver_url]
    products.append({
        "id":           uuid.uuid4().hex[:8],
        "brand":        brand,
        "product_name": product_name,
        "volume":       volume,
        "category_id":  category_id,
        "naver_url":    naver_url,
        "wing_inv_id":  wing_inv_id,
        "gtin":         gtin,
        "registered_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_history(products)
    print(f"[ProductHistory] 이력 저장: {brand} / {product_name}")


def find_candidates(
    brand: str,
    category_id: str,
    product_name: str,
    threshold: float = 0.25,   # 70% → 25%: 이름이 달라도 Gemini까지 보냄
    gtin: str = "",
) -> list[dict]:
    """
    Stage 0: GTIN 일치 → 즉시 반환 (완벽한 중복)
    Stage 1: 브랜드+카테고리 일치 필터
    Stage 2: 텍스트 유사도 ≥ 25% (낮춰서 Gemini가 최종 판별하게 함)
    각 후보에 similarity 점수 추가해 반환.
    """
    products = _load_history()
    candidates: list[dict] = []

    _brand_norm = _normalize(brand)
    _cat_id     = str(category_id or "")
    _gtin       = gtin.strip() if gtin else ""

    # ── Stage 0: GTIN 바코드 직접 매칭 ──────────────────────────
    if _gtin:
        for p in products:
            if p.get("gtin", "").strip() == _gtin and p.get("naver_url", "") != "":
                print(f"[ProductHistory] Stage0 GTIN 일치: {_gtin} → {p.get('product_name')}")
                return [{**p, "_similarity": 1.0, "_gtin_match": True}]

    # ── Stage 1+2: 브랜드+카테고리 일치 + 유사도 필터 ──────────
    for p in products:
        if _normalize(p.get("brand", "")) != _brand_norm:
            continue
        if _cat_id and p.get("category_id") and p["category_id"] != _cat_id:
            continue
        sim = _text_similarity(product_name, p.get("product_name", ""))
        if sim >= threshold:
            candidates.append({**p, "_similarity": round(sim, 3)})

    candidates.sort(key=lambda x: x["_similarity"], reverse=True)
    return candidates[:3]  # 최대 3개만 Gemini에 보냄 (속도 보장)


def check_with_gemini(
    new_name: str,
    past_name: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
) -> dict:
    """
    3단계: Gemini로 두 상품이 동일 구성인지 변형 상품인지 판별.

    Returns:
        {
            "verdict":    "DUPLICATE" | "VARIANT" | "UNKNOWN",
            "reason":     str,   # 20자 이내 이유
            "confidence": "high" | "low",
        }
    """
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        prompt = (
            "두 상품명이 주어집니다. 아래 기준으로 정확히 판별하세요.\n\n"
            f"[과거 등록 상품]: {past_name}\n"
            f"[새로 수집한 상품]: {new_name}\n\n"
            "판별 기준:\n"
            "- 브랜드, 제품 라인, 용량/중량, 맛/향, 수량/구성이 모두 같으면 → DUPLICATE\n"
            "- 위 항목 중 하나라도 다르면 (예: 5개입 vs 40개입, 500ml vs 1L, 레몬향 vs 민트향) → VARIANT\n"
            "- 판단이 불가능하면 → UNKNOWN\n\n"
            "반드시 아래 형식으로만 답하세요 (다른 텍스트 금지):\n"
            "DUPLICATE|이유(20자 이내)\n"
            "또는\n"
            "VARIANT|이유(20자 이내)\n"
            "또는\n"
            "UNKNOWN|이유(20자 이내)"
        )

        resp = genai.GenerativeModel(model).generate_content(prompt)
        raw  = (resp.text or "").strip().split("\n")[0].strip()

        if "|" in raw:
            verdict_raw, reason = raw.split("|", 1)
            verdict = verdict_raw.strip().upper()
        else:
            verdict = raw.upper()
            reason  = ""

        if verdict not in ("DUPLICATE", "VARIANT", "UNKNOWN"):
            verdict = "UNKNOWN"
            reason  = f"응답 파싱 실패: {raw[:30]}"

        confidence = "high" if verdict in ("DUPLICATE", "VARIANT") else "low"
        return {"verdict": verdict, "reason": reason.strip()[:30], "confidence": confidence}

    except Exception as e:
        print(f"[ProductHistory] Gemini 판별 오류: {e}")
        return {"verdict": "UNKNOWN", "reason": f"Gemini 오류: {str(e)[:20]}", "confidence": "low"}


def run_duplicate_check(
    brand: str,
    category_id: str,
    product_name: str,
    api_key: str = "",
    model: str = "gemini-2.5-flash",
    similarity_threshold: float = 0.25,
    gtin: str = "",
) -> dict:
    """
    전체 중복 감지 파이프라인 (1~3단계 통합).

    Returns:
        {
            "status":  "clean" | "duplicate" | "variant" | "unknown",
            "matched": dict | None,   # 매칭된 이력 상품
            "reason":  str,
        }
    """
    candidates = find_candidates(brand, category_id, product_name, similarity_threshold, gtin=gtin)
    if not candidates:
        return {"status": "clean", "matched": None, "reason": "이력에 유사 상품 없음"}

    best = candidates[0]  # 유사도 최상위 후보

    # Stage 0 GTIN 일치 → Gemini 불필요, 즉시 DUPLICATE
    if best.get("_gtin_match"):
        return {
            "status":  "duplicate",
            "matched": best,
            "reason":  f"바코드 일치 (GTIN: {gtin})",
        }

    # Gemini API 키 없으면 유사도만으로 경고
    if not api_key:
        return {
            "status":  "unknown",
            "matched": best,
            "reason":  f"유사 상품 감지 (유사도 {best['_similarity']:.0%}) — Gemini 미설정으로 수동 확인 필요",
        }

    # 3단계: Gemini 판별
    gemini_result = check_with_gemini(product_name, best["product_name"], api_key, model)
    verdict = gemini_result["verdict"]
    reason  = gemini_result["reason"]

    if verdict == "DUPLICATE":
        return {
            "status":  "duplicate",
            "matched": best,
            "reason":  reason or "Gemini: 동일 구성 상품",
        }
    elif verdict == "VARIANT":
        return {
            "status":  "variant",
            "matched": best,
            "reason":  reason or "Gemini: 다른 변형 상품",
        }
    else:
        return {
            "status":  "unknown",
            "matched": best,
            "reason":  reason or "Gemini: 판별 불가 — 수동 확인 권고",
        }


def get_history_count() -> int:
    """저장된 이력 상품 수 반환."""
    return len(_load_history())
