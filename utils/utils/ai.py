import asyncio
import json
import uuid
from venv import logger
from google import genai
from django.http.response import StreamingHttpResponse
from google.genai import types
from numpy import require
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from django.utils import timezone, translation
from rest_framework.decorators import api_view, permission_classes, authentication_classes,action
from ai import models
from ai.serializers.AISenseMetadata import AISenseMetadataSerializer
from ai.serializers.AIWord import AIWordSerializer
from core.models.Language import Language
from core.models.Word import Word
from flashcardApi import settings
from django.db import transaction

from django.contrib.auth.models import User
from ai.models.AIWord import AIWord
from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.models.AISenseContent import AISenseContent
from utils.celery.fetch_image import task_fetch_image_single
from utils.helper.sense_context import SenseContext
from utils.utils import uuidv7
from utils.utils.extract_object_from_string import extract_json_fragment
from utils.utils.flatten_id import flatten_ids
from utils.utils.kanji import get_ruby_generator
from utils.utils.limit_prefetch import limit_prefetch
from utils.utils.sense_handle import serialize_entries, serialize_senses
from utils.utils.socket import socket_message

# The client gets the API key from the environment variable `GEMINI_API_KEY`
client = genai.Client(api_key=settings.GEMINI_API_KEY)

# @api_view(["GET"])
# @permission_classes([])
# @authentication_classes([])
from asgiref.sync import sync_to_async

# @api_view(["GET"])
# @permission_classes([])
# @authentication_classes([])
async def full_data(data, callback=None):
    yield data
    # await asyncio.sleep(0.01) # Nhả for, giúp event stream gửi dữ liệu ngay lập tức
    if callback:
        callback()

# def ai_create_translate(user, content, language, user_language, socket_room): 

async def ai_create_new_word(user, word_instance, language_code, user_language_code, socket_room):
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
    current_prompt = get_prompt(mode, word_instance.value, language_code, user_language_code)

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
            async for chunk in response:
                if chunk.text:
                    ai_trunks.append(chunk.text)

                    # --- LOGIC ĐÁNH CHẶN LẤY ẢNH SỚM ---
                    full_text = "".join(ai_trunks)
                    sense_str, new_pointer = extract_json_fragment(full_text, "senses", pointer)
                    
                    if sense_str:
                        pointer = new_pointer
                        try:
                            sense = json.loads(sense_str)
                            id = str(uuidv7.generate_uuid7())

                            sense_obj = {
                                "id": id,
                                "word_id": str(word_instance.id),
                                "word_value":word_instance.value,
                                "language_code": language_code,
                                "created_by_id": user.id,
                            }
                               
                            sense["id"] = id

                            sense_objs.append(sense_obj)

                            sense_index += 1

                            await socket_message(socket_room, {"type": "PARTIAL_SENSE", "payload": sense})
                            
                            # Kích hoạt lấy ảnh 1 ngay lập tức (không đợi stream xong)
                            img_desc = sense.get('metadata',{}).get('image_describe',None)
                            if img_desc:
                               task_fetch_image_single.delay(sense_obj, img_desc, socket_room, temp_index=0)
                        except Exception as e: 
                            pass
            
        full_response_text = "".join(ai_trunks)
        data = json.loads(full_response_text)
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
        

def render_all_word_data(user, word_instance, language_code, user_language_code, group):
     # 1. Phân loại ngôn ngữ
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
    current_prompt = get_prompt(mode, word_instance.value, language_code, user_language_code)

    async def generator():
        response = client.models.generate_content_stream(
            # model="gemma-3-1b", 
            model="gemini-2.5-flash-lite",
            contents=current_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=8192, # Tăng từ 2048 lên 8192
                response_mime_type="application/json",
                response_schema=current_schema
            )
        )

        is_first_sense_found = False
        full_response_text = ""
        for chunk in response:
            if chunk.text:
                full_response_text += chunk.text

                # Check first sense and flag completed or not. If yes, return via socket
                if not is_first_sense_found:
                    first_sense_string = extract_json_fragment(full_response_text, "senses", None)
                    if(first_sense_string):
                        is_first_sense_found = True
                        first_sense = json.loads(first_sense_string)
                        metadata = first_sense['metadata']
                        if(metadata):
                            is_valid = metadata.get("valid", False)
                            should_be_save = metadata.get("should_be_save", False)

                            # handle error case

                        socket_message(group, first_sense)


                yield chunk.text
                await asyncio.sleep(0.001) # Nhả for, giúp event stream gửi dữ liệu ngay lập tức


        
        # Bạn có thể dùng sync_to_async nếu hàm lưu DB là đồng bộ
        data = json.loads(full_response_text)
        socket_message(group, data, True)
        await saveword(user, word_instance, language_code, user_language_code, data) 

    generator()

