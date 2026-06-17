"""
오류 엑셀 자동 수정 모듈

Wing 일괄등록 Excel을 파싱 → Gemini로 카테고리/브랜드 오매핑 감지 → 수정 Excel 재출력.
"""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from openpyxl import load_workbook
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False


# ── 데이터 모델 ────────────────────────────────────────────────────

@dataclass
class FixResult:
    product_name:      str
    old_category_id:   str
    old_category_name: str
    new_category_id:   str
    new_category_name: str
    new_gosisi_cat:    str    = ""
    old_brand:         str    = ""
    new_brand:         str    = ""
    needs_fix:         bool   = False
    reason:            str    = ""
    brand_locked:      bool   = False
    row_indices:       list[int] = field(default_factory=list)


# ── 헤더 감지 (ExcelBuilder와 동일 로직) ──────────────────────────

_HEADER_KW = frozenset([
    "카테고리", "등록상품명", "브랜드", "판매가격",
    "옵션유형", "옵션값", "재고수량", "출고리드타임",
])


def _find_header_row(ws) -> Optional[int]:
    best_row, best_cnt = None, 0
    for row in ws.iter_rows(min_row=1, max_row=10):
        cnt = 0
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            val = cell.value.strip()
            if len(val) > 20:
                continue
            if any(kw in val for kw in _HEADER_KW):
                cnt += 1
        if cnt > best_cnt:
            best_cnt, best_row = cnt, row[0].row
    return best_row if best_cnt >= 3 else None


def _map_columns(ws, header_row: int) -> dict[str, int]:
    """헤더 행 → 내부 키 → 열 인덱스(1-based) 매핑."""
    col_map: dict[str, int] = {}
    ot_idx = ov_idx = 0
    for cell in ws[header_row]:
        if not isinstance(cell.value, str):
            continue
        v = cell.value.strip()
        # 우선순위: 상품고시정보 카테고리 > 카테고리아이디
        if "상품고시정보 카테고리" in v:
            col_map["_gosisi_cat"] = cell.column
        elif "카테고리" in v and "상품고시정보" not in v and "category_id" not in col_map:
            col_map["category_id"] = cell.column
        elif "등록상품명" in v and "product_name" not in col_map:
            col_map["product_name"] = cell.column
        elif "브랜드" in v and "brand" not in col_map and "제조사" not in v:
            col_map["brand"] = cell.column
        elif "옵션유형" in v and ot_idx < 3:
            col_map[f"_option_type_{ot_idx}"] = cell.column
            ot_idx += 1
        elif "옵션값" in v and ov_idx < 3:
            col_map[f"_option_val_{ov_idx}"] = cell.column
            ov_idx += 1
    return col_map


# ── Excel 파싱 ────────────────────────────────────────────────────

def parse_excel(src_path: Path) -> tuple[dict, str, list[dict], int]:
    """
    Wing Excel 파싱.

    Returns:
        (col_map, src_ext, products, header_row)
        products: 상품명 기준으로 그룹핑된 dict 리스트
          각 dict: {product_name, category_id, brand, options, row_indices}
    """
    if not OPENPYXL_OK:
        raise RuntimeError("openpyxl 미설치: pip install openpyxl")

    src_ext = src_path.suffix.lower() or ".xlsx"
    wb = load_workbook(str(src_path), keep_vba=(src_ext == ".xlsm"), data_only=True)
    ws = wb.active

    header_row = _find_header_row(ws)
    if not header_row:
        raise ValueError("헤더 행을 찾을 수 없습니다. Wing 일괄등록 Excel인지 확인하세요.")

    col_map = _map_columns(ws, header_row)
    if "product_name" not in col_map:
        raise ValueError("'등록상품명' 컬럼을 찾을 수 없습니다.")

    def _get(row_idx: int, key: str) -> str:
        col = col_map.get(key)
        if not col:
            return ""
        v = ws.cell(row=row_idx, column=col).value
        return str(v).strip() if v is not None else ""

    data_start = header_row + 3
    seen: dict[str, dict] = {}

    for r in range(data_start, ws.max_row + 1):
        pname = _get(r, "product_name")
        if not pname:
            continue
        if pname not in seen:
            opts: list[tuple[str, str]] = []
            for i in range(3):
                ot = _get(r, f"_option_type_{i}")
                ov = _get(r, f"_option_val_{i}")
                if ot:
                    opts.append((ot, ov))
            seen[pname] = dict(
                product_name=pname,
                category_id=_get(r, "category_id"),
                brand=_get(r, "brand"),
                options=opts,
                row_indices=[],
            )
        seen[pname]["row_indices"].append(r)

    wb.close()
    return col_map, src_ext, list(seen.values()), header_row


