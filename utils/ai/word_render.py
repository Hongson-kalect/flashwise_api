import traceback
import json
import redis
import asyncio
from venv import logger
from google import genai
from google.genai import types
from rest_framework.renderers import JSONRenderer
from ai.serializers.AIWord import AIWordSerializer
from flashcardApi import settings
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from ai.models.AISense import AISense
from ai.models.AIWord import AIWord
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AISenseContent import AISenseContent
from utils.celery.fetch_image import task_fetch_image_single
from utils.helper.sense_context import SenseContext
from utils.utils import uuidv7
from utils.utils.extract_object_from_string import extract_json_fragment
from utils.utils.flatten_id import flatten_ids
from utils.utils.kanji import get_ruby_generator
from utils.utils.sense_handle import serialize_entries, get_user_lang_content
from utils.utils.socket import socket_message, get_safe_room_id
from utils.redis.word_init import WordCacheManager
from utils.celery.translate import task_create_translate
from asgiref.sync import sync_to_async

from utils.ai.schema import word_schema, nonlatin_schema, complex_schema
from utils.ai.prompt import render_word_prompt 
from utils.celery.fetch_image import get_image_by_keyword 
from utils.ai.translate import ai_create_translate_sema 
from utils.ai.metadata import ai_create_metadata 
from core.views.Collection import normalize

def get_schema(mode='latin'):
    if mode == "latin":
        return word_schema
    elif mode == "nonlatin":
        return nonlatin_schema
    elif mode == "complex":
        return complex_schema
    
