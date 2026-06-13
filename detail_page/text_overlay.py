"""
text_overlay.py — PIL로 한글 텍스트 오버레이 (오타 0% 보장)

수정 이력:
  v2 (2026-06-11):
    - 좌우·상하 안전 여백(MARGIN_PCT) 강제
    - textwrap 기반 자동 줄바꿈: (캔버스폭 - 좌우여백)에 맞춤
    - 줄바꿈 후에도 넘치면 폰트 크기 자동 축소 (최소 10px 보장)
    - 텍스트가 절대 캔버스 밖으로 나가지 않음
"""
from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/gulim.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# 안전 여백: 캔버스 폭의 이 비율만큼 좌우에 강제
_MARGIN_PCT  = 0.07   # 7%
_MIN_FONT_PX = 10     # 자동 축소 하한


@dataclass
class OverlayConfig:
    """섹션별 텍스트 오버레이 설정."""
    title:    str = ""
    subtitle: str = ""
    body:     str = ""
    layout:   str = "full"


def apply_overlay(
    img: Image.Image,
    config: OverlayConfig,
    section_type: str = "",
) -> Image.Image:
    result = img.copy().convert("RGBA")
    W, H   = result.size
    draw   = ImageDraw.Draw(result)

    if section_type in ("hero", "closing"):
        _overlay_hero(result, draw, config, W, H)
    elif section_type in ("features", "usage"):
        _overlay_features(result, draw, config, W, H)
    elif section_type == "specs":
        _overlay_specs(result, draw, config, W, H)
    elif section_type == "callout":
        _overlay_callout(result, draw, config, W, H)
    else:
        _overlay_hero(result, draw, config, W, H)

    return result.convert("RGB")


# ── 레이아웃별 오버레이 ───────────────────────────────────────────────

def _overlay_hero(result, draw, cfg: OverlayConfig, W: int, H: int):
    if not cfg.title and not cfg.subtitle:
        return

    bar_h    = int(H * 0.22)
    bar      = Image.new("RGBA", (W, bar_h), (0, 0, 0, 0))
    bar_draw = ImageDraw.Draw(bar)
    for i in range(bar_h):
        alpha = int(180 * (i / bar_h))
        bar_draw.rectangle([0, i, W, i + 1], fill=(0, 0, 0, alpha))
    result.alpha_composite(bar, (0, H - bar_h))

    draw   = ImageDraw.Draw(result)
    margin = int(W * _MARGIN_PCT)
    max_w  = W - margin * 2
    y_base = H - bar_h + int(bar_h * 0.12)
    y_bot  = H - int(H * 0.02)   # 캔버스 하단 경계

    if cfg.title:
        size   = int(H * 0.055)
        font_t, lines = _fit_text(cfg.title, size, max_w)
        line_h = _line_height(font_t)
        needed = len(lines) * line_h
        if y_base + needed > y_bot:
            y_base = max(H - bar_h + 4, y_bot - needed - int(bar_h * 0.1))
        for line in lines:
            _draw_centered_line(draw, line, W, y_base, font_t, (255, 255, 255, 255))
            y_base += line_h
        y_base += int(H * 0.008)

    if cfg.subtitle and y_base < y_bot:
        size   = int(H * 0.030)
        font_s, lines = _fit_text(cfg.subtitle, size, max_w)
        line_h = _line_height(font_s)
        for line in lines:
            if y_base + line_h > y_bot:
                break
            _draw_centered_line(draw, line, W, y_base, font_s, (220, 220, 220, 255))
            y_base += line_h


def _overlay_features(result, draw, cfg: OverlayConfig, W: int, H: int):
    margin = int(W * _MARGIN_PCT)
    max_w  = W - margin * 2

    if cfg.title:
        bar_h  = int(H * 0.13)
        bar    = Image.new("RGBA", (W, bar_h), (255, 255, 255, 210))
        result.alpha_composite(bar, (0, 0))
        draw   = ImageDraw.Draw(result)
        size   = int(H * 0.045)
        font_t, lines = _fit_text(cfg.title, size, max_w)
        line_h = _line_height(font_t)
        y      = int(bar_h * 0.18)
        for line in lines:
            if y + line_h > bar_h - 4:
                break
            _draw_centered_line(draw, line, W, y, font_t, (30, 30, 30, 255))
            y += line_h

    if cfg.body:
        _draw_body_bottom(result, cfg.body, W, H, max_w)


