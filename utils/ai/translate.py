import json
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

    language_str = user_language_code

    if(isinstance(user_language_code, list)):
        language_str = ", ".join(user_language_code)
        mode = 'multiple'

    prompt = f"""
    # ROLE: Translator
    # WORD: {word}
    # INPUT LANGUAGE: {language_code}
    # TARGET LANGUAGE: {language_str}
    # TRANSLATE CONTENTS:
    {json.dumps(temp_senses, ensure_ascii=False)}

    # TASK:
    Translate the dictionary contents from {language_code} to {language_str}.

    # OUTPUT RULE:
    - Output JSON only
    - Only return "translate"
    - DO NOT repeat original text
    - Use natural language that is appropriate to the context (avoid translating word for word).
    """

    # Giới hạn tiên trình dịch Khi vào bước này thì sẽ check translate status
    # Get or create with processing, word + lang for lang in list if is_list 
    # Hàm này chỉ dịch các sense nguyên bản, không phải patch, nên nó sẽ chỉ dịch 1 lần cho mỗi word / ngôn ngữ
    # Failed or not Exit -> Add translate process.
    # Processing -> Exit

    schema = render_translate_schema(word, language_code, temp_senses, user_language_code)

    print('schema', schema)

    # return

    try:
        local_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        async with local_client.aio as client:
            response = await client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=8192, # Tăng từ 2048 lên 8192
                    response_mime_type="application/json",
                    response_schema=schema
                )
            )
        clean_json = response.text.strip()
        data = json.loads(clean_json)

        print('clean_json',clean_json)

        final_data = {}
        for s_key, s_trans in data.items():
            original_s_uuid = mapping_table.get(s_key)
            if not original_s_uuid: continue
            
            # Khôi phục Examples
            original_examples = {}
            for e_key, e_trans in s_trans.get('examples', {}).items():
                original_e_uuid = mapping_table.get(e_key)
                if original_e_uuid:
                    original_examples[original_e_uuid] = e_trans
            
            # Ghi đè lại data với UUID thật
            final_data[original_s_uuid] = s_trans
            final_data[original_s_uuid]['examples'] = original_examples

        # Trả về final_data cho Socket/DB
        print("Data đã hồi nguyên UUID:", final_data)

        socket_room = "test" # mode = multiple => word+language; single = word+language+language_str

        await socket_message(
            socket_room,
            {
                "type": "TRANSLATE_SENSE_SUCCESS",
                "payload": data
            },
            True
        )

    except Exception as e:
        try:
            def update_failed_status():
                print('update_failed_status',e)
                pass
                # cập nhật lại instance
                # translate_instance.status = 'FAILED'
                # translate_instance.save()
            
            await sync_to_async(update_failed_status)()
            await socket_message(socket_room, {"type": "TRANSLATE_SENSE_ERROR", "payload": str(e)})
        except Exception as socket_error:
            # If socket message fails, just log it
            print('render_translate error')


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
