"""
image_splitter.py — 세로로 긴 이미지를 자동 분할

원리:
  1. 각 수평 행(row)의 픽셀 평균 밝기를 numpy로 계산
  2. 연속적으로 배경(밝기 > bg_threshold)인 행 구간 = '여백 갭'
  3. 갭 중앙을 절단선으로 삼아 조각 생성
  4. 너무 작은 조각(min_height 미만)은 인접 조각과 병합

공개 API:
    is_long_image(img)         → bool   (height/width > 2.5)
    split_long_image(path)     → list[PIL.Image]  (최대 max_splits 조각)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image

# 긴 이미지 판정 기준
_LONG_RATIO = 2.5   # height / width

# 분할 기본값
_BG_THRESHOLD  = 238   # 이 밝기(0-255) 이상인 행 = 배경 행
_MIN_GAP_PX    = 8     # 유효 갭 최소 높이 (px)
_MIN_PIECE_PX  = 80    # 조각 최소 높이 — 이보다 작으면 병합
_MAX_SPLITS    = 20    # 최대 분할 수 (긴 이미지 전체 조각 활용)


def is_long_image(img: Image.Image) -> bool:
    """이미지가 세로로 매우 긴지 판정."""
    w, h = img.size
    return w > 0 and (h / w) >= _LONG_RATIO


def split_long_image(
    image_path:     str,
    bg_threshold:   int = _BG_THRESHOLD,
    min_gap_px:     int = _MIN_GAP_PX,
    min_piece_px:   int = _MIN_PIECE_PX,
    max_splits:     int = _MAX_SPLITS,
) -> list[Image.Image]:
    """
    세로로 긴 이미지를 배경 여백 구간 기준으로 자동 분할.

    Args:
        image_path:   로컬 이미지 경로
        bg_threshold: 배경으로 판단할 최소 밝기 (0~255, 기본 238)
        min_gap_px:   유효 갭 최소 높이 (기본 8px)
        min_piece_px: 이보다 작은 조각은 인접 조각에 병합 (기본 80px)
        max_splits:   최대 분할 수 (기본 10)

    Returns:
        분할된 PIL.Image 리스트. 분할 불필요하면 원본 1장 리스트.
    """
    try:
        import numpy as np
    except ImportError:
        print("[Splitter] numpy 없음 — 분할 불가, 원본 반환")
        return [Image.open(image_path).convert("RGB")]

    img  = Image.open(image_path).convert("RGB")
    W, H = img.size

    if not is_long_image(img):
        return [img]

    arr  = np.array(img)                         # (H, W, 3)
    mean_brightness = arr.mean(axis=(1, 2))      # 각 행의 평균 밝기 (H,)

    # 배경 행 마스크
    is_bg = mean_brightness >= bg_threshold      # (H,) bool

    # 연속 배경 구간(갭) 찾기
    gaps: list[tuple[int, int]] = []
    in_gap   = False
    gap_start = 0
    for y in range(H):
        if is_bg[y] and not in_gap:
            in_gap    = True
            gap_start = y
        elif not is_bg[y] and in_gap:
            in_gap = False
            gap_len = y - gap_start
            if gap_len >= min_gap_px:
                gaps.append((gap_start, y))
    if in_gap and (H - gap_start) >= min_gap_px:
        gaps.append((gap_start, H))

    if not gaps:
        print(f"[Splitter] 유효 갭 없음 → 원본 반환 ({W}×{H})")
        return [img]

    # 갭 중앙을 절단선으로 변환, 상한 max_splits-1개
    cut_lines: list[int] = []
    for gs, ge in gaps[: max_splits - 1]:
        mid = (gs + ge) // 2
        cut_lines.append(mid)

    cut_lines = sorted(set(cut_lines))

    # 조각 경계 생성 [0, cut1, cut2, ..., H]
    boundaries = [0] + cut_lines + [H]
    pieces_raw: list[tuple[int, int]] = [
        (boundaries[i], boundaries[i + 1])
        for i in range(len(boundaries) - 1)
        if boundaries[i + 1] - boundaries[i] > 0
    ]

    # 너무 작은 조각 병합
    pieces = _merge_small_pieces(pieces_raw, min_piece_px)

    # PIL 크롭
    result: list[Image.Image] = []
    for top, bot in pieces:
        piece = img.crop((0, top, W, bot))
        result.append(piece)

    print(f"[Splitter] {W}×{H} → {len(result)}조각 (갭 {len(gaps)}개 감지)")
    return result


def _merge_small_pieces(
    pieces: list[tuple[int, int]],
    min_h:  int,
) -> list[tuple[int, int]]:
    """min_h 미만 조각을 인접 조각과 병합."""
    if not pieces:
        return pieces

    merged = list(pieces)
    changed = True
    while changed:
        changed = False
        new = []
        i = 0
        while i < len(merged):
            top, bot = merged[i]
            h = bot - top
            if h < min_h and len(merged) > 1:
                # 이전 조각과 병합 (없으면 다음과 병합)
                if new:
                    prev_top, _ = new[-1]
                    new[-1] = (prev_top, bot)
                elif i + 1 < len(merged):
                    next_top, next_bot = merged[i + 1]
                    new.append((top, next_bot))
                    i += 2
                    changed = True
                    continue
                else:
                    new.append((top, bot))
                changed = True
            else:
                new.append((top, bot))
            i += 1
        merged = new

    return merged
