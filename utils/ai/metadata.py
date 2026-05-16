import json
import asyncio
from google import genai
from google.genai import types
from flashcardApi import settings
from django.db import transaction
import traceback

from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from utils.utils import uuidv7
from utils.utils.socket import socket_message
from utils.utils.retry import retry_async
from asgiref.sync import sync_to_async
from .prompt import render_enhanced_prompt
from .schema import render_enhanced_schema

from utils.ai.schema import render_translate_schema

# 2. Hàm chia nhóm Senses
def chunk_dict(data, size=3):
    it = iter(data.items())
    for i in range(0, len(data), size):
        yield {k: v for k, v in [next(it) for _ in range(min(size, len(data) - i))]}

@retry_async()
async def process_metadata(chunk, word, language_code, mapping_table, socket_room):
    try:
        # 1. Khởi tạo Schema và Client cho riêng Task này
        sense_ids = list(chunk.keys())
        schema = render_enhanced_schema(sense_ids)

        print('schema', schema)
        
        prompt = render_enhanced_prompt(word, language_code, chunk)

        local_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        async with local_client.aio as client:
            try:
                response = await client.models.generate_content(
                    model="gemini-2.5-flash-lite", # Đã cập nhật bản lite mới nhất 2026
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        max_output_tokens=8192,
                        response_mime_type="application/json",
                        response_schema=schema
                    )
                )
            except Exception as e:
                print('Translate Error gemini-2.5-flash-lite', e)
                try:
                    response = await client.models.generate_content(
                        model="gemini-2.5-flash", # Đã cập nhật bản lite mới nhất 2026
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            max_output_tokens=8192,
                            response_mime_type="application/json",
                            response_schema=schema
                        )
                    )
                except Exception as e:
                    print('Translate Error gemini-2.5-flash', e)
                    try:
                        response = await client.models.generate_content(
                            model="gemini-2.5-pro", # Đã cập nhật bản lite mới nhất 2026
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                max_output_tokens=8192,
                                response_mime_type="application/json",
                                response_schema=schema
                            )
                        )
                    except Exception as e:
                        print(f"Translate gemini-2.5-pro error, trigger local ai: {e}")
                        await socket_message(socket_room, {"type": "TRANSLATE_SENSE_ERROR", "payload": str(e)})
                        return None
        
        data = json.loads(response.text.strip())
        
        # 2. Hồi nguyên UUID (Logic mapping của bạn)
        final_chunk_data = {}
        for s_key, s_trans in data.items():
            original_s_uuid = mapping_table.get(s_key)
            if not original_s_uuid: continue
            
            final_chunk_data[original_s_uuid] = s_trans

        # 3. BẮN SOCKET NGAY LẬP TỨC (Xong cái nào bắn cái đó)
        print('final_chunk_data', final_chunk_data)
        await socket_message(
            socket_room,
            {
                "type": "METADATA_SENSE",
                "payload": final_chunk_data # Trả về data đã hồi nguyên UUID
            },
            True
        )
        return final_chunk_data

    except Exception as e:
        print(f"Error in chunk {list(chunk.keys())}: {e}")
        await socket_message(socket_room, {"type": "TRANSLATE_SENSE_ERROR", "payload": str(e)})
        return None # Hoặc trả về lỗi để gather xử lý

import redis, json
from utils.redis.word_init import WordCacheManager
async def ai_create_metadata(props):

    try:
        
        r_queue = redis.Redis(host='redis', port=6379, db=0)
        cache = WordCacheManager()
        socket_room = 'test'

        word = props.get('word_value')
        language_code = props.get('language_code')
        current_senses = props.get('current_senses')

        senses = {sense['id']:{'definition': sense['contents']['definition'][language_code]['value'], "pos":sense['pos']} for sense in current_senses}
        mapping_sense = {sense["id"]: sense for sense in current_senses}

        mapping_table = {}
        temp_senses = {}

        print('senses', senses)
        
        for s_idx, (s_uuid, s_content) in enumerate(senses.items(), 1):

            s_key = f"s{s_idx}"
            mapping_table[s_key] = s_uuid
            temp_senses[s_key] = s_content

        # Tạo danh sách các Task
        # tasks = []
        # for chunk in chunk_dict(temp_senses, size=2):
        # tasks.append(
        result = await process_metadata(
                temp_senses, word, language_code, mapping_table, socket_room
            )
        # )

        # Vít ga song song
        # Kết quả trả về sẽ là một list các final_chunk_data
        # results = await asyncio.gather(*tasks)

        # Lọc bỏ các kết quả None (do lỗi) và gộp lại nếu cần lưu DB tổng
        full_metadata_data = []

        keywords = {}

        # for result in results:
        print('result aaaaaaaaaa', result)
        # Lấy image_keyword để lấy ảnh, hoặc lấy list keyword, sau đó search trong db sau đó chạy song song api để lấy ảnh, tạo context và lib và gán preview 

        for sense_id, sense_metadata in result.items():
            sense = mapping_sense.get(sense_id)

            sense['metadata'] = sense_metadata or {}
            
            full_metadata_data.append(sense)

        # tìm keywords đã tồn tại, gán image và preview vào sense đang tạo

        # batch api call nếu có thể phân biệt được keyword từng sense, gán khi có data trả về
        # nếu ko phân biệt được thì gọi lần lượt thôi, gán cho sense và gửi socket nếu cần.

        # Báo cáo hoàn tất toàn bộ tiến trình
        asyncio.create_task(socket_message(socket_room, {"type": "GET_METADATA_COMPLETED"}))
        return full_metadata_data
    
    except Exception as e:
        traceback.print_exc()
        print(f"Translate Error: {e}")

