"""
Module 2 – 이미지 자동 가공

파이프라인:
  원본 이미지
    → rembg 배경 제거 (누끼)  ← 실패 시 원본 그대로 fallback
    → 1개 / 2개 / 3개 합성 캔버스 생성
    → 수량 텍스트 각인
    → JPEG 저장  (data/images/composed/)
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from config.settings import Settings


# ── 배지 전용 폰트 후보 (Linux 서버 우선 — NanumSquareRound: 둥근 배지와
#    어울리는 모던하고 전문적인 인상, 기존 NanumGothic보다 덜 투박함) ──────
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/nanum/NanumSquareRoundB.ttf",  # 나눔스퀘어라운드 Bold (서버)
    "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "C:/Windows/Fonts/malgunbd.ttf",    # 맑은 고딕 Bold (Windows 로컬 개발용)
    "C:/Windows/Fonts/gulim.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/nanum/NanumSquareRoundR.ttf",  # 나눔스퀘어라운드 Regular (서버)
    "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",      # 맑은 고딕 Regular (Windows 로컬 개발용)
    "C:/Windows/Fonts/gulim.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
# 기존 코드 호환용 (self.font — 현재 미사용이나 초기화 유지)
_FONT_CANDIDATES = _FONT_CANDIDATES_REGULAR


class ImageProcessor:
    """누끼 추출 → 묶음 합성 → 수량 텍스트 각인."""

    def __init__(self, settings: Settings, store: str = "샵케이"):
        self.settings = settings
        self.canvas_size = (settings.CANVAS_WIDTH, settings.CANVAS_HEIGHT)
        self.font = self._load_font(settings.FONT_SIZE)
        self.store = store  # "샵케이" | "제니스 트레이딩"

    # ── Public ────────────────────────────────────────────────────

    def process(
        self,
        image_path: str,
        product_id: str,
        quantities: list[int] | None = None,
        skip_nobg: bool = False,
        unit_label: str = "개",
    ) -> dict[int, str]:
        """
        원본 이미지를 받아 묶음별 합성 이미지를 생성하고 경로를 반환.

        Args:
            skip_nobg: True 이면 배경 제거(누끼) 없이 원본 이미지로 합성.
            unit_label: 수량 배지 단위 텍스트 ("개" / "세트" / "박스" / "묶음").

        Returns:
            {1: "path/1ea.jpg", 2: "path/2ea.jpg", 3: "path/3ea.jpg"}
            실패 시 빈 dict 반환 (파이프라인을 죽이지 않음)
        """
        if quantities is None:
            quantities = [1, 2, 3]

        print(f"[ImageProcessor] 처리 시작: {image_path}")

        # 파일 존재 여부 먼저 확인
        if not os.path.isfile(image_path):
            print(f"[ImageProcessor] 파일 없음: {image_path}")
            return {}

        if skip_nobg:
            # 누끼 OFF: 원본 이미지를 RGBA로 변환해 배경 제거 없이 합성
            try:
                nobg = Image.open(image_path).convert("RGBA")
                print(f"[ImageProcessor] 누끼 스킵 — 원본 이미지 사용")
            except Exception as exc:
                print(f"[ImageProcessor] 원본 이미지 열기 실패: {exc}")
                return {}
        else:
            nobg = self._remove_background(image_path, product_id)
            if nobg is None:
                print("[ImageProcessor] 배경 제거 불가 – 이미지 가공 건너뜀")
                return {}

        # 1~1개 단일상품이면 배지(수량 숫자) 없이 저장
        _single_unit = (len(quantities) == 1 and quantities[0] == 1)

        result: dict[int, str] = {}
        for qty in quantities:
            try:
                composed = self._compose(nobg, qty, skip_crop=skip_nobg)
                if _single_unit:
                    # 1~1개 단일상품: 원형 배지 숫자 없이 깔끔한 이미지
                    labeled = composed
                    print(f"[ImageProcessor] 단일상품(1~1) — 배지 스킵")
                else:
                    labeled = self._stamp_label(composed, qty, unit_label)
                path     = self._save(labeled, product_id, qty)
                result[qty] = path
                print(f"[ImageProcessor] {qty}개 이미지 저장 완료: {path}")
            except Exception as exc:
                print(f"[ImageProcessor] {qty}개 이미지 생성 오류: {exc}")

        return result

    # ── Step 1: 배경 제거 ─────────────────────────────────────────

    def _remove_background(
        self, image_path: str, product_id: str
    ) -> Optional[Image.Image]:
        """
        배경 제거 2단계 폴백:
        1순위) rembg AI — 색상 배경 포함 모든 배경 처리
        2순위) 엣지 플러드필 — 흰/회색 단색 배경 전용 (rembg 실패 시)
        최종 폴백) 원본 RGBA 그대로 반환
        """
        nobg_dir = self.settings.IMAGE_NOBG_DIR
        os.makedirs(nobg_dir, exist_ok=True)
        nobg_path = os.path.join(nobg_dir, f"{product_id}_nobg.png")

        # ── 1순위: rembg AI 배경 제거 ────────────────────────────
        try:
            from rembg import remove as rembg_remove
            with open(image_path, "rb") as f:
                raw = f.read()
            out_bytes = rembg_remove(raw)
            nobg = Image.open(io.BytesIO(out_bytes)).convert("RGBA")
            nobg.save(nobg_path, "PNG")
            print(f"[ImageProcessor] 누끼 저장 (rembg AI): {nobg_path}")
            return nobg
        except Exception as exc:
            print(f"[ImageProcessor] rembg 실패, 플러드필로 전환: {exc}")

        # ── 2순위: 엣지 플러드필 (흰/회색 배경 전용) ─────────────
        try:
            img = Image.open(image_path).convert("RGBA")
            nobg = self._flood_fill_background(img)
            nobg.save(nobg_path, "PNG")
            print(f"[ImageProcessor] 누끼 저장 (플러드필): {nobg_path}")
            return nobg
        except Exception as exc2:
            print(f"[ImageProcessor] 플러드필 실패: {exc2}")

        # ── 최종 fallback: 원본 그대로 ───────────────────────────
        try:
            img = Image.open(image_path).convert("RGBA")
            print("[ImageProcessor] 원본 이미지 RGBA 변환 완료 (fallback)")
            return img
        except Exception as exc3:
            print(f"[ImageProcessor] 원본 이미지 열기 실패: {exc3}")
            return None

    @staticmethod
    def _flood_fill_background(
        img: Image.Image,
        threshold: int = 235,
        feather: int = 2,
    ) -> Image.Image:
        """
        이미지 4면 테두리에서 BFS로 연결된 밝은 픽셀(배경)만 투명 처리.
        테두리에 닿지 않는 투명 영역(제품 내부 흰색)은 자동 복원.

        Args:
            threshold : R,G,B 모두 이 값 이상이면 '밝은 픽셀'로 판단 (0~255)
            feather   : 배경·객체 경계를 부드럽게 블렌딩할 픽셀 범위
        """
        import numpy as np
        from collections import deque

        arr = np.array(img)          # shape: (H, W, 4) RGBA
        h, w = arr.shape[:2]

        # ── 배경 픽셀 마스크 ─────────────────────────────────────────
        # 조건 A: 순백/밝은 흰색 (RGB 모두 threshold 이상)
        # 조건 B: 저채도 회색 (max-min <= 30) + 중간 밝기 (평균 >= 170)
        #         → 연회색/은회색 그라디언트 배경도 제거 가능
        r = arr[:, :, 0].astype(np.int32)
        g = arr[:, :, 1].astype(np.int32)
        b = arr[:, :, 2].astype(np.int32)
        bright_white = np.all(arr[:, :, :3] >= threshold, axis=2)
        sat_range    = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
        mean_bright  = (r + g + b) // 3
        gray_bg      = (sat_range <= 30) & (mean_bright >= 170)
        bright       = bright_white | gray_bg          # (H, W) bool

        # ── 1단계 BFS: 테두리에서 연결된 밝은 픽셀 = 순수 배경 ──────
        bg_mask = np.zeros((h, w), dtype=bool)
        queue: deque = deque()

        def _seed(y: int, x: int) -> None:
            if bright[y, x] and not bg_mask[y, x]:
                bg_mask[y, x] = True
                queue.append((y, x))

        for x in range(w):
            _seed(0, x); _seed(h - 1, x)
        for y in range(h):
            _seed(y, 0); _seed(y, w - 1)

        while queue:
            y, x = queue.popleft()
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and not bg_mask[ny, nx] and bright[ny, nx]:
                    bg_mask[ny, nx] = True
                    queue.append((ny, nx))

        # ── 2단계: 배경 픽셀만 투명 처리 ─────────────────────────
        arr[bg_mask, 3] = 0

        # ── 3단계: 제품 내부 고립 투명 영역 복원 ─────────────────
        # 배경 제거 후 투명이 된 픽셀 중, 테두리와 연결되지 않은 영역
        # (= 제품 패키지 내부 흰색)을 원본 픽셀로 복원
        transparent = (arr[:, :, 3] == 0)
        border_touch = np.zeros((h, w), dtype=bool)
        restore_queue: deque = deque()

        # 투명 픽셀 중 테두리에 닿은 것만 시작점 (= 진짜 배경)
        for x in range(w):
            for y_edge in (0, h - 1):
                if transparent[y_edge, x] and not border_touch[y_edge, x]:
                    border_touch[y_edge, x] = True
                    restore_queue.append((y_edge, x))
        for y in range(h):
            for x_edge in (0, w - 1):
                if transparent[y, x_edge] and not border_touch[y, x_edge]:
                    border_touch[y, x_edge] = True
                    restore_queue.append((y, x_edge))

        while restore_queue:
            y, x = restore_queue.popleft()
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and not border_touch[ny, nx] and transparent[ny, nx]:
                    border_touch[ny, nx] = True
                    restore_queue.append((ny, nx))

        # 투명이지만 테두리와 연결 안 된 픽셀 = 제품 내부 → 원본으로 복원
        isolated = transparent & ~border_touch
        orig = np.array(img)
        arr[isolated] = orig[isolated]

        # ── 4단계: 경계 페더링 ───────────────────────────────────
        if feather > 0:
            try:
                from scipy.ndimage import binary_dilation  # type: ignore
                for _ in range(feather):
                    border = binary_dilation(bg_mask) & ~bg_mask & ~transparent
                    arr[border, 3] = np.clip(
                        arr[border, 3].astype(int) - 80, 0, 255
                    ).astype(np.uint8)
            except ImportError:
                pass  # scipy 없으면 페더링 생략

        return Image.fromarray(arr, "RGBA")

    # ── Step 2: 누끼 이미지 중앙 배치 (수량 무관 1개 고정) ─────────

    def _compose(self, nobg: Image.Image, qty: int, skip_crop: bool = False) -> Image.Image:
        """
        흰색 캔버스 중앙에 누끼 이미지를 1개 배치.
        수량 표시는 _stamp_label()의 원형 배지로 처리.

        skip_crop=True: 커스텀 이미지(원본 유지) — 배경 크롭 없이 원본 그대로 캔버스에 맞춤
        """
        W, H = self.canvas_size
        canvas = Image.new("RGBA", (W, H), (255, 255, 255, 255))

        if skip_crop:
            # 원본 이미지 그대로 정사각형 패딩 → 캔버스 92%
            obj = self._pad_to_square(nobg)
            obj = self._resize_obj(obj, int(W * 0.92), int(H * 0.92))
        else:
            # 누끼 이미지: 투명 여백 크롭 → 정사각형 패딩 → 캔버스 92%
            obj = self._crop_to_content(nobg)
            obj = self._pad_to_square(obj)
            obj = self._resize_obj(obj, int(W * 0.92), int(H * 0.92))

        x = (W - obj.width)  // 2
        y = (H - obj.height) // 2
        canvas.paste(obj, (x, y), obj)

        return canvas.convert("RGB")

    # ── Step 3: 좌측 하단 원형 수량 배지 ────────────────────────

    def _stamp_label(self, image: Image.Image, qty: int, unit: str = "개") -> Image.Image:
        """
        좌측 하단 원형 배지 + 수량 텍스트.

        스타일:
          샵케이        : 흰색 채움 + 검정 테두리 + 검정 글씨 (원형 — "개"일 때만)
          제니스 트레이딩: 흰색 채움 + 검정 테두리 + 검정 글씨 (모서리 둥근 사각형, 항상 —
                          포트와 동일하게 통일. 예전엔 "개"일 때만 검정 채움 원형이었으나
                          사용자 요청으로 검정 원형 폐지, 전 단위 포트 스타일로 고정).
          포트          : 흰색 채움 + 검정 테두리 + 검정 글씨 (모서리 둥근 사각형, 항상)

          단위가 "개"가 아닐 때(세트/박스/묶음)는 글자수가 많아 원형에 넣으면 잘 안 보여서
          샵케이도 포트와 동일한 흰색+검정테두리 모서리 둥근 사각형으로 전환.

        크기 기준 (800×800 캔버스):
          원 지름  ≈ 152 px  (캔버스 단변의 19%)
          1~9개   : 폰트 약 68 px
          10~15개 : 폰트 약 50 px

        unit: 수량 단위 텍스트 ("개" / "세트" / "박스" / "묶음") — 글자수가 길수록 폰트 축소.
        """
        draw  = ImageDraw.Draw(image)
        W, H  = image.size
        text  = f"{qty}{unit}"

        # ── 배지 크기·위치 ──────────────────────────────────────
        d      = int(min(W, H) * 0.194)  # 0.215 → 0.194 (10% 축소)
        margin = int(min(W, H) * 0.028)

        # ── 스토어별 배지 스타일 ─────────────────────────────────
        _store     = getattr(self, "store", "샵케이")
        _is_zenith = (_store == "제니스 트레이딩")
        _is_port   = (_store == "포트")
        _long_unit = (unit != "개")           # 세트/박스/묶음 — 원형엔 좁음
        _use_rect  = _is_port or _is_zenith or _long_unit   # 모서리 둥근 사각형 사용 여부

        # 사각형일 때 가로폭 확장 (장문 단위는 더 넉넉하게)
        if _use_rect:
            rw = int(d * (1.35 if _long_unit else 1.2))
        else:
            rw = d
        cx = margin + rw // 2
        cy = H - margin - d // 2

        if _use_rect:
            # 흰색 채움 + 검정 테두리, 모서리 둥근 사각형
            # (포트·제니스는 항상 / 샵케이는 장문단위(세트·박스·묶음)일 때만)
            border_w = max(2, int(d * 0.026))
            draw.rounded_rectangle(
                [cx - rw // 2, cy - d // 2,
                 cx + rw // 2, cy + d // 2],
                radius=int(d * 0.28),
                fill=(255, 255, 255),
                outline=(0, 0, 0),
                width=border_w,
            )
            text_color   = (0, 0, 0)
            stroke_fill  = (0, 0, 0)
            stroke_width = 2
        else:
            # 흰색 채움 + 검정 테두리 (기존 샵케이 원형 스타일)
            border_w = max(2, int(d * 0.026))
            draw.ellipse(
                [cx - d // 2, cy - d // 2,
                 cx + d // 2, cy + d // 2],
                fill=(255, 255, 255),
                outline=(0, 0, 0),
                width=border_w,
            )
            text_color   = (0, 0, 0)
            stroke_fill  = (0, 0, 0)
            stroke_width = 2

        # ── 폰트 크기 결정 — 숫자는 크게(Bold) / 단위는 작게(Regular) ──────
        # 숫자·단위를 분리 렌더링해 "3" 크게 + "개" 작게 같은 전문적인 태그 느낌을 낸다.
        qty_str, unit_str = str(qty), unit
        num_font_size  = int(d * (0.50 if len(qty_str) < 2 else 0.40))
        unit_font_size = int(d * (0.30 if len(unit_str) < 2 else 0.24))
        num_font  = self._load_font(num_font_size, bold=True)
        unit_font = self._load_font(unit_font_size, bold=False)

        num_bbox  = draw.textbbox((0, 0), qty_str, font=num_font)
        unit_bbox = draw.textbbox((0, 0), unit_str, font=unit_font)
        num_w   = num_bbox[2] - num_bbox[0]
        unit_w  = unit_bbox[2] - unit_bbox[0]
        gap     = max(1, int(d * 0.03))
        total_w = num_w + gap + unit_w

        # ── 숫자+단위 조합을 배지 중앙에 정렬, 하단(baseline) 맞춤 ────────
        start_x = cx - total_w // 2
        num_x   = start_x - num_bbox[0]
        num_y   = cy - (num_bbox[3] - num_bbox[1]) // 2 - num_bbox[1]
        num_bottom = num_y + num_bbox[3]
        unit_x  = start_x + num_w + gap - unit_bbox[0]
        unit_y  = num_bottom - unit_bbox[3]

        draw.text((num_x, num_y), qty_str, fill=text_color, font=num_font,
                  stroke_width=3, stroke_fill=stroke_fill)
        draw.text((unit_x, unit_y), unit_str, fill=text_color, font=unit_font,
                  stroke_width=2, stroke_fill=stroke_fill)

        return image

    # ── 상세페이지 전용 클린 이미지 생성 (배지 없음) ────────────────

    def process_detail(
        self,
        image_path: str,
        product_id: str,
        skip_nobg: bool = False,
    ) -> str:
        """
        상세페이지용 배지 없는 클린 이미지 생성.
        _compose() 까지만 실행하고 _stamp_label() 은 절대 호출하지 않음.

        Returns:
            저장된 이미지 경로. 실패 시 빈 문자열.
        """
        if not os.path.isfile(image_path):
            return ""

        if skip_nobg:
            try:
                img = Image.open(image_path).convert("RGBA")
            except Exception as exc:
                print(f"[ImageProcessor] 상세이미지 원본 열기 실패: {exc}")
                return ""
        else:
            img = self._remove_background(image_path, product_id)
            if img is None:
                # 누끼 실패 → 원본 이미지로 대체
                try:
                    img = Image.open(image_path).convert("RGBA")
                except Exception:
                    return ""

        composed = self._compose(img, qty=0, skip_crop=skip_nobg)
        path = self._save_detail(composed, product_id)
        print(f"[ImageProcessor] 상세이미지(배지없음) 저장 완료: {path}")
        return path

    # ── 저장 ─────────────────────────────────────────────────────

    def _save(self, image: Image.Image, product_id: str, qty: int) -> str:
        composed_dir = self.settings.IMAGE_COMPOSED_DIR
        os.makedirs(composed_dir, exist_ok=True)
        path = os.path.join(composed_dir, f"{product_id}_{qty}ea.jpg")
        image.save(path, "JPEG", quality=95, subsampling=0)
        return path

    def _save_detail(self, image: Image.Image, product_id: str) -> str:
        """상세페이지 전용 저장 (배지 없음, _detail.jpg)."""
        composed_dir = self.settings.IMAGE_COMPOSED_DIR
        os.makedirs(composed_dir, exist_ok=True)
        path = os.path.join(composed_dir, f"{product_id}_detail.jpg")
        image.save(path, "JPEG", quality=95, subsampling=0)
        return path

    # ── 헬퍼 ─────────────────────────────────────────────────────

    @staticmethod
    def _crop_to_content(img: Image.Image) -> Image.Image:
        """
        투명 여백 또는 배경 여백을 잘라내고 실제 상품 영역만 반환.
        1순위: RGBA 알파 채널 기준 크롭 (누끼 적용 이미지)
        2순위: 코너 배경색 자동 감지 → adaptive 크롭 (회색/유색 배경 포함)
        """
        import numpy as np

        ow, oh = img.size

        # 1순위: 알파 채널 기준 (누끼 후 투명 여백)
        if img.mode == "RGBA":
            try:
                alpha = img.split()[3]
                bbox = alpha.getbbox()
                if bbox:
                    cropped = img.crop(bbox)
                    cw, ch = cropped.size
                    if cw < ow * 0.95 or ch < oh * 0.95:
                        return cropped
            except Exception:
                pass

        # 2순위: 코너 배경색 자동 감지 → 배경 제거 후 크롭
        # 흰색/회색/유색 단색 배경 모두 처리 가능
        try:
            rgb = img.convert("RGB")
            arr = np.array(rgb, dtype=np.int32)
            h, w = arr.shape[:2]
            s = max(5, min(20, h // 20, w // 20))
            # 4코너 픽셀 평균 → 배경색 추정
            corners = np.concatenate([
                arr[:s, :s].reshape(-1, 3),
                arr[:s, -s:].reshape(-1, 3),
                arr[-s:, :s].reshape(-1, 3),
                arr[-s:, -s:].reshape(-1, 3),
            ])
            bg = np.median(corners, axis=0)  # mean 대신 median — 그라디언트 아웃라이어 강건
            tol = 60  # 배경색 ±60 허용 (회색 그라디언트 배경까지 제거)
            diff = np.max(np.abs(arr - bg), axis=2)
            mask = diff > tol  # True = 상품 픽셀
            rows = np.any(mask, axis=1)
            cols = np.any(mask, axis=0)
            if rows.any() and cols.any():
                r0, r1 = np.where(rows)[0][[0, -1]]
                c0, c1 = np.where(cols)[0][[0, -1]]
                pad = 8
                r0 = max(0, r0 - pad); r1 = min(h - 1, r1 + pad)
                c0 = max(0, c0 - pad); c1 = min(w - 1, c1 + pad)
                cropped2 = img.crop((c0, r0, c1 + 1, r1 + 1))
                cw2, ch2 = cropped2.size
                if cw2 < ow * 0.95 or ch2 < oh * 0.95:
                    return cropped2
        except Exception:
            pass

        return img

    @staticmethod
    def _pad_to_square(img: Image.Image) -> Image.Image:
        """짧은 축에 흰 여백을 추가해 정사각형으로 만든다. 항상 캔버스 92% 꽉 채우기 위한 전처리."""
        w, h = img.size
        if w == h:
            return img
        side = max(w, h)
        # RGBA 이미지는 투명 패딩, RGB는 흰색 패딩
        if img.mode == "RGBA":
            bg = Image.new("RGBA", (side, side), (255, 255, 255, 0))
        else:
            bg = Image.new("RGB", (side, side), (255, 255, 255))
        x = (side - w) // 2
        y = (side - h) // 2
        if img.mode == "RGBA":
            bg.paste(img, (x, y), img)
        else:
            bg.paste(img, (x, y))
        return bg

    @staticmethod
    def _resize_obj(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
        """비율 유지하면서 max_w×max_h 에 꽉 차도록 확대/축소."""
        iw, ih = img.size
        if iw == 0 or ih == 0:
            return img
        scale = min(max_w / iw, max_h / ih)
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))
        return img.resize((new_w, new_h), Image.LANCZOS)

    @staticmethod
    def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
        candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
        for path in candidates:
            if os.path.isfile(path):
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    continue
        print("[ImageProcessor] 한글 폰트를 찾지 못해 기본 폰트를 사용합니다.")
        return ImageFont.load_default()