async def ai_create_new_word_sema(word_info):

    cache = WordCacheManager()
    r_queue = redis.Redis(host='redis', port=6379, db=0)

    word_id = word_info.get('word_id', None)
    user_id = word_info.get('user_id', None)
    word_value = normalize(word_info.get('value', ''))
    language_code = word_info.get('language_code', None)
    user_language_code = word_info.get('user_language_code', None)

    socket_room= get_safe_room_id(word_value, language_code)
    print(f"socket_room: {socket_room}")

    # word_instance = await sync_to_async(AIWord.objects.get)(id=word_id)
    LATIN_LANGS = ['vi', 'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'sv', 'no', 'da', 'fi', 'tr', 'cs', 'hu', 'id']
    SIMPLE_NON_LATIN = ['zh', 'ko', 'ru', 'el', 'ar', 'he', 'hi', 'th']
    # models = ['gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-2.5-pro']
    
    if language_code in LATIN_LANGS:
        mode = "latin"
    elif language_code == 'ja':
        mode = "complex"
    else:
        mode = "nolatin"

    # 2. Lấy Schema và Prompt tương ứng
    current_schema = get_schema(mode)
    current_prompt = render_word_prompt(word_value, language_code)

    try:
        models = settings.GEMINI_API_MODEL
        ai_trunks = []
        is_valid = False
        
        # Tập hợp quản lý toàn bộ task chạy ngầm (Socket, Ảnh, Dịch, Metadata)
        background_tasks = set()

        attempts = 0
        while not ai_trunks and attempts < len(models):
            local_client = genai.Client(api_key=settings.GEMINI_API_KEY)

            async with local_client.aio as client:
                model = models[attempts]
                attempts += 1

                try:
                    response = await client.models.generate_content_stream(
                        model=model,
                        contents=current_prompt,
                        config=types.GenerateContentConfig(
                            max_output_tokens=8192,
                            response_mime_type="application/json",
                            response_schema=current_schema,
                            thinking_config=types.ThinkingConfig(thinking_budget=0)
                        )
                    )   
                    pointer = 0
                    sense_objs = []
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
                                        await sync_to_async(
                                            lambda: AIWord.objects.filter(id=word_id).update(
                                                status='REJECTED', updated_at=timezone.now()
                                            )
                                        )()
                                        break
                                    else:
                                        valid = True
                            
                            if valid:
                                while True:
                                    sense_str, new_pointer = extract_json_fragment(full_text, "senses", pointer)
                                    if sense_str:
                                        pointer = new_pointer
                                        try:
                                            sense = json.loads(sense_str)
                                            id = str(uuidv7.generate_uuid7())

                                            processed_contents = {
                                                "id": id,
                                                "word_id": word_id,
                                                "word_value": word_value,
                                                "language_code": language_code,
                                                "is_offensive": sense.get("is_offensive"),
                                                "pos": sense.get("pos"),
                                                "level": sense.get("level"),
                                                "register": sense.get("register"),
                                                "ipas": sense.get("ipas"),
                                                "contents": {
                                                    "definition": {language_code: sense.get("definition")},
                                                    "usage": {language_code: sense.get("usage")},
                                                    "examples": [
                                                        {language_code: ex} for ex in sense.get("examples", [])
                                                    ],
                                                }
                                            }
                                            sense_objs.append(processed_contents)
                                            word_cache.cache_word_add_sense(language_code, word_value, id, processed_contents)
                                            
                                            # 🚀 1. Gửi socket PARTIAL ngầm an toàn
                                            s_task = asyncio.create_task(socket_message(socket_room, {"type": "PARTIAL_SENSE", "payload": {id:processed_contents}}))
                                            background_tasks.add(s_task)
                                            s_task.add_done_callback(background_tasks.discard)
                                            
                                            # 🚀 2. Kích hoạt lấy ảnh ngầm ngay lập tức
                                            img_desc = processed_contents.get('metadata', {}).get('image_keywords', None)
                                            if img_desc:
                                                img_task = asyncio.create_task(get_image_by_keyword({
                                                    "sense_info": processed_contents,
                                                    "keyword": img_desc
                                                }))
                                                background_tasks.add(img_task)
                                                img_task.add_done_callback(background_tasks.discard)

                                            # Lấy thông tin ngôn ngữ từ cache phục vụ cho việc dịch thuật cuốn chiếu
                                            word_data_tmp = word_cache.cache_word_get_data(language_code, word_value)
                                            redis_user_language_code = word_data_tmp.get('langs', [language_code])

                                            # 🚀 3. KÍCH HOẠT CHẠY NGẦM METADATA & BẢN DỊCH CUỐN CHIẾU CHO RIÊNG SENSE NÀY
                                            # Đóng gói mảng chỉ chứa 1 mục duy nhất để AI xử lý siêu tốc (gần như tức thì)
                                            single_sense_list = [processed_contents]

                                            meta_task = asyncio.create_task(ai_create_metadata({
                                                "word_value": word_value,
                                                "language_code": language_code,
                                                "current_senses": single_sense_list
                                            }))
                                            background_tasks.add(meta_task)
                                            meta_task.add_done_callback(background_tasks.discard)

                                            trans_task = asyncio.create_task(ai_create_translate_sema({
                                                "word_value": word_value,
                                                "language_code": language_code,
                                                "user_language_code": redis_user_language_code,
                                                "missing_translate": single_sense_list,
                                                "current_senses": single_sense_list
                                            }))
                                            background_tasks.add(trans_task)
                                            trans_task.add_done_callback(background_tasks.discard)

                                        except Exception as e: 
                                            print('error on exec ai trunks', e)
                                            pass
                                    else:
                                        break
                    
                    is_valid = valid
                except Exception as e:
                    if attempts == len(models):
                        def word_failed(word_instance_fn):
                            word_instance_fn.status = 'FAILED'
                            word_instance_fn.save()

                        await sync_to_async(word_failed)(word_instance)
                        return {"word_id": word_id, "senses": [], "error": str(e)}
                    print(f'Word Error in attempt {attempts} with {model}:', e)

        print('full_response_text', sense_objs)

        # =====================================================================
        # 🏁 CHẶNG CUỐI: ĐỢI TOÀN BỘ CÁC TASK CHẠY NGẦM (ẢNH, DỊCH, META) HOÀN THÀNH XONG XUÔI
        if background_tasks:
            print(f"Đang đợi {len(background_tasks)} tác vụ ngầm gối đầu hoàn thành...")
            await asyncio.gather(*background_tasks, return_exceptions=True)

        try:
            print('Lưu toàn bộ các sense vào postgres khi đã hoàn tất hết các chạy ngầm', sense_objs)
            await sync_to_async(save_sense)(sense_objs, word_id)

            # Load lại dữ liệu word và đồng bộ cache
            word_instance = await sync_to_async(get_word_by_id)(word_id)
            # senses_instance = word_instance.prefetched_senses

            # entries = serialize_entries(senses_instance)
            # word_instance.processed_entries = entries

            data = AIWordSerializer(word_instance).data

            print('data', word_instance.senses.all(), data)
            cache.cache_word(language_code=language_code, word_id=word_id, word_val=word_value, data=data.get('senses'))

            # Gửi socket FULL chốt hạ dữ liệu cuối cùng
            final_socket_task = asyncio.create_task(
                socket_message(socket_room, {"type": "FULL_SENSE", "payload": data}, True)
            )
            background_tasks.add(final_socket_task)
            final_socket_task.add_done_callback(background_tasks.discard)

        except Exception as e:
            traceback.print_exc()
            print(f"Error creating final database and cache records: {e}")

        return {"id": word_id, "senses": sense_objs, "is_valid": is_valid}
    
    except Exception as e:
        # Cách 1: In đầy đủ stack trace ra console (Dễ nhìn nhất khi dev)
        traceback.print_exc()
        print(f"Error processing word: {e}")
        try:
            if not word_instance:
                word_instance = AIWord.objects.get(id=word_id)
            def update_failed_status():
                word_instance.status = 'FAILED'
                word_instance.save()
            
            await sync_to_async(update_failed_status)()
            await socket_message(socket_room, {"type": "ERROR", "payload": str(e)})
        except Exception as socket_error:
            print('socket message error')
            # If socket message fails, just log it

        raise e

def get_word_by_id(word_id):
    sense_qs = AISense.objects.filter(is_official=True).select_related('metadata', 'previous').order_by('-updated_at')

    # 4. Gộp vào query chính
    word_instance = AIWord.objects.filter(id=word_id).prefetch_related(
        Prefetch('senses', queryset=sense_qs)
    ).first()

    return word_instance
def save_sense(sense_obj, word_id):
    # Chuyển list dict thành list Model Instance
    metadata_instances = []
    sense_instances =[]
    for s in sense_obj:
        metadata_instances.append(AISenseMetadata(**s['metadata']))
        metadata_id = s.pop('metadata').get('id')
        
        sense_instances.append(AISense(metadata_id=metadata_id, **s))

    # Bây giờ mới gọi bulk_create
    AISenseMetadata.objects.bulk_create(metadata_instances)
    AISense.objects.bulk_create(sense_instances)
    AIWord.objects.filter(id=word_id).update(status='COMPLETED')

async def ai_create_new_word(user, word_instance, language_code, user_language_code, socket_room):
    print('create new word')
    cache = WordCacheManager()

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
        async with local_client.aio as client:
            response = await client.models.generate_content_stream(
                model="gemini-2.5-flash-lite",
                contents=current_prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=8192, # Tăng từ 2048 lên 8192
                    response_mime_type="application/json",
                    response_schema=current_schema,
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
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
                                        "collocations": sense.get("collocations",[]),
                                        "idioms": sense.get("idioms",[]),
                                        "metadata": {**sense.get("metadata",{})},
                                        "definition": { **sense.get("definition")},
                                        "usage": { **sense.get("usage")},
                                        "examples": [
                                            { **ex} 
                                            for ex in sense.get("examples", [])
                                        ]
                                    }
                                    sense_objs.append(processed_contents)
                                    word_cache.cache_word_add_sense(language_code, word_instance.value, id, processed_contents)
                                    await socket_message(socket_room, {"type": "PARTIAL_SENSE", "payload": processed_contents})
                                    
                                    # Kích hoạt lấy ảnh 1 ngay lập tức (không đợi stream xong)
                                    img_desc = processed_contents.get('metadata',{}).get('image_keywords',None)
                                    if img_desc:
                                        print('Lấy ảnh, máy cty pixabay hoạt động hơi lỏ nên tạm tắt')
                                        # task_fetch_image_single.delay(sense_word_obj, img_desc, socket_room, temp_index=0)
                                except Exception as e: 
                                    print('error on exec ai trunks',e)
                                    pass
                            else:
                                break
        print('full_response_text', sense_objs)

        word_data = word_cache.cache_word_get_data(language_code, word_instance.value)
        redis_translates= word_data['translates']
        redis_senses = word_data['senses']
        redis_word = word_data['word']

        # word_data = await sync_to_async(saveword)(user, word_instance, language_code, user_language_code, data, socket_room)
        word_data = await sync_to_async(saveword)(user, word_instance, language_code, user_language_code, sense_objs, socket_room)

        try:
            task_create_translate.delay(redis_word,redis_senses, redis_translates)
        except Exception as e:
            print(f"Error creating translate: {e}")

        try:
            # ✅ Dùng DjangoJSONEncoder để convert UUID
            cache.cache_word_set_status(language_code, word_instance.value, 'SENSE_COMPLETED')
            json_data = JSONRenderer().render(word_data)
            clean_data = json.loads(json_data)  # Convert bytes → dict
            print('Socket full data')
            await socket_message(socket_room, {"type": "FULL_SENSE",
                                    "payload": clean_data}, True)
        except Exception as socket_error:
            print(f"Failed to send full data via socket: {socket_error}")
    
    except Exception as e:
        # Cách 1: In đầy đủ stack trace ra console (Dễ nhìn nhất khi dev)
        traceback.print_exc()
        print(f"Error processing word: {e}")
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
def saveword(user_id, word_instance, language_code, user_language_code, entries, socket_room):
    # entries = data.get("entries", [])
    ruby_gen = get_ruby_generator()
    contexts: list[SenseContext] = []
    cache_manager = WordCacheManager()
    
    def prepare_content_obj(val, lang=language_code, type=None):
        if not val: return None

        if isinstance(val, dict):
            value = val.get("value")
            id = val.get("id")
        else:
            value = val
            id = uuidv7.generate_uuid7()
        if not value: return None

        ruby = None
        if lang == "ja" and isinstance(val, dict):
            try: ruby = ruby_gen.generate(value)
            except: pass

        return AISenseContent(
            id=id,
            value=value,
            reading=val.get("reading") if isinstance(val, dict) else None,
            roman=val.get("roman") if isinstance(val, dict) else None,
            ruby=ruby,
            language_code=lang,
            created_by_id=user_id,
            type=type,
            # TYPE VÀ PARENT ĐÃ BỎ THEO YÊU CẦU
        )


    try:
        # =========================
        # PHASE 1: METADATA
        # =========================
        model_fields = {f.name for f in AISenseMetadata._meta.concrete_fields}
        metadata_to_create = []

        for sense_raw in entries:
            metadata_raw = sense_raw.get("metadata", {})

            clean_data = {k: v for k, v in metadata_raw.items() if k in model_fields}
            metadata = AISenseMetadata(**clean_data, created_by_id=user_id)
            
            metadata_to_create.append(metadata)
            # Lưu trữ sense_raw để xử lý content ở phase sau
            contexts.append(SenseContext(raw=sense_raw, pos=metadata_raw.get("pos", None), metadata=metadata, id=sense_raw.get("id")))

        if not contexts:
            print('no contexts', contexts)
            word_instance.status = "REJECTED"
            word_instance.save()
            return

        # =========================
        # PHASE 2: GENERATE ALL CONTENTS (FLAT LIST)
        # =========================
        all_contents_to_create = []



        # Duyệt qua các context để khởi tạo object Content (chưa có ID)
        # for ctx in contexts:
        #     s = ctx.raw
            
        #     # 1. Định nghĩa & Sử dụng
        #     ctx.obj_map["def"] = prepare_content_obj(s.get("definition"), type="definition")
        #     ctx.obj_map["usage"] = prepare_content_obj(s.get("usage"), type="usage")
        #     ctx.obj_map["collocations"] = prepare_content_obj(s.get("collocations"),type="collocations")
        #     ctx.obj_map["idioms"] = prepare_content_obj(s.get("idioms"), type = "idioms")


        #     ctx.example_count = len(s.get("examples", []))
        #     # 2. Ví dụ (Mỗi ví dụ là một cặp Orig-Trans)
        #     for index, ex_raw in enumerate(s.get("examples", []), start=1):
        #         ctx.obj_map[f'translate-{index}'] = prepare_content_obj(ex_raw)

        #     # Gom vào list tổng để bulk create
        #     all_contents_to_create.extend([obj for obj in ctx.obj_map.values() if obj])
        # Bulk create để lấy ID từ database cho toàn bộ content
        
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
                # "definition": {
                #     language_code: get_id("def"),
                # },
                # "usage": {
                #     language_code: get_id("usage"),
                # },
                # "examples": {
                #     get_id('translate-'+str(index)):{
                #         language_code: get_id('translate-'+str(index)),
                #     }
                #     for index in range(1, ctx.example_count + 1)
                # },
                # "collocations": get_id("colocations"),
                # "idioms": get_id("idioms"),
                **ctx.raw
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

        word_instance.status = "COMPLETED"
        word_instance.is_active = True
        with transaction.atomic():
            metadatas = AISenseMetadata.objects.bulk_create(metadata_to_create)
            # AISenseContent.objects.bulk_create(all_contents_to_create)
            senses = AISense.objects.bulk_create(
                senses_to_create, update_conflicts=True,
                unique_fields=['id'],  # Hoặc field nào định danh duy nhất
                update_fields=['contents', 'metadata']
                )
            word_instance.save()

        # meta_map = {
        #     str(metadata.id): {
        #         'id': str(metadata.id),
        #         'pos': metadata.pos,
        #         'level':metadata.level,
        #         'synonyms':metadata.synonyms,
        #         'antonyms':metadata.antonyms,
        #         'relateds':metadata.relateds,
        #         'forms':metadata.forms,
        #         'tags':metadata.tags,

        #         'created_at': metadata.created_at,
        #         'updated_at': metadata.updated_at
        #     } for metadata in metadatas
        # }

        # redis_save = []
        # for sense in senses:

        #     print("metadata", sense.metadata_id)

        #     redis_save.append({
        #         'id': str(sense.id),
        #         'metadata': meta_map.get(str(sense.metadata_id), None),
        #         'contents': sense.contents
        #     })

        # print(redis_save)

        # cache_manager.cache_word(language_code, word_instance.value, redis_save)
        # cache_manager.cache_word_clear_specific(language_code, word_instance.value)

        # =========================
        # FINAL PHASE: REFRESH & SERIALIZE
        # =========================

        word_instance.refresh_from_db()
        senses = word_instance.senses.select_related('metadata').all()
        
        # Collect all IDs from JSON to hydrate
        # all_content_ids = []
        # for s in senses:
        #     # Hàm này bóc tách toàn bộ UUID có trong JSON contents
        #     all_content_ids.extend(flatten_ids(s.contents))
        
        # contents_queryset = AISenseContent.objects.filter(id__in=all_content_ids)
        
        # serialized_senses = serialize_senses(senses, contents_queryset, language_code, user_language_code)
        word_instance.processed_entries = serialize_entries(senses)

        word_data = AIWordSerializer(word_instance).data
        cache_manager.cache_word(language_code, word_instance.id, word_instance.value, word_data.senses)
        
        return word_data

    except Exception as e:
        
        cache_manager.cache_word_clear_specific(language_code, word_instance.value)
        word_instance.status = "FAILED"
        word_instance.save()
        raise
