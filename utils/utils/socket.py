import json
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import asyncio
from core.views.Collection import normalize 

import hashlib
import re

def get_safe_room_id(word,word_lang=None, user_lang=None):
    # 1. Làm sạch word: chuyển về chữ thường, xóa khoảng trắng thừa

    normalized_word = word.strip()                  # Cắt khoảng trắng 2 đầu
    normalized_word = re.sub(r'\s+', ' ', normalized_word) # Thu gọn khoảng trắng ở giữa
    normalized_word = normalized_word.lower()       # Chuyển thành chữ thường

    # 2. Băm MD5 (Bắt buộc phải mã hóa chuỗi sang định dạng bytes bằng utf-8 trước khi băm)
    hash_object = hashlib.md5(normalized_word.encode('utf-8'))
    hash_str = hash_object.hexdigest()

    if not word_lang:
        return f"{hash_str}"
    if not user_lang:
        return f"{hash_str}_{word_lang.strip().lower()}"
    return f"{hash_str}_{word_lang.strip().lower()}_{user_lang.strip().lower()}"

async def socket_message(group, data, unsubscribe=False):
    print('Debug: socket_message',group, data,'abc')
    channel_layer = get_channel_layer()
    
    # Gắn flag close_now vào data để Consumer nhận diện
    if unsubscribe:
        data['unsubscribe'] = True
    if group:
        data['word_room'] = group

    await channel_layer.group_send(
        group,
        {
            # "word_room": group,
            "type": "chat_handler", # Phải trùng với tên hàm trong Consumer
            "data": data
        })
    

async def socket_close(group, message=None):
    """
    Chủ động đóng toàn bộ kết nối trong một group.
    :param group: Tên room
    :param message: Tin nhắn cuối cùng trước khi đóng (ví dụ: "Phiên làm việc kết thúc")
    """
    data = {"close_now": True}
    if message:
        data['message'] = message
        
    socket_message(group, data, is_close=True)

def run_async_task(coro):
    try:
        # Tạo một loop mới hoàn toàn cho thread này
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Chạy loop cho đến khi coroutine AI hoàn thành
        loop.run_until_complete(coro)
    except Exception as e:
        print(f"Thread Error: {e}")
    finally:
        loop.close()

