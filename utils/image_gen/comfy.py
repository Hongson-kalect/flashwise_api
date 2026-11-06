"""
Client ComfyUI (HTTP) vẽ ảnh bằng FLUX.2 klein 4B distilled (4 bước, cfg 1).
Không có thư viện riêng: POST /prompt -> hỏi /history/{id} -> GET /view. Ảnh chỉ nằm ở thư mục tạm của ComfyUI
(node PreviewImage), không đẩy rác vào thư mục output của người dùng.
"""
import io
import json
import logging
import time
import uuid

import requests
from django.conf import settings
from PIL import Image

logger = logging.getLogger(__name__)

RENDER_SIZE = (1216, 832)  # 3:2, bội số của 16
OUTPUT_SIZE = (768, 512)   # ảnh lưu: WebP ~12KB, đủ cho thẻ 16:9 và 1:1 (app tự cắt)
POLL_INTERVAL = 0.3


class ComfyUnavailable(Exception):
    """ComfyUI chưa bật / không với tới (máy host tắt, sai địa chỉ): việc giữ lại hàng đợi, chờ rồi thử lại."""


class ComfyError(Exception):
    """ComfyUI nhận việc nhưng vẽ lỗi (thiếu model, workflow sai...)."""


def build_workflow(prompt, seed, steps=None, size=RENDER_SIZE):
    width, height = size
    steps = steps or settings.IMAGE_GEN_STEPS
    return {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': settings.COMFYUI_UNET, 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': settings.COMFYUI_CLIP, 'type': 'flux2', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': settings.COMFYUI_VAE}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'text': prompt, 'clip': ['2', 0]}},
        '5': {'class_type': 'CLIPTextEncode', 'inputs': {'text': '', 'clip': ['2', 0]}},
        '6': {'class_type': 'CFGGuider', 'inputs': {'model': ['1', 0], 'positive': ['4', 0], 'negative': ['5', 0], 'cfg': 1.0}},
        '7': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': 'euler'}},
        '8': {'class_type': 'Flux2Scheduler', 'inputs': {'steps': steps, 'width': width, 'height': height}},
        '9': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': seed}},
        '10': {'class_type': 'EmptyFlux2LatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '11': {'class_type': 'SamplerCustomAdvanced', 'inputs': {'noise': ['9', 0], 'guider': ['6', 0], 'sampler': ['7', 0], 'sigmas': ['8', 0], 'latent_image': ['10', 0]}},
        '12': {'class_type': 'VAEDecode', 'inputs': {'samples': ['11', 0], 'vae': ['3', 0]}},
        '13': {'class_type': 'PreviewImage', 'inputs': {'images': ['12', 0]}},
    }


def _request(method, path, **kwargs):
    try:
        return requests.request(method, settings.COMFYUI_URL + path, timeout=kwargs.pop('timeout', 30), **kwargs)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ComfyUnavailable(f'Không gọi được ComfyUI ({settings.COMFYUI_URL}): {e}') from e


def generate(prompt, seed):
    """Vẽ 1 ảnh, trả bytes PNG. ComfyUI không với tới -> ComfyUnavailable; ComfyUI báo lỗi / quá hạn -> ComfyError."""
    response = _request('POST', '/prompt', json={'prompt': build_workflow(prompt, seed), 'client_id': uuid.uuid4().hex})
    if response.status_code != 200:
        raise ComfyError(f'ComfyUI từ chối workflow ({response.status_code}): {response.text[:500]}')
    prompt_id = response.json()['prompt_id']

    deadline = time.monotonic() + settings.IMAGE_GEN_TIMEOUT
    while True:
        history = _request('GET', f'/history/{prompt_id}').json()
        entry = history.get(prompt_id)
        if entry:
            status = entry.get('status') or {}
            if status.get('status_str') == 'error':
                raise ComfyError(f"ComfyUI vẽ lỗi: {json.dumps(status.get('messages'))[:500]}")
            images = (entry.get('outputs') or {}).get('13', {}).get('images') or []
            if images:
                break
            if status.get('completed'):
                raise ComfyError('ComfyUI xong nhưng không có ảnh')
        if time.monotonic() > deadline:
            raise ComfyError(f'Quá {settings.IMAGE_GEN_TIMEOUT}s chưa có ảnh')
        time.sleep(POLL_INTERVAL)

    info = images[0]
    view = _request('GET', '/view', params={'filename': info['filename'], 'subfolder': info.get('subfolder', ''), 'type': info.get('type', 'temp')})
    if view.status_code != 200:
        raise ComfyError(f'Không lấy được ảnh ({view.status_code})')
    return view.content


def to_webp(png_bytes, size=OUTPUT_SIZE, quality=80):
    """PNG -> WebP đã thu về cỡ lưu. Trả (bytes, (rộng, cao))."""
    image = Image.open(io.BytesIO(png_bytes)).convert('RGB')
    if image.size != size:
        image = image.resize(size, Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format='WEBP', quality=quality, method=6)
    return buffer.getvalue(), image.size
