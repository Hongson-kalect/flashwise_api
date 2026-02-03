from ai.models.AIWord import AIWord
from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def send_socket(room_id, message_type, data):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"chat_{room_id}",
        {
            "type": "chat_message", # Tùy thuộc vào Consumer của bạn
            "message": {
                "type": message_type,
                "payload": data
            }
        }
    )

@shared_task(bind=True)
def celery_ai_create_new_word(self, user_id, word_id, language_code, user_language_code, socket_room):
    word_instance = AIWord.objects.get(id=word_id)
    
    # 1. Gọi AI để lấy danh sách Senses (Chữ)
    # Giả sử hàm này trả về generator hoặc list các sense_data
    raw_senses = ai_service.generate_senses(word_instance.value, language_code) 
    
    for data in raw_senses:
        # 2. Tạo Sense (Text only)
        sense = AISense.objects.create(
            word=word_instance,
            # ... mapping các field từ data ...
            image_description=data.get('image_desc'), # AI sinh ra mô tả ảnh
            created_by_id=user_id
        )
        
        # 3. Bắn Socket báo SENSE_TEXT_READY
        # Serialize sense này lại theo format Client cần
        serialized_sense = serialize_single_sense(sense) 
        send_socket(socket_room, "SENSE_TEXT_READY", serialized_sense)
        
        # 4. Kích hoạt Task lấy ảnh cho Sense này (Mất hút - Song song)
        task_fetch_image_for_sense.delay(sense.id, socket_room)

    # 5. Cập nhật Word status hoàn thành chữ
    word_instance.status = 'COMPLETED'
    word_instance.save()
    send_socket(socket_room, "WORD_TEXT_COMPLETED", {"word": word_instance.value})

@shared_task(retry_backoff=True, max_retries=3)
def task_fetch_image_for_sense(sense_id, socket_room):
    sense = AISense.objects.get(id=sense_id)
    desc = sense.image_description
    
    # Logic ImageLibrary như đã bàn
    image = ImageService.get_or_fetch_from_unsplash(desc)
    
    if image:
        sense.preview_image_id = image.id
        sense.save()
        
        # Bắn Socket báo SENSE_IMAGE_READY
        send_socket(socket_room, "SENSE_IMAGE_READY", {
            "sense_id": str(sense.id),
            "image_url": image.url,
            "thumbnail_url": image.thumbnail_url
        })