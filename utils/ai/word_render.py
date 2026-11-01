import logging
import time
import traceback
import json
import redis
import asyncio
from google import genai
from google.genai import types
from rest_framework.renderers import JSONRenderer
from ai.serializers.AIWord import AIWordSerializer
from django.conf import settings
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
from utils.utils.extract_object_from_string import extract_json_fragment, extract_json_object
from utils.utils.flatten_id import flatten_ids
from utils.utils.kanji import get_ruby_generator
from utils.utils.sense_handle import serialize_entries, get_user_lang_content
from utils.utils.socket import socket_message, get_safe_room_id
from utils.redis.word_init import WordCacheManager
from utils.celery.translate import task_create_translate
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

from utils.ai.schema import word_schema, nonlatin_schema, complex_schema
from utils.ai.prompt import render_word_prompt, WORD_SYSTEM_PROMPT
from utils.celery.fetch_image import fetch_sense_image
from utils.ai.translate import ai_create_translate_sema
from utils.ai.metadata import ai_create_metadata
from utils.utils.normalize import normalize_lookup
from utils.utils.best_effort import best_effort
from ai.queries import load_word
from utils.ai import claude
from utils.ai.japanese import add_ruby
from utils.ai.refine import render_refine_prompt, request_refinement, refine_word

def get_schema(mode='latin'):
    if mode == "complex":
        return complex_schema
    if mode == "nonlatin":
        return nonlatin_schema
    # latin và các ngôn ngữ còn lại: schema phẳng (senses[]) mà bộ đọc stream bên dưới hiểu được
    return word_schema


def _set_word_status(word_id, status):
    AIWord.objects.filter(id=word_id).update(status=status, updated_at=timezone.now())


async def _close_word(word_id, word_value, language_code, socket_room, cache, status, cache_status, event, message, keep_old=False):
    """
    Kết thúc 1 từ không có kết quả dùng được (bị từ chối / lỗi): đánh dấu DB, cache và báo người đang chờ.
    cache_status NOT_FOUND / ERROR để các lần get-word sau trả ngay thay vì chờ mãi ở PROCESSING.
    keep_old: đang tạo lại 1 từ đã có nghĩa (replace_sense_ids) thì giữ bản cũ: từ về COMPLETED, bỏ cache để tra lại từ DB.
    """
    await sync_to_async(_set_word_status)(word_id, 'COMPLETED' if keep_old else status)
    try:
        if keep_old:
            cache.cache_word_clear_specific(language_code, word_value)
        else:
            cache.cache_word_set_status(language_code, word_value, cache_status)
    except Exception as e:
        logger.warning('Không đặt được trạng thái cache cho %s: %s', word_value, e)
    try:
        await socket_message(socket_room, {"type": event, "payload": message}, True)
    except Exception as e:
        logger.warning('Không gửi được socket %s cho %s: %s', event, word_value, e)


async def _metadata_then_image(props, sense, socket_room):
    """Metadata của 1 nghĩa rồi ảnh minh hoạ theo image_keywords vừa sinh (lỗi ảnh không ảnh hưởng metadata)."""
    await ai_create_metadata(props)
    await fetch_sense_image(sense, socket_room)


