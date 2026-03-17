import asyncio
import requests
import os
import io
from PIL import Image
from django.core.files.base import ContentFile
from celery import shared_task
from django.db import transaction
from asgiref.sync import async_to_sync

# Import đúng các model của bạn
from ai.models.AIWord import AIWord
from core.models.ImageLibrary import ImageContext, ImageLibrary, ImageLibraryContext
from ai.models.AISense import AISense 
from utils.utils.image_compress import save_sense_image
from utils.utils.socket import socket_message
from django.contrib.auth import get_user_model

User = get_user_model()


@shared_task(bind=True, retry_backoff=True, max_retries=3)
def task_fetch_image_single(self, sense_obj, img_desc, socket_room, temp_index=None):
    print('vào chỗ lấy image')
    """Task xử lý ảnh: Check Context -> Fetch Pixabay -> Save Local -> Link"""

    print(sense_obj)

    sense, is_created = AISense.objects.get_or_create(**sense_obj)

    preview_img = None

    if not is_created:
        print('sense duplicate', sense.id)
        return
    
    provider = 'pixabay'
    img_desc = img_desc.lower().strip()

    # 2. Kiểm tra Context đã có chưa (0đ API)
    print(10)

    context = ImageContext.objects.filter(description=img_desc).first()
    
    if not context:
        # 3. Nếu chưa có, gọi API Pixabay
        API_KEY = os.getenv('PIXABAY_API_KEY')
        url = f"https://pixabay.com/api/?key={API_KEY}&q={img_desc}&per_page=5&image_type=illustration&orientation=horizontal&safesearch=true"
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
            context = ImageContext.objects.create(description=img_desc, provider=provider)

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
                    if not preview_img: preview_img = img_obj.url

        except Exception as e:
            raise self.retry(exc=e)

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


        
    
    return f"Đã hoàn thành ảnh cho {img_desc}"