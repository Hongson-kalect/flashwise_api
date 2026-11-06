import asyncio
import logging
import re
import requests
import os
import io
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from celery import shared_task
from django.db import transaction
from asgiref.sync import async_to_sync, sync_to_async

# Import đúng các model của bạn
from ai.models.AIWord import AIWord
from core.models.ImageLibrary import ImageContext, ImageLibrary, ImageLibraryContext
from ai.models.AISense import AISense 
from utils.utils.image_compress import save_sense_image
from utils.utils.socket import socket_message
from django.contrib.auth import get_user_model

User = get_user_model()
logger = logging.getLogger(__name__)

PIXABAY_URL = 'https://pixabay.com/api/'
PROVIDER = 'pixabay'
MAX_KEYWORDS = 3  # thử lần lượt tối đa 3 từ khoá ảnh của 1 nghĩa, có ảnh thì dừng
PER_PAGE = 3  # mức nhỏ nhất Pixabay cho phép; mỗi ảnh mới phải tải + nén nên càng ít càng nhanh


def images_enabled():
    """
    Chỉ lấy ảnh Pixabay khi .env có PIXABAY_API_KEY, không có thì mọi bước lấy ảnh bỏ qua (không gọi mạng).
    Bật ảnh AI vẽ (IMAGE_GEN_ENABLED, utils/image_gen) thì tắt Pixabay: Pixabay chặn request từ server nên chỉ tốn thời gian.
    """
    return bool(settings.PIXABAY_API_KEY) and not settings.IMAGE_GEN_ENABLED


def _scrub(error):
    """Nội dung lỗi requests chứa nguyên URL kèm key Pixabay: che key trước khi ghi log."""
    return re.sub(r'(key=)[^&\s\'"]+', r'\1***', str(error))


def _context_preview(context):
    first = context.images.exclude(url__isnull=True).order_by('imagelibrarycontext__order').first()
    return first.url if first else None


def _search_pixabay(keyword):
    response = requests.get(PIXABAY_URL, params={
        'key': settings.PIXABAY_API_KEY,
        'q': keyword[:100],  # Pixabay giới hạn q 100 ký tự
        'lang': 'en',  # prompt metadata sinh image_keywords bằng tiếng Anh
        'image_type': 'illustration',
        'orientation': 'horizontal',
        'safesearch': 'true',
        'per_page': PER_PAGE,
    }, timeout=10)
    response.raise_for_status()
    return response.json().get('hits') or []


def find_image(keywords):
    """
    Ảnh minh hoạ cho 1 nghĩa theo image_keywords: dùng lại ImageContext đã có (không tốn lượt API),
    chưa có thì gọi Pixabay, tải + nén ảnh về storage, tạo context / thư viện / liên kết.
    Trả (image_context_id, url ảnh đầu) hoặc None. Lỗi mạng / ảnh hỏng chỉ ghi log, không ném ra ngoài.
    """
    if isinstance(keywords, str):
        keywords = [keywords]
    for raw in (keywords or [])[:MAX_KEYWORDS]:
        keyword = ' '.join(str(raw).lower().split())
        if not keyword:
            continue

        context = ImageContext.objects.filter(description=keyword, provider=PROVIDER).first()
        preview = _context_preview(context) if context else None
        if preview:
            return context.id, preview

        try:
            hits = _search_pixabay(keyword)
        except Exception as e:
            logger.warning('Pixabay lỗi với "%s": %s', keyword, _scrub(e))
            continue
        if not hits:
            continue

        context, _ = ImageContext.objects.get_or_create(description=keyword[:255], provider=PROVIDER)
        for index, hit in enumerate(hits):
            pid = str(hit['id'])
            image, is_new = ImageLibrary.objects.get_or_create(
                provider=PROVIDER, provider_id=pid,
                defaults={
                    'metadata': {'width': hit.get('imageWidth'), 'height': hit.get('imageHeight')},
                    'attribution': {'user': hit.get('user'), 'page_url': hit.get('pageURL')},
                },
            )
            if is_new or not image.url:
                try:
                    save_sense_image(image, f'{PROVIDER}_{pid}', hit['webformatURL'])
                except Exception as e:
                    # Ảnh tải / nén hỏng: bỏ bản ghi rỗng để lần sau thử lại được
                    logger.warning('Không tải được ảnh Pixabay %s: %s', pid, _scrub(e))
                    image.delete()
                    continue
            ImageLibraryContext.objects.get_or_create(image=image, context=context, defaults={'order': index})

        preview = _context_preview(context)
        if preview:
            return context.id, preview
    return None


async def fetch_sense_image(sense, socket_room=None):
    """
    Gắn ảnh cho 1 nghĩa đang tạo (dict, chưa vào DB) theo metadata.image_keywords: ghi image_preview /
    image_context_id lên nghĩa để save_sense lưu cùng, và báo PARTIAL_IMAGE cho người đang chờ.
    Không có PIXABAY_API_KEY hoặc nghĩa chưa có từ khoá ảnh thì bỏ qua. Không bao giờ ném lỗi.
    """
    if not images_enabled():
        return None
    keywords = (sense.get('metadata') or {}).get('image_keywords')
    if not keywords:
        return None
    try:
        # Tải + nén ảnh là việc chặn: chạy ở thread riêng để không giữ thread đồng bộ dùng chung của worker
        found = await sync_to_async(find_image, thread_sensitive=False)(keywords)
    except Exception as e:
        logger.warning('Lấy ảnh cho nghĩa %s lỗi: %s', sense.get('id'), e)
        return None
    if not found:
        return None

    context_id, url = found
    sense['image_context_id'] = str(context_id)
    sense['image_preview'] = url
    if socket_room:
        try:
            await socket_message(socket_room, {"type": "PARTIAL_IMAGE", "payload": {sense['id']: url}})
        except Exception as e:
            logger.warning('Gửi socket PARTIAL_IMAGE lỗi: %s', e)
    return url