# ── Gemini 검수 ───────────────────────────────────────────────────

def gemini_check(product: dict, api_key: str, model: str) -> dict:
    """
    Gemini로 상품의 카테고리/브랜드 정합성 검수.

    Returns:
        {needs_fix, category_keyword, brand, reason}
    """
    pname    = product["product_name"]
    cat_id   = product.get("category_id", "")
    cat_name = product.get("_cat_name", "")
    brand    = product.get("brand", "")
    options  = product.get("options", [])
    opts_str = ", ".join(f"{t}={v}" for t, v in options) if options else "없음"

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        prompt = (
            "쿠팡 Wing 일괄등록 엑셀 상품 데이터를 검수합니다.\n\n"
            f"상품명: {pname}\n"
            f"카테고리 ID: {cat_id} / 카테고리명: {cat_name or '미확인'}\n"
            f"브랜드: {brand or '없음'}\n"
            f"옵션: {opts_str}\n\n"
            "⚠️ 필수 분류 규칙:\n"
            "1. 상품명에 반려동물 키워드(강아지/고양이/펫/pet/dog/cat/멍/냥)와 "
            "건강보조 키워드(영양제/유산균/오메가/관절/눈건강/장건강/피부/면역/칼슘)가 "
            "함께 있으면 반드시 '강아지영양제' 또는 '고양이영양제' 카테고리 키워드로 분류. "
            "절대로 인체용 '건강기능식품' 카테고리 금지.\n"
            "2. 젤리/사탕/과자/구미/캔디/스낵/초콜릿 → '식품간식' 카테고리. "
            "절대 화장품·뷰티 카테고리 금지.\n"
            "3. 현재 카테고리명이 상품과 명백히 불일치할 때만 needs_fix=true. "
            "카테고리명이 비어있거나 미확인이면 상품명으로 올바른 카테고리를 추정.\n"
            "4. 브랜드가 맞으면 원본 그대로 반환. 확실치 않으면 원본 유지.\n\n"
            "아래 JSON 형식으로만 답하세요. 다른 텍스트 절대 금지:\n"
            '{"needs_fix": true또는false, '
            '"category_keyword": "올바른카테고리한국어키워드", '
            '"brand": "올바른브랜드명", '
            '"reason": "이유15자이내"}'
        )

        resp = genai.GenerativeModel(model).generate_content(prompt)
        raw  = (resp.text or "").strip()
        m    = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            # Sanitize
            if not isinstance(result.get("needs_fix"), bool):
                result["needs_fix"] = False
            result.setdefault("category_keyword", "")
            result.setdefault("brand", brand)
            result.setdefault("reason", "")
            return result

    except json.JSONDecodeError:
        pass
    except Exception as e:
        print(f"[ExcelFixer] Gemini 오류 ({pname[:20]}): {e}")

    return {"needs_fix": False, "category_keyword": "", "brand": brand, "reason": ""}


# ── 수정 적용 & 저장 ─────────────────────────────────────────────

def apply_and_save(
    src_path: Path,
    col_map: dict,
    fix_results: list[FixResult],
    output_dir: Path,
    src_ext: str = ".xlsx",
) -> Path:
    """
    원본 파일을 재로드하여 수정사항을 적용하고 저장.
    brand_locked=True인 항목은 브랜드 변경 차단.
    """
    if not OPENPYXL_OK:
        raise RuntimeError("openpyxl 미설치")

    wb = load_workbook(str(src_path), keep_vba=(src_ext == ".xlsm"))
    ws = wb.active

    for fr in fix_results:
        cat_changed   = fr.new_category_id and fr.new_category_id != fr.old_category_id
        brand_changed = (not fr.brand_locked) and fr.new_brand and fr.new_brand != fr.old_brand

        if not cat_changed and not brand_changed:
            continue

        for r in fr.row_indices:
            if cat_changed and "category_id" in col_map:
                raw = fr.new_category_id.strip()
                ws.cell(row=r, column=col_map["category_id"],
                        value=int(raw) if raw.isdigit() else raw)
            if cat_changed and fr.new_gosisi_cat and "_gosisi_cat" in col_map:
                ws.cell(row=r, column=col_map["_gosisi_cat"], value=fr.new_gosisi_cat)
            if brand_changed and "brand" in col_map:
                ws.cell(row=r, column=col_map["brand"], value=fr.new_brand)

    output_dir.mkdir(parents=True, exist_ok=True)
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"fixed_{ts}{src_ext}"
    wb.save(str(out_path))
    print(f"[ExcelFixer] 수정 저장: {out_path}")
    return out_path