def render_schema(missing_content: dict, need_translation: dict):
    properties = {}
    required = []


    for sense_id, contents in missing_content.items():
        # required.append(sense_id) # Tùy chọn: có bắt buộc sense_id này phải có trong response không

        # Tạo object chứa các content_id
        content_properties = {}
        content_required = []
        
        for item in contents:
            c_id = item['id']
            content_required.append(c_id)
            content_properties[c_id] = {
                        "type": "STRING",
                        "description": f"Translated text for content {c_id}"
            }

        need_trans = need_translation.get(sense_id, None)
        if need_trans:
            del need_translation[sense_id]
            content_properties["translations"] = {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "description": f"translate word to user language{f', suitable with definition: {need_trans}'}"
                },
                "maxItems": 4
            }
            content_required.append("translations")

        properties[sense_id] = {
            "type": "OBJECT",
            "properties": content_properties,
            "required": content_required
        }
        required.append(sense_id)

    

    for sense_id, definition in need_translation.items():
        if not definition:
            continue
        content_properties = {}
        content_required = []

        content_properties["translations"] = {
            "type": "ARRAY",
            "items": {
                "type": "STRING",
                "description": f"translate word to user language{f', suitable with definition: {definition}'}"
            },
            "maxItems": 4
        }
        content_required.append("translations")

        properties[sense_id] = {
            "type": "OBJECT",
            "properties": content_properties,
            "required": content_required
        }
        required.append(sense_id)


    return {
        "type": "OBJECT",
        "properties": properties,
        "required": required
    }

async def render_translate(
    user,
    translate_instance,
    word,
    sense_instances,
    missing_content,
    need_translation,
    language_code,
    user_language_code,
    socket_room
):
    prompt = f"""
    # ROLE: Translator
    # WORD: {word}
    # INPUT LANGUAGE: {language_code}
    # TARGET LANGUAGE: {user_language_code}
    # TRANSLATE CONTENTS:
    {json.dumps(missing_content, ensure_ascii=False)}

    # TASK:
    Translate the dictionary contents from {language_code} to {user_language_code}.

    # OUTPUT RULE:
    - Output JSON only
    - Only return "translate"
    - DO NOT repeat original text
    """


    schema = render_schema(missing_content, need_translation)


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


        all_contents_map ={}
        for sense_id, contents in missing_content.items():
            for item in contents:
                all_contents_map[item['id']] = item

        translate_data = await sync_to_async(save_translate)(
            user,
            translate_instance,
            user_language_code,
            sense_instances,
            data
        )

        await socket_message(
            socket_room,
            {
                "type": "TRANSLATE_SENSE_SUCCESS",
                "payload": translate_data
            },
            True
        )

    except Exception as e:
        try:
            def update_failed_status():
                translate_instance.status = 'FAILED'
                translate_instance.save()
            
            await sync_to_async(update_failed_status)()
            await socket_message(socket_room, {"type": "TRANSLATE_SENSE_ERROR", "payload": str(e)})
        except Exception as socket_error:
            # If socket message fails, just log it
            print('render_translate error')

def get_schema(mode):
    if mode == "latin":
        return word_schema
    elif mode == "nonlatin":
        return nonlatin_schema
    elif mode == "complex":
        return complex_schema

