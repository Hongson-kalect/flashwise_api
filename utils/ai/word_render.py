import json
from venv import logger
from google import genai
from google.genai import types
from rest_framework.renderers import JSONRenderer
from ai.serializers.AIWord import AIWordSerializer
from flashcardApi import settings
from django.db import transaction

from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AISenseContent import AISenseContent
from utils.celery.fetch_image import task_fetch_image_single
from utils.helper.sense_context import SenseContext
from utils.utils import uuidv7
from utils.utils.extract_object_from_string import extract_json_fragment
from utils.utils.flatten_id import flatten_ids
from utils.utils.kanji import get_ruby_generator
from utils.utils.sense_handle import serialize_entries, serialize_senses
from utils.utils.socket import socket_message
from utils.redis.word_init import WordCacheManager
from utils.celery.translate import task_create_translate
from asgiref.sync import sync_to_async

from utils.ai.schema import word_schema, nonlatin_schema, complex_schema
from utils.ai.prompt import render_word_prompt

def get_schema(mode):
    if mode == "latin":
        return word_schema
    elif mode == "nonlatin":
        return nonlatin_schema
    elif mode == "complex":
        return complex_schema

async def ai_create_new_word(user, word_instance, language_code, user_language_code, socket_room):
    print('create new word')
    # word_instance = await sync_to_async(AIWord.objects.get)(id=word_id)
    LATIN_LANGS = ['vi', 'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'sv', 'no', 'da', 'fi', 'tr', 'cs', 'hu', 'id']
    SIMPLE_NON_LATIN = ['zh', 'ko', 'ru', 'el', 'ar', 'he', 'hi', 'th']
    
    if language_code in LATIN_LANGS:
        mode = "latin"
    elif language_code == 'ja':
        mode = "complex"
    else:
        mode = "nolatin"

    # 2. Lấy Schema và Prompt tương ứng
    current_schema = get_schema(mode)
    current_prompt = render_word_prompt(mode, word_instance.value, language_code, user_language_code)

    try:
        local_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        ai_trunks =[]
        is_first_sense_found = False
        sense_index =0
        async with local_client.aio as client:
            response = await client.models.generate_content_stream(
                model="gemini-2.5-flash-lite",
                contents=current_prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=8192, # Tăng từ 2048 lên 8192
                    response_mime_type="application/json",
                    response_schema=current_schema
                )
            )
            pointer = 0
            sense_objs=[]
            valid = False
            word_cache = WordCacheManager()
            async for chunk in response:
                if chunk.text:
                    ai_trunks.append(chunk.text)

                    # --- LOGIC ĐÁNH CHẶN LẤY ẢNH SỚM ---
                    full_text = "".join(ai_trunks)

                    if not valid:
                        word_meta_str, _ = extract_json_fragment(full_text, "metadata") 

                        if word_meta_str:
                            word_meta = json.loads(word_meta_str)
                            if not word_meta.get("should_be_saved", True):
                                print("Word rejected - Stopping stream")
                                # Gửi socket thông báo từ không hợp lệ
                                # Ngắt stream/vòng lặp tại đây
                                break
                            else:
                                valid = True

                                print("Word accepted", full_text)
                    
                    if valid:
                        while True:

                            sense_str, new_pointer = extract_json_fragment(full_text, "senses", pointer)
                            
                            if sense_str:
                                pointer = new_pointer
                                try:
                                    sense = json.loads(sense_str)
                                    id = str(uuidv7.generate_uuid7())

                                    sense_word_obj = {
                                        "id": id,
                                        "word_id": str(word_instance.id),
                                        "word_value":word_instance.value,
                                        "language_code": language_code,
                                        "created_by_id": user.id,
                                    }

                                    processed_contents = {
                                        "id":id,
                                        "metadata": {id: str(uuidv7.generate_uuid7()),**sense.get("metadata",{})},
                                        "definition": {"id": str(uuidv7.generate_uuid7()), "value": sense.get("definition")},
                                        "usage": {"id": str(uuidv7.generate_uuid7()), "value": sense.get("usage")},
                                        "examples": [
                                            {"id": str(uuidv7.generate_uuid7()), "value": ex} 
                                            for ex in sense.get("examples", [])
                                        ]
                                    }
                                    
                                    sense["id"] = id
                                    sense_objs.append(processed_contents)
                                    # sense_objs.append(sense_obj)
                                    sense_index += 1

                                    word_cache.cache_word_add_sense(language_code, word_instance.value, id, processed_contents)
                                    await socket_message(socket_room, {"type": "PARTIAL_SENSE", "payload": processed_contents})
                                    
                                    # Kích hoạt lấy ảnh 1 ngay lập tức (không đợi stream xong)
                                    img_desc = processed_contents.get('metadata',{}).get('image_keywords',None)
                                    print('img_desc', img_desc)
                                    if img_desc:
                                        print('Lấy ảnh, máy cty pixabay hoạt động hơi lỏ nên tạm tắt')
                                        # task_fetch_image_single.delay(sense_word_obj, img_desc, socket_room, temp_index=0)
                                except Exception as e: 
                                    pass
                            else:
                                break

            print('is valied', valid)
            
        full_response_text = "".join(ai_trunks)
        print('full_response_text', full_response_text)
        data = json.loads(full_response_text)

        word_data = word_cache.cache_word_get_data(language_code, word_instance.value)
        redis_translates= word_data['translates']
        redis_senses = word_data['senses']
        redis_word = word_data['word']

        print('get_data', redis_word, redis_senses, redis_translates)

        try:
            task_create_translate.delay(redis_word,redis_senses, redis_translates)
        except Exception as e:
            print(f"Error creating translate: {e}")

        # word_data = await sync_to_async(saveword)(user, word_instance, language_code, user_language_code, data, socket_room)
        word_data = await sync_to_async(saveword)(user, word_instance, language_code, user_language_code, sense_objs, socket_room)

        try:
            # ✅ Dùng DjangoJSONEncoder để convert UUID
            json_data = JSONRenderer().render(word_data)
            clean_data = json.loads(json_data)  # Convert bytes → dict
            print('Socket full data')
            await socket_message(socket_room, {"type": "FULL_SENSE",
                                    "payload": clean_data}, True)
        except Exception as socket_error:
            print(f"Failed to send full data via socket: {socket_error}")
    
    except Exception as e:
        try:
            def update_failed_status():
                word_instance.status = 'FAILED'
                word_instance.save()
            
            await sync_to_async(update_failed_status)()
            await socket_message(socket_room, {"type": "ERROR", "payload": str(e)})
        except Exception as socket_error:
            print('socket message error')
            # If socket message fails, just log it

        raise e
    