def create_content_instance(value, lang_code, content_type=None, audio=None, reading=None):
    """
    Khởi tạo instance AISenseContent (chưa save) để đưa vào danh sách bulk_create.
    """
    id = uuidv7.generate_uuid7()
    return AISenseContent(
        id=id,
        value=value,
        type=content_type, # Optional: Dùng cho dashboard/thống kê sau này
        language_code=lang_code,
        audio=audio,
        reading=reading,
        is_ai_created=True
    )

def save_metadata(data):
    # Lấy danh sách các sense. 
    content_bulk = []
    sense_ids = data.keys()
    sense_instances = AISense.objects.filter(id__in=sense_ids).all()

    # Lấy danh sách các content hiện tại của các sense
    # Map các sense và gán các bản dịch mới vào cấu trúc JSON
    for sense in sense_instances:
        struct = sense.contents or { "definition": {}, "usage": {}, "examples": {}, "translations": {} }
        new_trans = data.get(str(sense.id))
        if not new_trans:
            continue

        new_def = new_trans.get('definition', {})
        new_usage = new_trans.get('usage', {})
        new_examples = new_trans.get('examples', {})
        new_translations = new_trans.get('translations', {})
        
        for index, content in enumerate([new_def, new_usage, new_examples,new_translations]):
            type = index == 0 and 'definition' or index == 1 and 'usage' or index == 2 and 'examples' or 'translations'

            if (type == 'examples'):
                for id, example in content.items():
                    for lang, value in example.items():
                        new_content = create_content_instance(value, lang, content_type='example')
                        content_bulk.append(new_content)
                        struct['examples'].setdefault(id,{})[lang] = str(new_content.id)
            
            else:

                for lang, value in content.items():
                    new_content = create_content_instance(value, lang, content_type=type)
                    content_bulk.append(new_content)
                    struct.setdefault(type, {})[lang] = str(new_content.id)

        sense.contents = struct
    with transaction.atomic():
        # 3. Bulk Update để tối ưu hiệu năng (chỉ 1 câu lệnh SQL duy nhất)
        AISense.objects.bulk_update(sense_instances, ['contents'])
        AISenseContent.objects.bulk_create(content_bulk)

    return

import asyncio
import httpx
from django.http import JsonResponse

async def call_external_api(client, query_id, url, payload):
    """
    Hàm helper để gọi API và đính kèm query_id để phân biệt
    """
    try:
        # Giả sử bạn dùng POST để gửi prompt
        response = await client.post(url, json=payload, timeout=20.0)
        response.raise_for_status()
        return {"query_id": query_id, "data": response.json(), "status": "success"}
    except Exception as e:
        return {"query_id": query_id, "error": str(e), "status": "error"}

async def flashwise_multi_query_view(request):
    # 1. Khởi tạo dữ liệu
    api_url = "https://pixaybay...." # URL ảo
    queries = [
        {"id": "sense_id", "payload": {"keyword": "Query 1 prompt here..."}},

        {"id": "enhanced_data", "payload": {"prompt": "Query 2 prompt here..."}},
    ]
    
    results = {}
    
    # 2. Sử dụng httpx.AsyncClient để gọi song song
    async with httpx.AsyncClient() as client:
        try:
            # Tạo danh sách các task
            tasks = [
                call_external_api(client, q["id"], api_url, q["payload"]) # hoặc có thể truyền cả sense vào để trực tiếp cập nhật sense khi hoàn tất api
                for q in queries
            ]
            
            # Chạy song song tất cả các task
            responses = await asyncio.gather(*tasks)
            
            # Phân biệt kết quả dựa trên query_id
            for res in responses:
                results[res["query_id"]] = res
                
        except Exception as global_err:
            print(f"Global Error: {global_err}")
            
        finally:
            # 3. Khối FINALLY - Luôn chạy khi toàn bộ API hoàn tất (hoặc lỗi)
            # Ví dụ: Log dữ liệu, đóng kết nối, hoặc cập nhật trạng thái Task
            print("--- All API calls finished. Cleaning up or Logging... ---")
            # Bạn có thể thực hiện logic hậu xử lý ở đây
            results["metadata"] = {"processed_at": "2026-05-12", "all_tasks_done": True}

    return JsonResponse(results)