def _overlay_specs(result, draw, cfg: OverlayConfig, W: int, H: int):
    if not cfg.body:
        return
    margin    = int(W * _MARGIN_PCT)
    max_w     = W - margin * 2
    _BOT_MARGIN = int(H * 0.025)   # 하단 안전 여백

    # ── wrap 먼저 → 실제 라인 수 기준으로 panel_h 계산 ──────────
    size   = int(H * 0.027)
    font_b = _load_font(size)
    line_h = _line_height(font_b)

    raw_lines = [l for l in cfg.body.split("\n")]
    all_subs: list[tuple[str, bool]] = []   # (text, is_blank)
    for raw in raw_lines:
        if not raw.strip():
            all_subs.append(("", True))
        else:
            _, wrapped = _fit_text(raw, size, max_w, font_b)
            for w in wrapped:
                all_subs.append((w, False))

    # panel 높이 = 실제 라인 수 기준 + 상하 패딩
    pad     = int(H * 0.05)
    panel_h = pad + len(all_subs) * line_h + pad
    panel_h = max(panel_h, int(H * 0.30))
    panel_h = min(panel_h, int(H * 0.55))

    panel = Image.new("RGBA", (W, panel_h), (255, 255, 255, 230))
    result.alpha_composite(panel, (0, H - panel_h))
    draw  = ImageDraw.Draw(result)

    y     = H - panel_h + pad
    y_bot = H - _BOT_MARGIN

    for text, is_blank in all_subs:
        if is_blank:
            y += int(line_h * 0.4)
            continue
        if y + line_h > y_bot:
            break
        draw.text((margin, y), text, font=font_b, fill=(40, 40, 40, 255))
        y += line_h


def _overlay_callout(result, draw, cfg: OverlayConfig, W: int, H: int):
    margin = int(W * _MARGIN_PCT)
    max_w  = W - margin * 2

    if cfg.title:
        size   = int(H * 0.075)
        font_t, lines = _fit_text(cfg.title, size, max_w)
        line_h = _line_height(font_t)
        total  = len(lines) * line_h
        y      = int(H * 0.40) - total // 2
        for line in lines:
            _draw_centered_line(draw, line, W, y, font_t,
                                (255, 255, 255, 255), stroke=True)
            y += line_h

    if cfg.subtitle:
        size   = int(H * 0.035)
        font_s, lines = _fit_text(cfg.subtitle, size, max_w)
        line_h = _line_height(font_s)
        y      = int(H * 0.53)
        for line in lines:
            if y + line_h > int(H * 0.85):
                break
            _draw_centered_line(draw, line, W, y, font_s, (240, 240, 240, 255))
            y += line_h


def _draw_body_bottom(result, body: str, W: int, H: int, max_w: int):
    """
    하단 그라디언트 바 위에 본문 텍스트 (v2 — 하단 잘림 완전 방지).

    핵심 원칙:
      1. wrap 먼저 → 실제 서브라인 수 확정 후 bar_h 계산 (역순 아님)
      2. bar_h 가 _MAX_BAR_PCT 초과 시 폰트 자동 축소
      3. 하단 안전 여백(_BOT_MARGIN_PCT) 강제 → 캔버스 밖 절대 불가
    """
    _BOT_MARGIN_PCT = 0.025   # 하단 안전 여백 2.5%
    _PAD_TOP_PCT    = 0.018
    _PAD_BOT_PCT    = 0.018
    _MAX_BAR_PCT    = 0.38    # bar 최대 높이 38%
    _MAX_LINES      = 10

    raw_lines = [l for l in body.split("\n") if l.strip()][-6:]
    if not raw_lines:
        return

    # ── Step 1: wrap 완료된 서브라인 목록 먼저 확정 ──────────────
    def _collect_subs(sz: int):
        fnt  = _load_font(sz)
        subs = []
        for raw in raw_lines:
            _, wrapped = _fit_text(raw, sz, max_w, fnt)
            subs.extend(wrapped)
        return fnt, subs[:_MAX_LINES]

    size             = int(H * 0.028)
    font_b, all_subs = _collect_subs(size)
    line_h           = _line_height(font_b)

    # ── Step 2: bar_h = 실제 서브라인 수 기준 ───────────────────
    pad_top = int(H * _PAD_TOP_PCT)
    pad_bot = int(H * _PAD_BOT_PCT)
    max_bar = int(H * _MAX_BAR_PCT)

    def _calc_bar_h():
        return pad_top + len(all_subs) * line_h + pad_bot

    # bar가 너무 크면 폰트 축소
    while _calc_bar_h() > max_bar and size > _MIN_FONT_PX:
        size -= 2
        font_b, all_subs = _collect_subs(size)
        line_h = _line_height(font_b)

    bar_h = min(_calc_bar_h(), max_bar)

    # ── Step 3: 그라디언트 bar 그리기 ───────────────────────────
    bar = Image.new("RGBA", (W, bar_h), (0, 0, 0, 0))
    bd  = ImageDraw.Draw(bar)
    for i in range(bar_h):
        alpha = int(170 * (i / bar_h))
        bd.rectangle([0, i, W, i + 1], fill=(0, 0, 0, alpha))
    result.alpha_composite(bar, (0, H - bar_h))
    draw = ImageDraw.Draw(result)

    # ── Step 4: 텍스트 — 하단 안전 여백 강제 ────────────────────
    y     = H - bar_h + pad_top
    y_bot = H - int(H * _BOT_MARGIN_PCT)

    for sub in all_subs:
        if y + line_h > y_bot:
            break
        _draw_centered_line(draw, sub, W, y, font_b, (230, 230, 230, 255))
        y += line_h