# @sync_to_async
def saveword(user, word_instance, language_code, user_language_code, entries, socket_room):
    # entries = data.get("entries", [])
    contexts: list[SenseContext] = []

    print('start save word')

    with transaction.atomic():
        try:
            # =========================
            # PHASE 1: METADATA
            # =========================
            model_fields = {f.name for f in AISenseMetadata._meta.concrete_fields}
            metadata_to_create = []

            for sense_raw in entries:
                metadata_raw = sense_raw.get("metadata", {})

                clean_data = {k: v for k, v in metadata_raw.items() if k in model_fields}
                metadata = AISenseMetadata(**clean_data, created_by=user)
                
                metadata_to_create.append(metadata)
                # Lưu trữ sense_raw để xử lý content ở phase sau
                contexts.append(SenseContext(raw=sense_raw, pos=metadata_raw.get("pos", None), metadata=metadata, id=sense_raw.get("id")))

            if not contexts:
                print('no contexts', contexts)
                word_instance.status = "REJECTED"
                word_instance.save()
                return

            AISenseMetadata.objects.bulk_create(metadata_to_create)

            # =========================
            # PHASE 2: GENERATE ALL CONTENTS (FLAT LIST)
            # =========================
            ruby_gen = get_ruby_generator()
            all_contents_to_create = []

            def prepare_content_obj(val, lang= language_code):
                if not val: return None

                if isinstance(val, dict):
                    text = val.get("text")
                    id = val.get("id")
                else:
                    text = val.get("value")
                    id = uuidv7.generate_uuid7()
                if not text: return None

                ruby = None
                if lang == "ja" and isinstance(val, dict):
                    try: ruby = ruby_gen.generate(text)
                    except: pass

                return AISenseContent(
                    id=id,
                    value=text,
                    reading=val.get("reading") if isinstance(val, dict) else None,
                    roman=val.get("roman") if isinstance(val, dict) else None,
                    ruby=ruby,
                    language_code=lang,
                    created_by=user,
                    # TYPE VÀ PARENT ĐÃ BỎ THEO YÊU CẦU
                )

            # Duyệt qua các context để khởi tạo object Content (chưa có ID)
            for ctx in contexts:
                s = ctx.raw
                
                # 1. Định nghĩa & Sử dụng
                ctx.obj_map["def"] = prepare_content_obj(s.get("definition"))
                ctx.obj_map["usage"] = prepare_content_obj(s.get("usage"))


                ctx.example_count = len(s.get("examples", []))
                # 2. Ví dụ (Mỗi ví dụ là một cặp Orig-Trans)
                for index, ex_raw in enumerate(s.get("examples", []), start=1):
                    ctx.obj_map[f'translate-{index}'] = prepare_content_obj(ex_raw)

                # Gom vào list tổng để bulk create
                all_contents_to_create.extend([obj for obj in ctx.obj_map.values() if obj])
            # Bulk create để lấy ID từ database cho toàn bộ content
            AISenseContent.objects.bulk_create(all_contents_to_create)

            # =========================
            # PHASE 3: BUILD HIERARCHICAL JSON & AISense
            # =========================
            senses_to_create = []

            for ctx in contexts:
                # Helper để lấy ID an toàn
                get_id = lambda key: str(ctx.obj_map[key].id) if ctx.obj_map.get(key) else None

                # Xây dựng cấu trúc JSON lồng nhau (Nested Structure)
                # Đây là nơi quy định quan hệ thay vì dùng Parent_id
                sense_structure = {
                    "definition": {
                        language_code: get_id("def"),
                    },
                    "usage": {
                        language_code: get_id("usage"),
                    },
                    "examples": [
                        {
                            language_code: get_id('translate-'+str(index)),
                        }
                        for index in range(1, ctx.example_count + 1)
                    ]
                }

                senses_to_create.append(
                    AISense(
                        word=word_instance,
                        language_code=language_code,
                        word_value=word_instance.value,
                        metadata=ctx.metadata,
                        contents=sense_structure, # JSON Structure mới
                        is_frozen=True,
                        created_by=user,
                        id=ctx.id
                    )
                )

            AISense.objects.bulk_create(
                senses_to_create, update_conflicts=True,
                unique_fields=['id'],  # Hoặc field nào định danh duy nhất
                update_fields=['contents', 'metadata']
                )

            # =========================
            # FINAL PHASE: REFRESH & SERIALIZE
            # =========================
            word_instance.status = "COMPLETED"
            word_instance.is_active = True
            word_instance.save()

            word_instance.refresh_from_db()
            senses = word_instance.senses.select_related('metadata').all()
            
            # Collect all IDs from JSON to hydrate
            all_content_ids = []
            for s in senses:
                # Hàm này bóc tách toàn bộ UUID có trong JSON contents
                all_content_ids.extend(flatten_ids(s.contents))
            
            contents_queryset = AISenseContent.objects.filter(id__in=all_content_ids)
            
            serialized_senses = serialize_senses(senses, contents_queryset, language_code, user_language_code)
            word_instance.processed_entries = serialize_entries(serialized_senses)
            
            return AIWordSerializer(word_instance).data

        except Exception as e:
            word_instance.status = "FAILED"
            word_instance.save()
            raise

        