async def ai_create_new_word_sema(word_info):
    """
    Tạo 1 từ mới theo 2 bước AI:
      1. draft (Claude Haiku, AI_DRAFT_MODELS): stream từng nghĩa, mỗi nghĩa gửi socket ngay cho người tra đầu tiên,
         đồng thời dịch / sinh metadata cho nghĩa đó (Gemini flash-lite, GEMINI_API_MODEL).
      2. refine (Claude Haiku, AI_REFINE_MODELS; mọi bước đều tắt thinking): kiểm tra và chuẩn hoá cả bản draft (chạy song song với việc dịch của bước 1),
         bản đã chuẩn hoá mới được ghi vào DB và cache.
    """
    cache = WordCacheManager()

    word_id = word_info.get('word_id', None)
    word_value = normalize_lookup(word_info.get('value', ''))  # phải trùng chuỗi view dùng làm cache key / room
    language_code = word_info.get('language_code', None)
    # Tạo lại từ đã có: nghĩa cũ thôi là nghĩa chính thức khi bản mới lưu xong (bộ từ đang trỏ tới vẫn giữ nguyên)
    replace_ids = [str(i) for i in (word_info.get('replace_sense_ids') or [])]

    socket_room = get_safe_room_id(word_value, language_code)

    LATIN_LANGS = ['vi', 'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'sv', 'no', 'da', 'fi', 'tr', 'cs', 'hu', 'id']

    if language_code in LATIN_LANGS:
        mode = "latin"
    elif language_code == 'ja':
        mode = "complex"
    else:
        mode = "flat"

    current_schema = get_schema(mode)
    current_prompt = render_word_prompt(word_value, language_code)

    try:
        models = settings.AI_DRAFT_MODELS
        ai_trunks = []
        sense_objs = []
        valid = False
        rejected = False
        draft_model = None

        # Tập hợp quản lý toàn bộ task chạy ngầm (Socket, Ảnh, Dịch, Metadata)
        background_tasks = set()
        marks = {'start': time.monotonic()}

        attempts = 0
        while not ai_trunks and attempts < len(models):
            model = models[attempts]
            attempts += 1

            try:
                # Claude Haiku, tắt thinking để nghĩa đầu về nhanh nhất; schema đặt metadata trước senses nên biết sớm từ có bị từ chối không
                async with claude.client() as client, client.messages.stream(
                    model=model,
                    max_tokens=8192,
                    system=WORD_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": current_prompt}],
                    thinking={"type": "disabled"},
                    output_config=claude.output_config(current_schema),
                ) as response:
                    pointer = 0

                    async for text in response.text_stream:
                        if not text:
                            continue
                        ai_trunks.append(text)
                        full_text = "".join(ai_trunks)

                        if not valid:
                            word_meta_str = extract_json_object(full_text, "metadata")

                            if word_meta_str:
                                word_meta = json.loads(word_meta_str)
                                if not word_meta.get("should_be_saved", True):
                                    # Từ sai chính tả / vô nghĩa: dừng stream, không tốn thêm token
                                    rejected = True
                                    break
                                valid = True
                                draft_model = model

                        if not valid:
                            continue

                        while True:
                            sense_str, new_pointer = extract_json_fragment(full_text, "senses", pointer)
                            if not sense_str:
                                break
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
                                            {language_code: ex} for ex in (sense.get("examples") or [])[:2]  # schema Claude không có maxItems
                                        ],
                                    }
                                }
                                if language_code == 'ja':
                                    add_ruby(processed_contents["contents"], language_code)
                                sense_objs.append(processed_contents)
                                marks.setdefault('first_sense', time.monotonic())  # bản nháp chỉ đi qua socket cho người tra đầu tiên, không vào cache

                                # 1. Gửi socket PARTIAL ngầm: người tra đầu tiên thấy nghĩa ngay
                                s_task = asyncio.create_task(socket_message(socket_room, {"type": "PARTIAL_SENSE", "payload": {id: processed_contents}}))
                                background_tasks.add(s_task)
                                s_task.add_done_callback(background_tasks.discard)

                                # Ngôn ngữ cần dịch lấy từ cache (người tra sau vào thêm ngôn ngữ của họ)
                                word_data_tmp = best_effort(cache.cache_word_get_data, language_code, word_value, default={}) or {}
                                redis_user_language_code = word_data_tmp.get('langs', [language_code])

                                # 2. Dịch + metadata cho riêng nghĩa này (1 nghĩa/lần để gần như tức thì)
                                single_sense_list = [processed_contents]

                                # Ảnh cần image_keywords của metadata nên chạy nối tiếp ngay sau metadata (có PIXABAY_API_KEY mới chạy)
                                meta_task = asyncio.create_task(_metadata_then_image({
                                    "word_value": word_value,
                                    "language_code": language_code,
                                    "current_senses": single_sense_list
                                }, processed_contents, socket_room))
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
                                logger.warning('Lỗi đọc 1 nghĩa từ stream của %s: %s', word_value, e)
            except Exception as e:
                logger.warning('Tạo từ %s lỗi ở lần thử %s với %s: %s', word_value, attempts, model, e)
                if not rejected:
                    # Stream đứt giữa chừng: các nghĩa đã nhận chỉ là một phần của từ, không được lưu như từ hoàn chỉnh.
                    # Bỏ cả lượt (huỷ dịch / metadata đang chạy, báo app gỡ các nghĩa đã gửi) rồi thử model kế tiếp; hết model thì FAILED.
                    for task in list(background_tasks):
                        task.cancel()
                    background_tasks.clear()
                    if sense_objs:
                        try:
                            await socket_message(socket_room, {"type": "SENSE_REMOVED", "payload": [s['id'] for s in sense_objs]})
                        except Exception as socket_error:
                            logger.warning('Gửi socket SENSE_REMOVED lỗi: %s', socket_error)
                    ai_trunks, sense_objs, valid, draft_model = [], [], False, None

        marks['stream_end'] = time.monotonic()

        if rejected:
            await _close_word(word_id, word_value, language_code, socket_room, cache, 'REJECTED', 'NOT_FOUND', 'REJECTED', 'Word rejected', keep_old=bool(replace_ids))
            return {"id": word_id, "senses": [], "is_valid": False}

        if not valid or not sense_objs:
            await _close_word(word_id, word_value, language_code, socket_room, cache, 'FAILED', 'ERROR', 'ERROR', 'No usable AI response', keep_old=bool(replace_ids))
            return {"id": word_id, "senses": [], "is_valid": False, "error": "No usable AI response"}

        # --- BƯỚC 2: chuẩn hoá. Chỉ gửi nội dung ngôn ngữ gốc nên chạy song song được với các task dịch đang chạy ---
        refine_task = None
        if settings.AI_REFINE_ENABLED:
            best_effort(cache.cache_word_set_stage, language_code, word_value, 'refine')
            refine_task = asyncio.create_task(request_refinement(render_refine_prompt(word_value, language_code, sense_objs)))

        # Đợi các task chạy ngầm (socket, dịch, metadata) xong rồi mới đụng vào nội dung các nghĩa
        # Có trần thời gian: một lệnh gọi Gemini treo / 503 liên tục không được giữ người dùng chờ vô hạn.
        # Phần chưa xong bị huỷ, nghĩa vẫn được lưu và bản dịch thiếu sẽ được get-word dịch bù sau (redis_trans).
        if background_tasks:
            _, pending = await asyncio.wait(list(background_tasks), timeout=settings.AI_BACKGROUND_TIMEOUT)
            for task in pending:
                task.cancel()
            if pending:
                logger.warning('Tạo từ %s: %s tác vụ dịch / metadata quá %ss, bỏ qua để lưu phần đã có', word_value, len(pending), settings.AI_BACKGROUND_TIMEOUT)

        marks['background_done'] = time.monotonic()

        refinement = None
        if refine_task:
            try:
                refinement = await refine_task
            except Exception:
                logger.exception('Refine %s lỗi', word_value)
        marks['refine_response'] = time.monotonic()

        sense_objs = await refine_word(
            word_value, language_code, sense_objs, refinement,
            draft_model=draft_model, socket_room=socket_room, cache=cache,
        )
        marks['applied'] = time.monotonic()

        try:
            await sync_to_async(save_sense)(sense_objs, word_id, replace_ids)
            data = await sync_to_async(build_word_payload)(word_id)
            cache.cache_word(language_code=language_code, word_id=word_id, word_val=word_value, data=data.get('senses'))

            # Gửi socket FULL chốt hạ dữ liệu cuối cùng
            await socket_message(socket_room, {"type": "FULL_SENSE", "payload": data}, True)

        except Exception as e:
            traceback.print_exc()
            logger.error('Lỗi khi lưu DB / cache cho %s: %s', word_value, e)
            await _close_word(word_id, word_value, language_code, socket_room, cache, 'FAILED', 'ERROR', 'ERROR', str(e), keep_old=bool(replace_ids))
            raise

        marks['saved'] = time.monotonic()
        logger.info('Tạo từ %s [%s]: %s', word_value, language_code, ', '.join(f'{name} {value - marks["start"]:.1f}s' for name, value in marks.items() if name != 'start'))
        return {"id": word_id, "senses": sense_objs, "is_valid": True}

    except Exception as e:
        traceback.print_exc()
        logger.error('Lỗi xử lý từ %s: %s', word_value, e)
        # Chỉ đánh dấu FAILED khi chưa lưu xong (nếu đã COMPLETED thì lỗi nằm ở bước báo cuối)
        # Đang tạo lại từ đã có nghĩa thì giữ COMPLETED với nghĩa cũ (FAILED sẽ khiến get-word tạo thêm bản nữa)
        await sync_to_async(AIWord.objects.filter(id=word_id).exclude(status='COMPLETED').update)(
            status='COMPLETED' if replace_ids else 'FAILED', updated_at=timezone.now())
        raise