# ── 텍스트 피팅 헬퍼 ─────────────────────────────────────────────────

def _fit_text(
    text: str,
    size: int,
    max_w: int,
    font: Optional[ImageFont.ImageFont] = None,
) -> tuple[ImageFont.ImageFont, list[str]]:
    """
    주어진 폰트 크기로 텍스트를 max_w에 맞게 wrapping.
    wrapping 후에도 한 줄이 max_w를 초과하면 폰트 크기를 자동 축소.

    Returns:
        (사용된 font, 줄 목록)
    """
    size  = max(size, _MIN_FONT_PX)
    font  = font or _load_font(size)
    lines = _wrap_to_width(text, font, max_w)

    # 한 줄이라도 max_w 초과하면 폰트 축소 반복
    dummy_img  = Image.new("RGB", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)

    while size > _MIN_FONT_PX:
        overflow = False
        for line in lines:
            try:
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                tw   = bbox[2] - bbox[0]
            except Exception:
                tw = 0
            if tw > max_w:
                overflow = True
                break
        if not overflow:
            break
        size = max(size - 2, _MIN_FONT_PX)
        font  = _load_font(size)
        lines = _wrap_to_width(text, font, max_w)
        if size == _MIN_FONT_PX:
            break

    return font, lines


def _wrap_to_width(text: str, font: ImageFont.ImageFont, max_w: int) -> list[str]:
    """
    텍스트를 max_w 픽셀 폭에 맞게 줄바꿈.
    한글+영문 혼합 대응: 문자 단위 greedy wrapping.
    """
    dummy_img  = Image.new("RGB", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)

    def _text_w(s: str) -> int:
        try:
            bbox = dummy_draw.textbbox((0, 0), s, font=font)
            return bbox[2] - bbox[0]
        except Exception:
            return 0

    # 이미 짧으면 바로 반환
    if _text_w(text) <= max_w:
        return [text]

    # 어절 단위 우선 wrapping
    words  = text.split()
    lines: list[str] = []
    cur    = ""
    for word in words:
        test = (cur + " " + word).strip() if cur else word
        if _text_w(test) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            # 단어 하나가 max_w 초과 → 문자 단위 split
            if _text_w(word) > max_w:
                sub = ""
                for ch in word:
                    if _text_w(sub + ch) <= max_w:
                        sub += ch
                    else:
                        if sub:
                            lines.append(sub)
                        sub = ch
                cur = sub
            else:
                cur = word
    if cur:
        lines.append(cur)
    return lines if lines else [text]


def _line_height(font: ImageFont.ImageFont) -> int:
    dummy_img  = Image.new("RGB", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)
    try:
        bbox = dummy_draw.textbbox((0, 0), "가나다Ag", font=font)
        return int((bbox[3] - bbox[1]) * 1.35)
    except Exception:
        return 20


def _draw_centered_line(
    draw, text: str, W: int, y: int,
    font, color: tuple, stroke: bool = False,
):
    """텍스트 1줄을 수평 중앙에 그림."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw   = bbox[2] - bbox[0]
        tx   = (W - tw) // 2 - bbox[0]
        kw: dict = {"fill": color, "font": font}
        if stroke:
            kw["stroke_width"] = 3
            kw["stroke_fill"]  = (0, 0, 0, 200)
        draw.text((tx, y), text, **kw)
    except Exception as e:
        print(f"[TextOverlay] 텍스트 그리기 오류: {e}")


def _load_font(size: int) -> ImageFont.ImageFont:
    size = max(size, _MIN_FONT_PX)
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()