def attach_image_to_sense(sense_id, keywords):
    """Gắn ảnh cho 1 nghĩa đã lưu DB (job hàng đợi redis_image, vd lấy bù ảnh cho từ cũ). Nghĩa đã có ảnh thì bỏ qua."""
    sense = AISense.objects.filter(id=sense_id).first()
    if sense is None or sense.image_preview:
        return None
    found = find_image(keywords)
    if not found:
        return None
    sense.image_context_id, sense.image_preview = found
    sense.save(update_fields=['image_context', 'image_preview', 'updated_at'])
    return sense.image_preview


async def get_image_by_keyword(job):
    """Job hàng đợi redis_image: {sense_id, keywords}. Không có PIXABAY_API_KEY thì bỏ qua."""
    if not images_enabled():
        return None
    return await sync_to_async(attach_image_to_sense, thread_sensitive=False)(job.get('sense_id'), job.get('keywords'))

@shared_task(bind=True, retry_backoff=True, max_retries=3)
def task_fetch_image_single(self, sense_obj, keyword, socket_room, temp_index=None):
    print('vào chỗ lấy image')
    """Task xử lý ảnh: Check Context -> Fetch Pixabay -> Save Local -> Link"""

    print(sense_obj)

    sense, is_created = AISense.objects.get_or_create(**sense_obj)

    if sense.image_preview:
        print('sense duplicate', sense.id)
        return
    
    provider = 'pixabay'
    keyword = keyword.lower().strip()

    # 2. Kiểm tra Context đã có chưa (0đ API)
    print(10)

    context = ImageContext.objects.filter(description=keyword).first()
    
    if not context:
        # 3. Nếu chưa có, gọi API Pixabay
        API_KEY = os.getenv('PIXABAY_API_KEY')
        url = f"https://pixabay.com/api/?key={API_KEY}&q={keyword}&per_page=5&image_type=illustration&orientation=horizontal&safesearch=true"
        print(11)
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                raise self.retry(countdown=3) # Thử lại sau 3s nếu API lỗi
            
            data = response.json()
            pixabay_hits = data.get('hits', [])
            
            if not pixabay_hits:
                return "Không tìm thấy ảnh"

            # Tạo Context mới
            context = ImageContext.objects.create(description=keyword, provider=provider)

            # 4. Xử lý danh sách ảnh

            with transaction.atomic():
                for index, hit in enumerate(pixabay_hits):
                    pid = str(hit['id'])

                    img_obj, is_new = ImageLibrary.objects.get_or_create(
                        provider=provider,
                        provider_id=pid,
                        metadata={'width': hit['imageWidth'], 'height': hit['imageHeight']}
                    )
                    
                    if not is_new:
                        print('image exited '+ pid)
                    else:
                        # Tải và nén ảnh
                        try:
                            print('fetch_id '+ pid)
                            save_sense_image(img_obj,provider+'_'+pid, hit['webformatURL'])

                            # img_res = requests.get(hit['webformatURL'], timeout=10)
                            # if img_res.status_code == 200:
                            #     img = Image.open(io.BytesIO(img_res.content))
                            #     if img.mode in ("RGBA", "P"):
                            #         img = img.convert("RGB")
                                
                            #     output = io.BytesIO()
                            #     img.save(output, format="WEBP", quality=70, optimize=True)
                            #     output.seek(0)

                            #     # Lưu file local
                            #     file_name = f"{pid}.webp"
                            #     img_obj.file.save(file_name, ContentFile(output.read()), save=True)
                            # else:
                            #     print(pid + ' error ' + img_res.status_code)
                            #     continue
                        except Exception as e:
                            print(f"Lỗi nén ảnh {pid}: {e}")
                            continue

                    # Tạo liên kết bảng phụ
                    ImageLibraryContext.objects.create(image=img_obj, context=context, order= index)
                    preview_img = img_obj.url

        except Exception as e:
            pass
            # raise self.retry(exc=e)

    # 5. Gán Context cho Sense và Lưu
    sense.image_context = context
    sense.image_preview = preview_img
    sense.save()

    # 6. Bắn Socket báo App cập nhật UI
    # Lấy ảnh đầu tiên của Context này để hiển thị
    first_image = context.images.all().first()
    if first_image:
        async_to_sync(socket_message)(socket_room, {
            "type": "UPDATE_IMAGE",
            "payload": {
                "sense_id": sense.id,
                "temp_index": temp_index,
                "image_url": preview_img # URL này được tạo tự động từ .file.url
            }
        })


        
    
    return f"Đã hoàn thành ảnh cho {keyword}"