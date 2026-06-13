# -*- coding: utf-8 -*-
"""
Extract per-category options and gosisi_cat from Coupang category guide xlsx files.
Output: config/category_options.json
"""
import pandas as pd, json, re, os

GUIDE_FOLDER = r'C:\Users\pp\Desktop\해구대\Coupang_Category_20260311_1413 (1)'
CAT_MAP_PATH  = r'C:\Users\pp\Desktop\naver to coupang\config\category_map.json'
OUT_PATH      = r'C:\Users\pp\Desktop\naver to coupang\config\category_options.json'

files = [f for f in os.listdir(GUIDE_FOLDER) if f.endswith('.xlsx')]

result = {}

for fname in files:
    path = os.path.join(GUIDE_FOLDER, fname)
    try:
        df = pd.read_excel(path, sheet_name='data', header=None, engine='openpyxl')
    except Exception:
        try:
            df = pd.read_excel(path, header=None, engine='openpyxl')
        except Exception as e:
            print(f'SKIP {fname}: {e}', flush=True)
            continue

    ncols = df.shape[1]

    # Row 2 has 필수/선택필수 markings for purchase option columns
    req_row = df.iloc[2]

    # Data rows start at index 4
    data_rows = df.iloc[4:]

    for _, row in data_rows.iterrows():
        cat_str = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ''
        if not cat_str or cat_str == 'nan':
            continue

        # Extract category ID from bracket like [73014]
        m = re.search(r'\[(\d+)\]', cat_str)
        if not m:
            m = re.search(r'(\d{4,6})\s*$', cat_str)
        if not m:
            continue
        cat_id = m.group(1)

        # Purchase option types at cols 2, 4, 6, 8 (4 pairs: type + value)
        opt_types = []
        req_types = []
        for ci in [2, 4, 6, 8]:
            if ci >= ncols:
                continue
            t = row.iloc[ci]
            if pd.isna(t):
                continue
            t = str(t)
            if t == 'nan':
                continue
            # Cell may contain: "엔진오일 SAE점도\n[필수]\n[기본단위: ...]"
            # or "(택1) 개당 용량\n[필수]\n[기본단위: ml]"
            t_clean = t.split('\n')[0].strip()
            if not t_clean:
                continue
            # Strip (택N) prefix so we get the actual option type name
            # "(택1) 개당 용량" → "개당 용량"
            t_normalized = re.sub(r'^\(택\d+\)\s*', '', t_clean)
            if t_normalized not in opt_types:
                opt_types.append(t_normalized)
            req_mark = str(req_row.iloc[ci]) if pd.notna(req_row.iloc[ci]) else ''
            if '필수' in req_mark and t_normalized not in req_types:
                req_types.append(t_normalized)

        # gosisi_cat at col 150 (empty in guide data rows — filled by user)
        # We'll populate from category_map.json later
        gosisi = ''
        if ncols > 150:
            v = row.iloc[150]
            if pd.notna(v) and str(v) != 'nan':
                gosisi = str(v).strip()

        # Notice field names: appear at the rightmost populated columns in data rows
        # (col151-164 in header are template labels; actual values appear at end of row)
        # Strategy: look at last 16 cols for non-null string values
        notice_fields = []
        start_ci = max(151, ncols - 16)
        for ci in range(start_ci, ncols):
            v = row.iloc[ci]
            if pd.notna(v) and str(v) != 'nan':
                s = str(v).strip()
                if s:
                    notice_fields.append(s)

        result[cat_id] = {
            'gosisi_cat': gosisi,
            'valid_options': opt_types,
            'required_options': req_types,
            'notice_fields': notice_fields,
            'source': fname
        }

print(f'Extracted {len(result)} categories from guide files')

# ── Enrich gosisi_cat from category_map.json ──────────────────────────────
# category_map.json has structure: {keywords: {keyword: {category_id, gosisi_cat, ...}}}
with open(CAT_MAP_PATH, encoding='utf-8') as f:
    cat_map = json.load(f)

# Build cat_id → gosisi_cat lookup from keywords section
cat_id_to_gosisi: dict[str, str] = {}
for kw, info in cat_map.get('keywords', {}).items():
    if isinstance(info, dict):
        cid = info.get('category_id', '')
        gcat = info.get('gosisi_cat', '')
        if cid and gcat and cid not in cat_id_to_gosisi:
            cat_id_to_gosisi[cid] = gcat

enriched = 0
for cat_id, gcat in cat_id_to_gosisi.items():
    if cat_id in result:
        result[cat_id]['gosisi_cat'] = gcat
        enriched += 1

print(f'Enriched gosisi_cat for {enriched} categories from category_map.json ({len(cat_id_to_gosisi)} mappings found)')

# ── Fallback: infer gosisi_cat from source file name ──────────────────────
FILE_GOSISI = {
    '식품.xlsx':         '가공식품',
    '뷰티.xlsx':         '화장품',
    '자동차용품.xlsx':   '자동차용품',
    '가전디지털.xlsx':   '가정용 전기제품(냉장고/세탁기 등)',
    '생활용품.xlsx':     '기타 재화',
    '주방용품.xlsx':     '기타 재화',
    '패션의류잡화.xlsx': '기타 재화',
    '스포츠레져.xlsx':   '기타 재화',
    '완구취미.xlsx':     '기타 재화',
    '문구오피스.xlsx':   '기타 재화',
    '반려애완용품.xlsx': '기타 재화',
    '가구홈데코.xlsx':   '기타 재화',
    '출산유아동.xlsx':   '기타 재화',
    '음반DVD.xlsx':      '기타 재화',
    '기프트카드.xlsx':   '기타 재화',
    '도서.xlsx':         '기타 재화',
}

fallback_cnt = 0
for cat_id, entry in result.items():
    if not entry['gosisi_cat']:
        src = entry.get('source', '')
        fallback = FILE_GOSISI.get(src, '')
        if fallback:
            entry['gosisi_cat'] = fallback
            fallback_cnt += 1

print(f'Fallback gosisi_cat for {fallback_cnt} categories from file name')

# ── Save ──────────────────────────────────────────────────────────────────
with open(OUT_PATH, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f'Saved → {OUT_PATH}')