def build_word_payload(word_id):
    """Dữ liệu 1 từ như API trả về (nghĩa kèm metadata, đã prefetch nên không phát sinh thêm query)."""
    return AIWordSerializer(load_word(id=word_id)).data


def save_sense(sense_obj, word_id, replace_ids=()):
    """
    Ghi các nghĩa (đã chuẩn hoá) và metadata của chúng trong 1 transaction, rồi đánh dấu từ COMPLETED.
    replace_ids: nghĩa của lần tạo trước, thôi là nghĩa chính thức (không xoá: bộ từ đang trỏ tới).
    Nghĩa thiếu metadata (bước metadata lỗi) vẫn được lưu, phần thiếu sẽ do app / lần tra sau bổ sung.
    """
    metadata_instances = []
    sense_instances = []
    for s in sense_obj:
        metadata = s.pop('metadata', None)
        if metadata:
            metadata_instances.append(AISenseMetadata(**metadata))
            s['metadata_id'] = metadata.get('id')
        sense_instances.append(AISense(**s))

    with transaction.atomic():
        AISenseMetadata.objects.bulk_create(metadata_instances)
        AISense.objects.bulk_create(sense_instances)
        if replace_ids:
            AISense.objects.filter(id__in=list(replace_ids), word_id=word_id).update(is_official=False, updated_at=timezone.now())
        AIWord.objects.filter(id=word_id).update(status='COMPLETED', updated_at=timezone.now())

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