def get_prompt(mode, word, language_code, user_language_code):
    language_name = language_map.get(language_code, 'en')
    user_language_name = language_map.get(user_language_code, 'en')

    # if mode == "simple":
    #     return get_word_prompt(word, language, user_language)
    # elif mode == "latin":
    #     return get_latin_prompt(word, language, user_language)
    # elif mode == "complex":
    return f"""
    # INPUTS:
    - "word": {word}
    - "WORD_LANGUAGE": {language_name}
    - "USER_LANGUAGE": {user_language_name}

    # ROLE: You are an expert multilingual lexicographer. 
    # TASK: Analyze the {language_name} EXACT word '{word}' for a learner's dictionary.

    # LANGUAGE RULES:
    1. "definition.text", "usage.text", "examples.text" MUST be written in {language_name}. 
    2. "definitionTranslated", "usageTranslate", "translate", "translations" fields: Must be in {user_language_name}.
    3. "ipas": 
    - For Latin languages: Use Standard IPA (e.g., US/UK).
    - For non-Latin (Japanese, Chinese, Korean): Provide ROMAN (Romaji/Pinyin) and phonetic script.
    4. "pos":
    - MUST be in English

    # CONTENT QUALITY:
    - All information MUST be correct.
    - NEVER use other word have similar sound, words to show instead.
    - NEVER anser with content that you not sure.
    - The definition and examples must use vocabulary at the same level as the word's level
    
    - FOLLOW RESTRICLY LANGUAGE RULES.
    - Sense order by frequency.
    - Accuracy: Do not hallucinate antonyms/synonyms. Use null for "audio" if unknown.
    - Image Prompt: "image_describe" is list of tags for image prompt.
    - should_be_saved: This is a dictionary entry. Therefore, this field is TRUE only for single-meaning phrases, not combinations of different words. These are words or phrases that actually carry meaning, not variations, allusions, or rhetorical devices created by other words. 
    - I am paying for this service, please provide full detail for every field

    # FORMATTING:
    - Strictly adhere to the provided JSON schema. 
    - No markdown formatting in the output, just raw JSON.

    # GROUPING RULE:
    - All senses with the same part of speech MUST be grouped into a single entry.
    - Do NOT create multiple entries with the same POS.

    # OUTPUT:
    Synonyms/antonyms/relateds/tags: each item MUST be unique in the list, no more than 5 items per field.
    """

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
                if not metadata_raw.get("should_be_saved"):
                    continue

                clean_data = {k: v for k, v in metadata_raw.items() if k in model_fields}
                metadata = AISenseMetadata(**clean_data, created_by=user)
                
                metadata_to_create.append(metadata)
                # Lưu trữ sense_raw để xử lý content ở phase sau
                contexts.append(SenseContext(raw=sense_raw, pos=metadata_raw.get("pos", None), metadata=metadata))

            if not contexts:
                word_instance.status = "REJECTED"
                word_instance.save()
                return

            AISenseMetadata.objects.bulk_create(metadata_to_create)

            # =========================
            # PHASE 2: GENERATE ALL CONTENTS (FLAT LIST)
            # =========================
            ruby_gen = get_ruby_generator()
            all_contents_to_create = []

            def prepare_content_obj(val, lang):
                if not val: return None
                text = val.get("text") if isinstance(val, dict) else val
                if not text: return None

                ruby = None
                if lang == "ja" and isinstance(val, dict):
                    try: ruby = ruby_gen.generate(text)
                    except: pass

                return AISenseContent(
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
                ctx.obj_map["def_orig"] = prepare_content_obj(s.get("definition"), language_code)
                ctx.obj_map["def_trans"] = prepare_content_obj(s.get("definition", {}).get("translate"), user_language_code)
                
                ctx.obj_map["usage_orig"] = prepare_content_obj(s.get("usage"), language_code)
                ctx.obj_map["usage_trans"] = prepare_content_obj(s.get("usage", {}).get("translate"), user_language_code)
                
                ctx.obj_map["extra_trans"] = prepare_content_obj(s.get("translations"), user_language_code)

                # Gom vào list tổng để bulk create
                all_contents_to_create.extend([obj for obj in ctx.obj_map.values() if obj])

                # 2. Ví dụ (Mỗi ví dụ là một cặp Orig-Trans)
                for ex_raw in s.get("examples", []):
                    ex_orig = prepare_content_obj(ex_raw, language_code)
                    ex_trans = prepare_content_obj(ex_raw.get("translate"), user_language_code)
                    
                    if ex_orig:
                        ctx.example_objs.append({"orig": ex_orig, "trans": ex_trans})
                        all_contents_to_create.append(ex_orig)
                        if ex_trans:
                            all_contents_to_create.append(ex_trans)

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
                        language_code: get_id("def_orig"),
                        user_language_code: get_id("def_trans")
                    },
                    "usage": {
                        language_code: get_id("usage_orig"),
                        user_language_code: get_id("usage_trans")
                    },
                    "translations": {
                        user_language_code: get_id("extra_trans")
                    },
                    "examples": [
                        {
                            language_code: str(pair["orig"].id),
                            user_language_code: str(pair["trans"].id) if pair["trans"] else None
                        }
                        for pair in ctx.example_objs
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
                    )
                )

            AISense.objects.bulk_create(senses_to_create)

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

