import json
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import asyncio

import hashlib
import re

def get_safe_room_id(word, language, user_language):
    # 1. Làm sạch word: chuyển về chữ thường, xóa khoảng trắng thừa
    clean_word = word.strip().lower()
    
    # 2. Xử lý ký tự đặc biệt: Chỉ giữ lại chữ cái và số (hoặc dùng MD5 cho an toàn tuyệt đối)
    # MD5 giúp độ dài room luôn cố định và không bị lỗi ký tự lạ (như tiếng Trung, Nhật, Arabic)
    word_hash = hashlib.md5(clean_word.encode('utf-8')).hexdigest()[:16]
    
    # 3. Kết hợp: language (ngôn ngữ gốc) + user_lang (ngôn ngữ đích) + hash
    # Ví dụ: en_vi_ab12cd34...
    return f"{word_hash}_{language}_{user_language}"

async def socket_message(group, data, is_close=False):
    print('Debug: socket_message',group)
    channel_layer = get_channel_layer()
    
    # Gắn flag close_now vào data để Consumer nhận diện
    if is_close:
        data['close_now'] = True

    await channel_layer.group_send(
        group,
        {
            "type": "chat_message", # Phải trùng với tên hàm trong Consumer
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

