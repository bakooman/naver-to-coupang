"""
카테고리 자동 감지 모듈

상품명 키워드 → 쿠팡 카테고리 ID + 상품고시정보 카테고리 자동 매핑.

설계:
  - config/category_map.json 에서 키워드 사전 로드
  - 상품명에 포함된 키워드 중 priority 가장 높은 것 선택
  - category_id 가 비어있으면 빈 문자열 반환 (Wing 수동 선택 필요)
  - 매핑 없으면 ("", "기타 재화") 반환

JSON 구조:
  {
    "keywords": {
      "엔진오일": {"category_id": "78889", "gosisi_cat": "자동차용품...", "priority": 15},
      "향수":     {"category_id": "",      "gosisi_cat": "화장품...",    "priority": 15},
      ...
    }
  }

런타임 업데이트:
  update(keyword, category_id, gosisi_cat) → 메모리 + JSON 파일 동시 갱신
  → GUI에서 카테고리 ID 입력 시 즉시 저장
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional


# ── 기본 경로 ─────────────────────────────────────────────────────
_DEFAULT_MAP_PATH = Path(__file__).parent.parent / "config" / "category_map.json"
# 자동저장 항목 전용 파일 (gitignore 대상 — git 충돌 방지)
_AUTO_MAP_PATH    = Path(__file__).parent.parent / "config" / "category_auto.json"
_WING_CAT_FILES   = [
    Path(__file__).parent.parent / "config" / "wing_categories.json",
    Path(__file__).parent.parent / "config" / "wing_beauty_categories.json",
]

# wing_categories 에서 대분류 경로 → gosisi_cat 매핑
_PATH_TO_GOSISI = {
    "자동차용품":   "자동차용품 (자동차부품/기타 자동차용품 등)",
    "뷰티":        "화장품 (화장품법에 의한 화장품, 기능성화장품 포함)",
    "식품":        "가공식품 (식품위생법에 의한 가공식품)",
    "건강기능식품": "건강기능식품 (건강기능식품에 관한 법률에 의한 건강기능식품)",
    "생활용품":    "생활용품 (공산품)",
    "스포츠":      "기타 재화",
    "가전":        "기타 재화",
    "완구":        "기타 재화",
}

# fallback 검색 시 제외할 너무 일반적인 카테고리 이름
# ※ 상품 수식어로도 쓰이는 단어(클래식, 프리미엄 등)는 반드시 여기에 추가
_SKIP_NAMES = {
    "기타", "일반", "세트", "선물세트", "기타용품", "기타소품",
    "기타소재", "기타잡화", "기타식품", "기타제품", "기타화장품",
    # 상품 수식어/브랜드명과 겹치는 카테고리명 → 오매칭 방지
    "클래식", "프리미엄", "스탠다드", "베이직", "스페셜", "리미티드",
    "골드", "실버", "블랙", "화이트", "레드", "블루", "그린",
    "미니", "맥시", "라지", "스몰", "플러스", "프로", "라이트",
    # 포장 형태/용기 단어 → 엉뚱한 카테고리(가전 등)에 매칭되는 오류 방지
    "파우치", "캔", "병", "팩", "박스", "봉투", "튜브", "캡슐",
    "스틱", "시트", "롤", "컵", "트레이", "백", "케이스",
    # 과일/플레이버 단어 → 농산물 카테고리 오매칭 방지
    # (사탕·음료·과자 상품명에 플레이버로 쓰이는 경우가 압도적으로 많음)
    "복숭아", "딸기", "사과", "레몬", "오렌지", "포도", "체리",
    "망고", "바나나", "멜론", "수박", "파인애플", "블루베리", "라즈베리",
    # 소스/간장류 단어 → 주방기기 등 엉뚱한 카테고리 방지
    "간장", "소스", "타레", "드레싱",
    # 일본/해외 식품 브랜드명 → 브랜드가 카테고리로 오매칭 방지
    "하우스", "닛신", "모리나가", "롯데", "가루비", "칼비",
    "아지노모토", "기린", "삿포로", "아사히", "메이지",
    "글리코", "야마자키", "닛토", "에스비", "오타후쿠",
    # 조리/제조 방법 단어 → 관련 없는 카테고리 오매칭 방지
    "믹스", "파우더", "분말", "액상", "농축",
}

# 캐시된 flat 카테고리 목록
_wing_flat_cache: list[dict] | None = None


def _load_wing_flat() -> list[dict]:
    """wing_categories.json들을 flat 리스트로 로드 (캐시)."""
    global _wing_flat_cache
    if _wing_flat_cache is not None:
        return _wing_flat_cache
    result: list[dict] = []
    for fpath in _WING_CAT_FILES:
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(data, list):
                result.extend(data)
        except Exception:
            pass
    _wing_flat_cache = result
    return result


def _gosisi_from_path(path: str) -> str:
    """path(ROOT>자동차용품>...) 에서 gosisi_cat 추출."""
    parts = path.split(">")
    if len(parts) >= 2:
        top = parts[1]
        for k, v in _PATH_TO_GOSISI.items():
            if k in top:
                return v
    return "기타 재화"


class CategoryDetector:
    """키워드 기반 카테고리 자동 감지기."""

    def __init__(self, map_path: str | Path | None = None):
        self._map_path = Path(map_path or _DEFAULT_MAP_PATH)
        self._keywords: dict[str, dict] = {}   # keyword → {category_id, gosisi_cat, priority}
        self._load()

    # ── Public ─────────────────────────────────────────────────────

    def detect(
        self,
        product_name: str,
        naver_category: str = "",
    ) -> tuple[str, str, str]:
        """
        상품명(+선택적 네이버 카테고리)에서 쿠팡 카테고리 자동 감지.

        Returns:
            (category_id, gosisi_cat, matched_keyword)
            매핑 없으면 ("", "기타 재화", "")
        """
        # 정규화: 소문자 + 공백·하이픈 제거
        # "5W-30" → "5w30", "0W-20" → "0w20" 등 하이픈 표기 통일
        def _norm(s: str) -> str:
            return s.lower().replace(" ", "").replace("-", "")

        combined = _norm(f"{product_name} {naver_category}")

        best: Optional[dict] = None
        best_kw = ""
        best_priority = -1

        for kw, data in self._keywords.items():
            # 구분자/주석 키 건너뜀
            if kw.startswith("──") or not isinstance(data, dict):
                continue
            if not data.get("category_id") and not data.get("gosisi_cat"):
                continue

            # 키워드를 정규화 (공백·하이픈 제거) 하여 상품명에 포함 여부 확인
            kw_norm = _norm(kw)
            if kw_norm not in combined:
                continue
            # 순수 한글 키워드가 복합어 내부 substring으로 오매핑되는 것을 방지.
            # 원본 문자열에서 키워드가 독립 단어로 한 번이라도 등장하지 않으면 skip.
            # 앞뒤 중 어느 한 쪽이라도 한글이 붙으면 복합어 내부로 판단.
            # 예) '린스' in '프린스'    → 앞에 '프' → standalone 없음 → skip ✅
            #     '파이' in '리파이너'  → 앞뒤 모두 한글 → standalone 없음 → skip ✅
            #     '파이' in '마카롱 파이' → 앞(공백) → standalone 존재 → pass ✅
            #     '젤리' in '하리보젤리 젤리' → naver_cat에 standalone '젤리' → pass ✅
            #     '[자동]인덕션' in '인덕션용냄비' → '용' 뒤 한글 → skip ✅
            # [자동]xxx 형태 키워드에서 한글 코어만 추출해 standalone 체크
            _kw_core = kw
            if kw.startswith('[') and ']' in kw:
                _kw_core = kw[kw.index(']') + 1:]
            if re.fullmatch(r'[가-힣]+', _kw_core):
                _orig = f"{product_name} {naver_category}"
                _standalone = re.search(
                    r'(?<![가-힣])' + re.escape(_kw_core) + r'(?![가-힣])',
                    _orig,
                )
                if not _standalone:
                    continue

            p = int(data.get("priority", 10))
            # 우선순위가 같으면 더 긴(더 구체적) 키워드 선택
            if p > best_priority or (p == best_priority and len(kw) > len(best_kw)):
                best = data
                best_kw = kw
                best_priority = p

        if best:
            return (
                best.get("category_id", ""),
                best.get("gosisi_cat", "기타 재화"),
                best_kw,
            )

        # ── Fallback: wing_categories.json 전체 세분류 이름 검색 ──────
        fb = self._fallback_wing_search(product_name)
        if fb:
            return fb

        return ("", "기타 재화", "")

    def update(
        self,
        keyword: str,
        category_id: str,
        gosisi_cat: str,
        priority: int | None = None,
    ) -> None:
        """
        키워드의 category_id / gosisi_cat 업데이트 + JSON 파일 저장.
        keyword 가 없으면 새로 추가.
        """
        if keyword not in self._keywords:
            self._keywords[keyword] = {
                "category_id": category_id,
                "gosisi_cat":  gosisi_cat,
                "priority":    priority or 10,
            }
        else:
            if category_id:
                self._keywords[keyword]["category_id"] = category_id
            if gosisi_cat:
                self._keywords[keyword]["gosisi_cat"] = gosisi_cat
            if priority is not None:
                self._keywords[keyword]["priority"] = priority
        self._save()

    def all_keywords(self) -> list[dict]:
        """
        UI 표시용: 실제 키워드(주석 제외) 목록 반환.
        """
        result = []
        for kw, data in self._keywords.items():
            if kw.startswith("──") or not isinstance(data, dict):
                continue
            if not data.get("gosisi_cat"):
                continue
            result.append({
                "keyword":     kw,
                "category_id": data.get("category_id", ""),
                "gosisi_cat":  data.get("gosisi_cat", ""),
                "priority":    data.get("priority", 10),
            })
        return sorted(result, key=lambda x: (-x["priority"], x["keyword"]))

    # ── Private ────────────────────────────────────────────────────

    def _fallback_wing_search(
        self, product_name: str
    ) -> tuple[str, str, str] | None:
        """
        wing_categories.json 전체를 검색해 상품명에 카테고리 세분류 이름이
        포함된 항목을 자동 매핑.

        매칭 기준:
          - 카테고리명(name)이 상품명에 포함되어야 함
          - 너무 짧거나(2자 이하) 일반적인 이름은 제외
          - 여러 매칭 시 이름이 긴(더 구체적) 것 우선
        Returns:
          (category_id, gosisi_cat, matched_name) or None
        """
        flat = _load_wing_flat()
        if not flat:
            return None

        pname_norm = product_name.lower().replace(" ", "").replace("-", "")
        best_entry: dict | None = None
        best_len = 0

        for entry in flat:
            name: str = entry.get("name", "")
            code: str = entry.get("code", "")
            if not name or not code:
                continue
            if name in _SKIP_NAMES or len(name) < 2:
                continue
            # 카테고리명 정규화 후 상품명에 포함 여부 확인
            name_norm = name.lower().replace(" ", "").replace("-", "")
            if name_norm not in pname_norm:
                continue
            # 순수 한글 카테고리명: 복합어 내부 substring 오매핑 방지
            if re.fullmatch(r'[가-힣]+', name):
                if not re.search(
                    r'(?<![가-힣])' + re.escape(name) + r'(?![가-힣])',
                    product_name,
                ):
                    continue
            # 영문 포함 카테고리명: 단어 경계 체크 ("Anger" in "Varanger" 방지)
            elif re.search(r'[A-Za-z]', name):
                if not re.search(
                    r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])',
                    product_name,
                    re.IGNORECASE,
                ):
                    continue
            if len(name_norm) > best_len:
                best_entry = entry
                best_len = len(name_norm)

        if best_entry:
            code     = best_entry["code"]
            name     = best_entry["name"]
            path     = best_entry.get("path", "")
            gosisi   = _gosisi_from_path(path)
            print(f"[CategoryDetector] Fallback 매칭: '{name}' (ID:{code}) ← {path}")
            return (code, gosisi, f"[자동]{name}")

        return None

    def search_by_keyword(
        self, keyword: str, top_k: int = 1
    ) -> tuple[str, str, str] | None:
        """
        Gemini 폴백 전용 — 카테고리 키워드로 wing DB를 직접 검색.

        기존 _fallback_wing_search 와의 차이:
          - "상품명에 카테고리명 포함" 제약 없음
          - 대신 keyword ↔ 카테고리명 간 유사도(토큰 겹침 + 부분 포함) 기반 매핑
          - 유사도 동점 시 카테고리명이 더 긴(구체적) 것 우선

        유사도 계산 (점수 0~1):
          1. 완전 일치(정규화): 1.0
          2. keyword ⊆ name  or  name ⊆ keyword (정규화): 0.8
          3. 토큰 교집합 비율 (Jaccard): len(교집합) / len(합집합)
          최종 점수 = max(위 세 가지)

        Returns:
          (category_id, gosisi_cat, matched_name) or None (점수 0.3 미만 제외)
        """
        # 0순위: category_map.json 직접 키워드 매핑 (wing 검색보다 우선)
        # Gemini가 반환한 카테고리명이 사전에 있으면 즉시 사용 (오매핑 방지)
        _kw_lower = keyword.strip().lower()
        for _mk, _md in self._keywords.items():
            if _mk.startswith("──") or not isinstance(_md, dict):
                continue
            if _mk.strip().lower() == _kw_lower and _md.get("category_id"):
                return (_md["category_id"], _md.get("gosisi_cat", "기타 재화"), _mk)

        flat = _load_wing_flat()
        if not flat or not keyword.strip():
            return None

        def _norm(s: str) -> str:
            return s.lower().replace(" ", "").replace("-", "")

        def _tokens(s: str) -> set[str]:
            # 2자 이상 토큰만 사용
            return {t for t in re.split(r'[\s/·,]+', s.lower()) if len(t) >= 2}

        kw_norm   = _norm(keyword)
        kw_tokens = _tokens(keyword)

        best_entry: dict | None = None
        best_score = 0.0
        best_len   = 0

        for entry in flat:
            name: str = entry.get("name", "")
            code: str = entry.get("code", "")
            if not name or not code:
                continue
            if name in _SKIP_NAMES or len(name) < 2:
                continue

            name_norm   = _norm(name)
            name_tokens = _tokens(name)

            # 1. 완전 일치
            if kw_norm == name_norm:
                score = 1.0

            # 2. 토큰 단위 포함 — 단어 경계에서만 허용
            # ❌ "크림" in "아이스크림스푼" 처럼 복합어 내부 substring 오매칭 방지
            # ✅ 토큰으로 쪼갰을 때 keyword 토큰이 name 토큰 집합에 완전히 포함될 때만
            # ※ 비율이 낮으면 점수 감소 — 예) '젤리' 1개가 4토큰 카테고리에 포함될 때
            #    0.8 * (1/4) = 0.2 → 임계값 0.5 미달 → '크림/젤리/쿠션 아이섀도' 오매핑 방지
            elif kw_tokens and kw_tokens.issubset(name_tokens):
                _ratio = len(kw_tokens) / max(len(name_tokens), 1)
                score = 0.8 * _ratio if _ratio < 0.5 else 0.8
            # name 전체가 keyword 안에 포함(name이 더 짧은 경우) — 완전 토큰 일치
            elif name_tokens and name_tokens.issubset(kw_tokens):
                score = 0.75
            # 3. 토큰 Jaccard 유사도
            elif kw_tokens and name_tokens:
                inter = kw_tokens & name_tokens
                union = kw_tokens | name_tokens
                score = len(inter) / len(union) if union else 0.0
            else:
                score = 0.0

            if score > best_score or (score == best_score and len(name_norm) > best_len):
                best_score = score
                best_entry = entry
                best_len   = len(name_norm)

        _MIN_SCORE = 0.5
        if best_entry and best_score >= _MIN_SCORE:
            code   = best_entry["code"]
            name   = best_entry["name"]
            path   = best_entry.get("path", "")
            gosisi = _gosisi_from_path(path)
            print(f"[CategoryDetector] Gemini 유사도 매핑: '{keyword}' → '{name}' "
                  f"(ID:{code}, score:{best_score:.2f})")
            return (code, gosisi, name)  # prefix는 호출부에서 추가

        return None

    def _load(self) -> None:
        # 1) category_map.json (수동 관리 — git 동기화 대상)
        try:
            raw = json.loads(self._map_path.read_text(encoding="utf-8"))
            self._keywords = raw.get("keywords", {})
            print(f"[CategoryDetector] 매핑 로드: {self._map_path.name} "
                  f"({sum(1 for k in self._keywords if not k.startswith('──'))}개 키워드)")
        except FileNotFoundError:
            print(f"[CategoryDetector] 매핑 파일 없음: {self._map_path} → 빈 사전으로 시작")
            self._keywords = {}
        except Exception as e:
            print(f"[CategoryDetector] 매핑 파일 로드 오류: {e}")
            self._keywords = {}

        # 2) category_auto.json (자동저장 — 수동 항목보다 낮은 우선순위)
        try:
            auto_raw = json.loads(_AUTO_MAP_PATH.read_text(encoding="utf-8"))
            auto_kws = auto_raw.get("keywords", {})
            added = 0
            for kw, data in auto_kws.items():
                if kw not in self._keywords:   # 수동 항목이 항상 우선
                    self._keywords[kw] = data
                    added += 1
            if auto_kws:
                print(f"[CategoryDetector] 자동저장 로드: category_auto.json "
                      f"({added}개 추가, {len(auto_kws) - added}개 수동 항목에 가려짐)")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[CategoryDetector] 자동저장 로드 오류 (무시): {e}")

    def get_name_by_id(self, category_id: str) -> str:
        """category_id로 wing_categories에서 카테고리 이름을 반환. 없으면 빈 문자열."""
        flat = _load_wing_flat()
        for entry in flat:
            if entry.get("code") == category_id:
                return entry.get("name", "")
        return ""

    def _save(self) -> None:
        # [자동]/[네이버카테고리]/[Gemini] 접두 항목 → category_auto.json (gitignore)
        # 그 외 수동 항목 → category_map.json (git 동기화)
        _AUTO_PREFIXES = ("[자동]", "[네이버카테고리]", "[Gemini")
        manual_kws: dict = {}
        auto_kws:   dict = {}
        for kw, data in self._keywords.items():
            if any(kw.startswith(p) for p in _AUTO_PREFIXES):
                auto_kws[kw] = data
            else:
                manual_kws[kw] = data

        # category_map.json — 수동 항목만
        try:
            try:
                raw = json.loads(self._map_path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            raw["keywords"] = manual_kws
            self._map_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[CategoryDetector] 매핑 저장 오류 (수동): {e}")

        # category_auto.json — 자동저장 항목만
        try:
            _AUTO_MAP_PATH.write_text(
                json.dumps({"keywords": auto_kws}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[CategoryDetector] 자동저장 저장 오류 (무시): {e}")

        print(f"[CategoryDetector] 저장 완료: 수동 {len(manual_kws)}개 / 자동 {len(auto_kws)}개")


# ── 전역 싱글턴 ────────────────────────────────────────────────────
_detector: Optional[CategoryDetector] = None


def get_detector() -> CategoryDetector:
    """전역 싱글턴 CategoryDetector 반환 (최초 호출 시 초기화)."""
    global _detector
    if _detector is None:
        _detector = CategoryDetector()
    return _detector