word_schema = {
    "type": "OBJECT",
    "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },
                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "metadata":{
                                    "type":"OBJECT",
                                    "properties":{
                                        "is_valid":{"type":"BOOLEAN","description": "Word or phrase is valid or not"},
                                        "is_offensive":{"type":"BOOLEAN"},
                                        "pos":{"type":"STRING"},
                                        "should_be_saved": {"type":"BOOLEAN","description":"Only True if word is widely known in language and write in correct form"},
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "ipas": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "text": { "type": "STRING" },
                                                    "label": {
                                                        "type": "STRING",
                                                        "description": "US, UK, ROMAN, etc."
                                                    },
                                                },
                                                "required": ["text", "label"]
                                            }
                                        },
                                        "synonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "antonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "relateds": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "forms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" },
                                            "description": "Word forms (plural, past tense, etc.)"
                                        },
                                        "tags":{
                                            "type":"ARRAY",
                                            "items": {"type":"STRING"},
                                            "description":"additional tags for the sense, for searching, grouping, etc."
                                        },
                                        "image_describe": {
                                            "type": "STRING",
                                            "description": "1-5 keywords for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        }
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_describe", "level","pos"]
                                },
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                        },

                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                    },
                                    "required": ["text","translate"]
                                },
                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { 
                                            "type": "STRING",
                                            "description": "Technical usage: collocations, specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                    },
                                    "required": ["text", "translate"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "text": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                        },
                                        "required": ["text", "translate"]
                                    },
                                    "maxItems": 2,
                                },
                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples",
                                "translations"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING" }, 
    },
    "required": ["word", "entries"]
}

nonlatin_schema = {
    "type": "OBJECT",
    "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },

                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                 "metadata":{
                                    "type":"OBJECT",
                                    "properties":{
                                        "is_valid":{"type":"BOOLEAN","description": "Word or phrase is valid or not"},
                                        "is_offensive":{"type":"BOOLEAN"},
                                        "is_compound": {"type":"BOOLEAN"},
                                        "should_be_saved": {"type":"BOOLEAN","description":"Only True if word is widely known in language and write in correct form"},
                                        "is_correct_language": {"type":"BOOLEAN","description":"is the word in the correct language"},
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "pos":{"type":"STRING"},
                                        "ipas": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "text": { "type": "STRING" },
                                                    "label": {
                                                        "type": "STRING",
                                                        "description": "US, UK, ROMAN, etc."
                                                    },
                                                    "roman": { "type": "STRING" },
                                                },
                                                "required": ["text", "label"]
                                            }
                                        },
                                        "synonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "antonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "relateds": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "forms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" },
                                            "description": "Word forms (plural, past tense, etc.)"
                                        },
                                        "tags":{
                                            "type":"ARRAY",
                                            "items": {"type":"STRING"},
                                            "description":"additional tags for the sense, for searching, grouping, etc."
                                        },
                                        "image_describe": {
                                            "type": "STRING",
                                            "description": "1-5 keywords for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        },
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_describe", "level", "pos"]
                                },
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                },

                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                        "roman": { "type": "STRING" },
                                    },
                                    "required": ["text","translate"]
                                },

                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { 
                                            "type": "STRING",
                                            "description": "Technical usage: collocations, specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                        "roman": { "type": "STRING" },
                                    },
                                    "required": ["text", "translate"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "text": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                        },
                                        "required": ["text", "translate"]
                                    },
                                    "description": "1 Example only"
                                },
                                
                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING"}
    },
    "required": ["word", "entries"]
}

