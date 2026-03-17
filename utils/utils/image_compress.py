import io
import os
import requests
from PIL import Image
from django.core.files.base import ContentFile


PNG_MIME = "image/png"


def download_image(url: str) -> tuple[bytes, str]:
    """
    Tải ảnh từ URL
    return: (raw_bytes, content_type)
    """
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "").lower()
    return resp.content, content_type


def is_png_image(url: str, content_type: str) -> bool:
    if PNG_MIME in content_type:
        return True
    return url.lower().endswith(".png")


def compress_image_from_url(
    url: str,
    max_width: int = 640,
    quality: int = 75,
) -> bytes:
    raw_data, content_type = download_image(url)

    img = Image.open(io.BytesIO(raw_data))
    img.load()  # ensure image fully loaded

    # Resize
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize(
            (max_width, int(img.height * ratio)),
            Image.Resampling.LANCZOS,
        )

    buffer = io.BytesIO()

    is_png = is_png_image(url, content_type)
    has_alpha = img.mode == "RGBA"

    # ==============================
    # CASE 1: PNG + transparency
    # ==============================
    # if is_png and has_alpha:
    #     img.save(
    #         buffer,
    #         format="WEBP",
    #         quality=quality, # WebP nén lossy vẫn giữ được kênh Alpha!
    #         optimize=True,
    #     )

    # # ==============================
    # # CASE 2: Other images
    # # ==============================
    # else:

    print("is_png", is_png)
    if is_png:
        img = img.convert('RGBA')
        print("PNGggggggggggg")
        # flatten alpha to white background
        # img = img.convert("P", palette=Image.ADAPTIVE, colors=256)
        # bg = Image.new("RGB", img.size, (255, 255, 255))
        # bg.paste(img, mask=img.split()[3])
        # img = bg
        img.save(
            buffer,
            format="WEBP",
            quality=quality,
            optimize=True
        )
        buffer.seek(0)
    else:

        img.save(
            buffer,
            format="WEBP",
            quality=quality,
            optimize=True,
        )

    return buffer.getvalue()


def save_sense_image(image_obj, file_name: str, url: str):
    webp_data = compress_image_from_url(url)
    image_obj.file.save(
        f"{file_name}.webp",
        ContentFile(webp_data),
        save=True,
    )
