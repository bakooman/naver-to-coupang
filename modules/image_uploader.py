"""
Cloudflare R2 이미지 업로드 모듈

- PIL Image 또는 로컬 파일 경로를 Cloudflare R2에 업로드 → 공개 URL 반환
- S3-compatible API 사용 (boto3)
- 쿠팡 일괄등록 엑셀에서 이미지 URL 로 사용
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Optional

import requests
from PIL import Image

from config.settings import Settings


_TIMEOUT   = 40
_MAX_RETRY = 2


def upload_pil(
    img: Image.Image,
    filename: str = "image.jpg",
    quality: int = 90,
) -> Optional[str]:
    """PIL Image → R2 업로드 → 공개 URL 반환. 실패 시 None."""
    buf = io.BytesIO()
    fmt = "JPEG" if filename.lower().endswith((".jpg", ".jpeg")) else "PNG"
    img.save(buf, format=fmt, quality=quality)
    buf.seek(0)
    mime = f"image/{fmt.lower()}"
    return _do_upload(buf.getvalue(), filename, mime)


def upload_file(path: str | Path) -> Optional[str]:
    """로컬 파일 경로 → R2 업로드 → 공개 URL 반환. 실패 시 None."""
    p = Path(path)
    data = p.read_bytes()
    mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return _do_upload(data, p.name, mime)


def upload_url(src_url: str, filename: str = "image.jpg") -> Optional[str]:
    """외부 URL 이미지 → 다운로드 → R2 업로드. 실패 시 None."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://smartstore.naver.com/",
        }
        r = requests.get(src_url, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "image/jpeg")
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
            # webp → jpeg 변환
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return _do_upload(buf.getvalue(), "image.jpg", "image/jpeg")
        fname = Path(filename).stem + ext
        return _do_upload(r.content, fname, ct.split(";")[0].strip())
    except Exception as e:
        print(f"[Uploader] URL 다운로드 실패 ({src_url[:60]}): {e}")
        return None


# ── 내부 ────────────────────────────────────────────────────────────

def _get_r2_client():
    """boto3 S3 클라이언트 (R2 엔드포인트)."""
    import boto3  # type: ignore

    s = Settings
    endpoint = f"https://{s.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=s.R2_ACCESS_KEY_ID,
        aws_secret_access_key=s.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _do_upload(data: bytes, filename: str, mime: str) -> Optional[str]:
    """데이터를 R2에 업로드하고 공개 URL 반환."""
    s = Settings

    # 자격증명 미설정 시 조기 실패
    if not s.R2_ACCESS_KEY_ID or not s.R2_SECRET_ACCESS_KEY:
        print("[Uploader] ❌ R2 자격증명 미설정 (.env에 R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY 필요)")
        return None

    for attempt in range(_MAX_RETRY):
        try:
            client = _get_r2_client()
            client.put_object(
                Bucket=s.R2_BUCKET_NAME,
                Key=filename,
                Body=data,
                ContentType=mime,
            )
            # 공개 URL 조합
            url = f"{s.R2_PUBLIC_URL.rstrip('/')}/{filename}"
            print(f"[Uploader] ✅ R2 업로드 성공: {url}")
            return url

        except Exception as e:
            print(f"[Uploader] R2 업로드 오류 (시도 {attempt + 1}): {e}")
            if attempt < _MAX_RETRY - 1:
                time.sleep(2)

    print("[Uploader] ❌ R2 모든 시도 실패")
    return None