complex_schema = {
    "type": "OBJECT",
    "properties": {
        "entries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "pos": {
                        "type": "STRING",
                        "description": "Part of speech (noun, verb, adjective, etc.)"
                    },

                    "senses": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                 "metadata":{
                                     "type":"OBJECT",
                                     "properties":{
                                        "is_valid":{"type":"BOOLEAN","description": "Word or phrase is valid or not"},
                                        "is_offensive":{"type":"BOOLEAN"},
                                        "is_compound": {"type":"BOOLEAN"},
                                        "should_be_saved": {"type":"BOOLEAN","description":"Only True if word is widely known in language and write in correct form"},
                                        "is_correct_language": {"type":"BOOLEAN","description":"is the word in the correct language"},
                                        "register":{"type":"STRING", "description": "formal, informal, slang, vulgar, technical, etc."},
                                        "pos":{"type":"STRING"},
                                        "ipas": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "text": { "type": "STRING" },
                                                    "label": {
                                                        "type": "STRING",
                                                        "description": "US, UK, ROMAN, etc."
                                                    },
                                                    "roman": { "type": "STRING" },
                                                },
                                                "required": ["text", "label"]
                                            }
                                        },
                                        "synonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "antonyms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "relateds": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" }
                                        },

                                        "forms": {
                                            "type": "ARRAY",
                                            "items": { "type": "STRING" },
                                            "description": "Word forms (plural, past tense, etc.)"
                                        },
                                        "tags":{
                                            "type":"ARRAY",
                                            "items": {"type":"STRING"},
                                            "description":"additional tags for the sense, for searching, grouping, etc."
                                        },
                                        "image_describe": {
                                            "type": "STRING",
                                            "description": "1-5 keywords for stock image search"
                                        },
                                        "level": {
                                            "type": "STRING",
                                            "description": "A1–C2, N1–N5, TOPIC1, etc."
                                        },
                                    },
                                    "required": ["should_be_saved","is_valid","ipas", "tags","image_describe", "level", "pos"]
                                },
                                "translations": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING"
                                            }
                                        },

                                "definition": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { "type": "STRING" },
                                        "translate": { "type": "STRING", "description": "Definition in user language" },
                                        "roman": { "type": "STRING" },
                                    },
                                    "required": ["text","translate", "roman"],
                                },
                                "usage": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": { 
                                            "type": "STRING",
                                            "description": "Technical usage: collocations, specific prepositions, grammatical patterns, or social register (formal/informal). Example: 'Often used with the particle NI' or 'Commonly used in business contexts'."
                                        },
                                        "translate": { "type": "STRING", "description": "Usage translated to user language" },
                                        "roman": { "type": "STRING" },
                                    },
                                    "required": ["text", "translate", "roman"],
                                },

                                "examples": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "text": { "type": "STRING" },
                                            "translate": {
                                                "type": "STRING",
                                                "description": "Example translated to user language in sense context"
                                            },
                                            "roman": { "type": "STRING" },
                                        },
                                        "required": ["text", "translate", "roman"],
                                    },
                                    "description": "1 Example only"
                                },

                            },

                            "required": [
                                "definition",
                                "usage",
                                "examples"
                            ]
                        }
                    }
                },

                "required": ["pos", "senses"]
            }
        },
        "word": { "type": "STRING"}
    },

    "required": ["word", "entries"]
}


language_map={
    "en":"English",
    "es":"Spanish",
    "fr":"French",
    "de":"German",
    "ja":"Japanese",
    "zh":"Chinese",
    "ko":"Korean",
    "ru":"Russian",
    "it":"Italian",
    "pt":"Portuguese",
    "ar":"Arabic",
    "nl":"Dutch",
    "pl":"Polish",
    "sv":"Swedish",
    "no":"Norwegian",
    "da":"Danish",
    "fi":"Finnish",
    "el":"Greek",
    "tr":"Turkish",
    "cs":"Czech",
    "hu":"Hungarian",
    "vi":"Vietnamese",
    "th":"Thai",
    "hi":"Hindi",
    "id":"Indonesian",
    "he":"Hebrew",
    }

