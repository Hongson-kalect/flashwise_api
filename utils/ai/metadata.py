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
from .prompt import get_enhanced_prompt
from .schema import get_enhanced_schema

from utils.ai.schema import render_translate_schema

def get_minified_schema(sense_ids, langs):
    lang_props = {l: {"type": "string"} for l in langs}
    lang_array_props = {l: {"type": "array", "items": {"type": "string"}} for l in langs}
    
    sense_props = {}
    for s_id in sense_ids:
        sense_props[s_id] = {
            "type": "object",
            "properties": {
                "d": {"type": "object", "properties": lang_props, "required": langs}, # definition
                "u": {"type": "object", "properties": lang_props, "required": langs}, # usage
                "tr": {"type": "object", "properties": lang_array_props, "required": langs} # translations
            },
            "required": ["d", "u", "tr"]
        }
    
    return {
        "type": "object",
        "properties": sense_props,
        "required": list(sense_props.keys())
    }

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
        schema = get_enhanced_schema(sense_ids)

        print('schema', schema)
        
        prompt = get_enhanced_prompt(word, language_code, chunk)

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

def patch_content_id(data, target_id, new_id, lang_dest):
    """
    Tìm target_id (bản gốc) trong JSON và điền new_id vào lang_dest tương ứng.
    """
    if isinstance(data, dict):
        # Nếu dict này chứa target_id ở bất kỳ key nào
        if any(str(v) == str(target_id) for v in data.values()):
            data[lang_dest] = str(new_id)
            return True
        # Nếu không, đào sâu vào các key khác
        for v in data.values():
            if patch_content_id(v, target_id, new_id, lang_dest):
                return True
    elif isinstance(data, list):
        for item in data:
            if patch_content_id(item, target_id, new_id, lang_dest):
                return True
    return False
import redis, json
from utils.redis.word_init import WordCacheManager
async def ai_create_metadata(props):

    try:
        print('ai_create_metadata', props)
        
        r_queue = redis.Redis(host='redis', port=6379, db=0)
        cache = WordCacheManager()
        socket_room = 'test'

        word = props.get('word_value')
        language_code = props.get('language_code')
        user_language_code = props.get('user_language_code')
        senses_obj = props.get('missing_metadata') # {id: definition}
        current_senses = props.get('current_senses')

        senses = {[sense['id']]:sense['definition']['value'] for sense in senses_obj}
        mapping_sense = {sense["id"]: sense for sense in current_senses}

        mapping_table = {}
        temp_senses = {}

        print('senses', senses)
        
        for s_idx, (s_uuid, s_def) in enumerate(senses.items(), 1):

            s_key = f"s{s_idx}"
            mapping_table[s_key] = s_uuid

        # Tạo danh sách các Task
        tasks = []
        # for chunk in chunk_dict(temp_senses, size=2):
        tasks.append(
            process_metadata(
                senses, word, language_code, mapping_table, socket_room
            )
        )

        # Vít ga song song
        # Kết quả trả về sẽ là một list các final_chunk_data
        results = await asyncio.gather(*tasks)

        # Lọc bỏ các kết quả None (do lỗi) và gộp lại nếu cần lưu DB tổng
        full_metadata_data = []

        for result in results:
            print('result aaaaaaaaaa', result)
            for sense_id, sense_metadata in result.items():
                sense = mapping_sense.get(sense_id)

                sense.metadata = sense_metadata or {}
                
                
                full_metadata_data.append(sense)

        # for r in results:
        #     # print('r',r)
        #     if r:
        #         full_translated_data.update(r)

        # print('full_translated_data',full_translated_data)

        # r_queue.rpush("redis_translate_result", json.dumps({
        #     "data": full_translated_data
        # }))

        # await sync_to_async(save_translate)(full_translated_data)

        # cache_manager.cache_word_set_status( language_code,word, 'REDIS-CACHED')


        # Báo cáo hoàn tất toàn bộ tiến trình
        asyncio.create_task(socket_message(socket_room, {"type": "TRANSLATE_ALL_COMPLETED"}))
        return full_metadata_data
    
    except Exception as e:
        traceback.print_exc()
        print(f"Translate Error: {e}")

async def render_translate(
    word_object,
    senses,
    user_language_code,
):
    cache_manager = WordCacheManager()
    socket_room = 'test'

    language_code = word_object.get("language_code", None)
    word = word_object.get("value", None)

    mapping_table = {}
    temp_senses = {}
    
    for s_idx, (s_uuid, s_data) in enumerate(senses.items(), 1):
        s_key = f"s{s_idx}"
        mapping_table[s_key] = s_uuid
        
        # Xử lý Examples bên trong Sense
        temp_examples = {}
        for e_idx, (e_uuid, e_val) in enumerate(s_data.get('examples', {}).items(), 1):
            e_key = f"e{s_idx}_{e_idx}" # Ví dụ: e1_1, e1_2
            mapping_table[e_key] = e_uuid
            temp_examples[e_key] = e_val.get('value') # Chỉ gửi text để dịch
            
        temp_senses[s_key] = {
            "definition": s_data.get('definition', {}).get('value'),
            "usage": s_data.get('usage', {}).get('value'),
            "examples": temp_examples
        }

    base_lang = ['en','zh','es','fr','ar','ja','ko','de','pt','vi'] # Sau còn cần kiểm tra xem đã dịch những ngôn ngữ nào nữa...

    # B1: Gộp 2 list và chuyển thành set để lọc trùng
    merged_set = set(user_language_code + base_lang)

    # B2: Loại bỏ string (dùng discard để không bị lỗi nếu string không tồn tại)
    merged_set.discard(language_code)

    # B3: Chuyển ngược lại thành list (nếu cần)
    translate_lang = list(merged_set)

    language_str = user_language_code

    if(isinstance(user_language_code, list)):
        language_str = ", ".join(translate_lang)
        mode = 'multiple'

    # Tạo danh sách các Task
    tasks = []
    for chunk in chunk_dict(temp_senses, size=2):
        tasks.append(
            process_translation_chunk(
                chunk, word, language_code, language_str, 
                translate_lang, mapping_table, socket_room
            )
        )

    # Vít ga song song
    # Kết quả trả về sẽ là một list các final_chunk_data
    results = await asyncio.gather(*tasks)

    # Lọc bỏ các kết quả None (do lỗi) và gộp lại nếu cần lưu DB tổng
    full_translated_data = {}
    for r in results:
        # print('r',r)
        if r:
            full_translated_data.update(r)

    print('full_translated_data',full_translated_data)

    await sync_to_async(save_translate)(full_translated_data)

    cache_manager.cache_word_set_status( language_code,word, 'REDIS-CACHED')


    # Báo cáo hoàn tất toàn bộ tiến trình
    await socket_message(socket_room, {"type": "TRANSLATE_ALL_COMPLETED"})
    return full_translated_data

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

def save_translate(data):
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