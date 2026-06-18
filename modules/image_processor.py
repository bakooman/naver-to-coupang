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


# ── 폰트 후보 (Windows → Linux 순) ──────────────────────────────
_FONT_CANDIDATES = [
    "data/fonts/NotoSansKR-Light.otf",  # Noto Sans KR Light — 가장 얇음 (프로젝트 내장)
    "C:/Windows/Fonts/malgunsl.ttf",    # 맑은 고딕 Semilight (Windows)
    "C:/Windows/Fonts/malgun.ttf",      # 맑은 고딕 Regular   (Windows)
    "C:/Windows/Fonts/malgunbd.ttf",    # 맑은 고딕 Bold      (fallback)
    "C:/Windows/Fonts/gulim.ttc",       # 굴림                (Windows)
    "/usr/share/fonts/truetype/nanum/NanumGothicLight.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


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
    ) -> dict[int, str]:
        """
        원본 이미지를 받아 묶음별 합성 이미지를 생성하고 경로를 반환.

        Args:
            skip_nobg: True 이면 배경 제거(누끼) 없이 원본 이미지로 합성.

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
                composed = self._compose(nobg, qty)
                if _single_unit:
                    # 1~1개 단일상품: 원형 배지 숫자 없이 깔끔한 이미지
                    labeled = composed
                    print(f"[ImageProcessor] 단일상품(1~1) — 배지 스킵")
                else:
                    labeled = self._stamp_label(composed, qty)
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

    def _compose(self, nobg: Image.Image, qty: int) -> Image.Image:
        """
        흰색 캔버스 중앙에 누끼 이미지를 1개 배치.
        수량 표시는 _stamp_label()의 원형 배지로 처리.

        qty 값은 _stamp_label()에 전달되어 배지 텍스트로 사용됨.
        """
        W, H = self.canvas_size
        canvas = Image.new("RGBA", (W, H), (255, 255, 255, 255))

        # 투명 여백 제거 후 캔버스의 92% 크기로 중앙 배치
        obj = self._crop_to_content(nobg)
        obj = self._resize_obj(obj, int(W * 0.92), int(H * 0.92))
        x   = (W - obj.width)  // 2
        y   = (H - obj.height) // 2
        canvas.paste(obj, (x, y), obj)

        return canvas.convert("RGB")

    # ── Step 3: 좌측 하단 원형 수량 배지 ────────────────────────

    def _stamp_label(self, image: Image.Image, qty: int) -> Image.Image:
        """
        좌측 하단 원형 배지 + 수량 텍스트.

        스타일:
          샵케이        : 흰색 채움 + 검정 테두리 + 검정 글씨 (기존)
          제니스 트레이딩: 검정 채움 (테두리 없음) + 흰색 글씨

        크기 기준 (800×800 캔버스):
          원 지름  ≈ 152 px  (캔버스 단변의 19%)
          1~9개   : 폰트 약 68 px
          10~15개 : 폰트 약 50 px
        """
        draw  = ImageDraw.Draw(image)
        W, H  = image.size
        text  = f"{qty}개"

        # ── 원 크기·위치 ────────────────────────────────────────
        d      = int(min(W, H) * 0.190)
        margin = int(min(W, H) * 0.028)
        cx     = margin + d // 2
        cy     = H - margin - d // 2

        # ── 스토어별 배지 스타일 ─────────────────────────────────
        _is_zenith = (getattr(self, "store", "샵케이") == "제니스 트레이딩")

        if _is_zenith:
            # 검정 채움 원, 테두리 없음
            draw.ellipse(
                [cx - d // 2, cy - d // 2,
                 cx + d // 2, cy + d // 2],
                fill=(0, 0, 0),
            )
            text_color   = (255, 255, 255)
            stroke_fill  = (255, 255, 255)  # 흰 stroke → 흰 텍스트 두껍게
            stroke_width = 5
        else:
            # 흰색 채움 + 검정 테두리 (기존 샵케이 스타일)
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

        # ── 폰트 크기 결정 ───────────────────────────────────────
        font_size = int(d * 0.38) if qty < 10 else int(d * 0.28)
        font = self._load_font(font_size)

        # ── 텍스트 원 중앙 정렬 ──────────────────────────────────
        bbox = draw.textbbox((0, 0), text, font=font)
        tw   = bbox[2] - bbox[0]
        th   = bbox[3] - bbox[1]
        tx   = cx - tw // 2 - bbox[0]
        ty   = cy - th // 2 - bbox[1]

        # Light 폰트 보정: stroke로 중간 굵기 느낌
        stroke_width = 2 if _is_zenith else 2
        draw.text((tx, ty), text, fill=text_color, font=font,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)

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

        composed = self._compose(img, qty=0)   # qty=0: _compose 내부에서 배지 없이 캔버스만 생성
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
        투명 여백 또는 흰/밝은 배경 여백을 잘라내고 실제 상품 영역만 반환.
        1순위: RGBA 알파 채널 기준 크롭
        2순위: 흰/밝은 배경 여백(임계값 240) BBox 크롭
        """
        import numpy as np

        # 1순위: 알파 채널 기준
        if img.mode == "RGBA":
            try:
                alpha = img.split()[3]
                bbox = alpha.getbbox()
                if bbox:
                    cropped = img.crop(bbox)
                    cw, ch = cropped.size
                    ow, oh = img.size
                    # 의미 있는 크롭이면 (원본 대비 10% 이상 줄었으면) 반환
                    if cw < ow * 0.95 or ch < oh * 0.95:
                        return cropped
            except Exception:
                pass

        # 2순위: 흰/밝은 배경 여백 크롭 (RGB or RGBA 모두)
        try:
            rgb = img.convert("RGB")
            arr = np.array(rgb)
            # 밝기 임계값 240 이상 = 흰 여백으로 간주
            mask = np.any(arr < 240, axis=2)  # True = 상품 픽셀
            rows = np.any(mask, axis=1)
            cols = np.any(mask, axis=0)
            if rows.any() and cols.any():
                r0, r1 = np.where(rows)[0][[0, -1]]
                c0, c1 = np.where(cols)[0][[0, -1]]
                pad = 4  # 살짝 여유
                r0 = max(0, r0 - pad); r1 = min(arr.shape[0]-1, r1 + pad)
                c0 = max(0, c0 - pad); c1 = min(arr.shape[1]-1, c1 + pad)
                bbox2 = (c0, r0, c1+1, r1+1)
                cropped2 = img.crop(bbox2)
                cw2, ch2 = cropped2.size
                ow, oh = img.size
                if cw2 < ow * 0.95 or ch2 < oh * 0.95:
                    return cropped2
        except Exception:
            pass

        return img

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
    def _load_font(size: int) -> ImageFont.ImageFont:
        for path in _FONT_CANDIDATES:
            if os.path.isfile(path):
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    continue
        print("[ImageProcessor] 한글 폰트를 찾지 못해 기본 폰트를 사용합니다.")
        return ImageFont.load_default()
