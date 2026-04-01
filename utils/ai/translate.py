import json
import asyncio
from google import genai
from google.genai import types
from flashcardApi import settings
from django.db import transaction

from ai.models.AISense import AISense
from ai.models.AISenseContent import AISenseContent
from utils.utils import uuidv7
from utils.utils.socket import socket_message
from asgiref.sync import sync_to_async

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

async def process_translation_chunk(chunk, word, language_code, language_str, translate_lang, mapping_table, socket_room):
    try:
        # 1. Khởi tạo Schema và Client cho riêng Task này
        sense_ids = list(chunk.keys())
        schema = render_translate_schema(word, language_code, chunk, translate_lang)

        print('schema', schema)
        
        prompt = f"""
        # ROLE: Translator
        # WORD: {word} | FROM: {language_code} | TO: {language_str}
        # CONTENTS: {json.dumps(chunk, ensure_ascii=False)}
        # TASK: Translate into {language_str}. Output MINIFIED JSON only.
        """

        local_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        async with local_client.aio as client:
            response = await client.models.generate_content(
                model="gemini-2.5-flash-lite", # Đã cập nhật bản lite mới nhất 2026
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=8192,
                    response_mime_type="application/json",
                    response_schema=schema
                )
            )
        
        data = json.loads(response.text.strip())
        
        # 2. Hồi nguyên UUID (Logic mapping của bạn)
        final_chunk_data = {}
        for s_key, s_trans in data.items():
            original_s_uuid = mapping_table.get(s_key)
            if not original_s_uuid: continue
            
            original_examples = {}
            for e_key, e_trans in s_trans.get('examples', {}).items():
                original_e_uuid = mapping_table.get(e_key)
                if original_e_uuid:
                    original_examples[original_e_uuid] = e_trans
            
            final_chunk_data[original_s_uuid] = s_trans
            final_chunk_data[original_s_uuid]['examples'] = original_examples

        # 3. BẮN SOCKET NGAY LẬP TỨC (Xong cái nào bắn cái đó)
        await socket_message(
            socket_room,
            {
                "type": "TRANSLATE_SENSE_SUCCESS",
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

async def render_translate(
    word_object,
    senses,
    user_language_code,
):
    print('word', word_object,senses)
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
    for chunk in chunk_dict(temp_senses, size=3):
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
        print('r',r)
        if r:
            full_translated_data.update(r)

    # Báo cáo hoàn tất toàn bộ tiến trình
    await socket_message(socket_room, {"type": "TRANSLATE_ALL_COMPLETED"})
    return full_translated_data


def save_translate(user, translate_instance, user_language_code, sense_instances, data):
    with transaction.atomic():
        content_bulk = []
        sense_bulk = []
        save_results = {} # Trả về cho socket/frontend

        try:
            for sense_id, translations in data.items():
                # translations: { "origin_id_1": "văn bản dịch 1", "translations": "văn bản dịch tổng" }
                
                sense = next((s for s in sense_instances if str(s.id) == str(sense_id)), None)
                if not sense: continue

                struct = sense.contents or {}
                save_results[sense_id] = {}

                for origin_id, text in translations.items():
                    new_id = uuidv7.generate_uuid7()
                    
                    # 1. Tạo Instance Content mới (Sạch, không parent, không type)
                    new_content = AISenseContent(
                        id=new_id,
                        value=text,
                        language_code=user_language_code,
                        created_by=user
                    )
                    content_bulk.append(new_content)

                    # 2. Cập nhật vào cấu trúc JSON của Sense
                    if origin_id == 'translations':
                        # Case đặc biệt: Bản dịch tổng quát của Sense
                        if 'translations' not in struct:
                            struct['translations'] = {}
                        struct['translations'][user_language_code] = str(new_id)
                    else:
                        # Case thông thường: Dịch cho definition, usage, example
                        # Tìm origin_id ở đâu trong JSON và nhét new_id vào đó
                        patch_content_id(struct, origin_id, new_id, user_language_code)

                    # Lưu log để trả về kết quả
                    save_results[sense_id][origin_id] = {
                        "id": str(new_id),
                        "value": text
                    }

                sense.contents = struct
                sense_bulk.append(sense)

            # Bulk save để tối ưu performance
            if content_bulk:
                AISenseContent.objects.bulk_create(content_bulk)
            
            if sense_bulk:
                # Chỉ update cột contents
                AISense.objects.bulk_update(sense_bulk, ['contents'])

            translate_instance.status = "COMPLETED"
            translate_instance.save()

            return save_results

        except Exception as e:
            translate_instance.status = "FAILED"
            translate_instance.save()
            raise
