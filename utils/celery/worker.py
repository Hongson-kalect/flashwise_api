# 1. Thiết lập Django
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'flashcardApi.settings')
django.setup()
import asyncio
import json
import logging
import socket
from django.utils import timezone
from asgiref.sync import sync_to_async

from collections import defaultdict
from django.db import connection, transaction
from django.apps import apps # Tuyệt chiêu đọc Model động của Django
from django.db.models import Prefetch, Case, When, F, Value, Func, ExpressionWrapper, IntegerField, Max
from django.contrib.postgres.fields import ArrayField
from django.db.models import CharField

from ai.models import AIWord, AISense
from core.models import Collection
from ai.serializers import AIWordSerializer
from utils.redis.word_init import WordCacheManager

from utils.ai.word_render import ai_create_new_word_sema
from utils.ai.translate import ai_create_translate_sema
from utils.celery.fetch_image import get_image_by_keyword
from utils.utils.sense_handle import merge_translations
from ai.queries import load_word
from django.conf import settings
from utils.ai.rate_limit import COOLDOWN_KEY

# --- CẤU HÌNH ---
REDIS_URL = 'redis://redis:6379/0'
WORD_SEMA_LIMIT = settings.WORD_SEMA_LIMIT
COLLECTION_WORD_SEMA_LIMIT = settings.COLLECTION_WORD_SEMA_LIMIT
IMAGE_SEMA_LIMIT = 10
TRANS_SEMA_LIMIT = settings.TRANS_SEMA_LIMIT

# Hàng đợi tin cậy: job lấy ra được chuyển sang list "đang làm" riêng của worker (BLMOVE), làm xong mới xoá.
# Worker chết thì heartbeat hết hạn, worker còn sống trả các job đang làm dở về đầu hàng đợi (tối đa JOB_MAX_REQUEUES lần).
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
HEARTBEAT_KEY = "worker:alive:{}"
HEARTBEAT_SECONDS = 30
REAP_INTERVAL = 30
# Hàng đợi gọi Claude: đang bị giới hạn (ai_cooldown:anthropic) thì tạm không nhận job mới
QUEUE_PROVIDER = {"redis_word": "anthropic", "redis_collection_word": "anthropic"}
TERMINAL_WORD_STATUS = ('COMPLETED', 'REJECTED', 'BANNED')
BATCH_SIZE = 500
FLUSH_INTERVAL = 3 
# Cấu hình danh sách các hàng đợi mà Flusher cần quét qua
BUFFER_CONFIGS = [
    {"key": "db_buffer:sense:update", "model_name": "AISense", "action": "bulk_update", "fields": ["contents"], "app":"ai"},
    {"key": "db_buffer:word:update", "model_name": "AIWord", "action": "bulk_update", "fields": ["text", "updated_at"], "app":"ai"},
    {"key": "db_buffer:collection:atoms_update", "model_name": "Collection", "action": "atoms_update", "fields": [], "app":"core"},
    {"key": "db_buffer:collectionItem:create", "model_name": "CollectionItem", "action": "collection_items_create", "fields": [], "app":"core"},
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- TẦNG GIAO TIẾP DB TRÚT BUFFER ---
def save_to_postgres_sync(batch_data, task_type):
    """Thực thi ghi DB theo mẻ (Bulk Operations)"""
    logger.info(f"[DB-SAVE] {task_type.upper()} | Trút thành công mẻ gồm: {len(batch_data)} records vào Postgres.")
    # Thực tế: Model.objects.bulk_create(...) hoặc bulk_update(...)

# --- TẦNG XỬ LÝ LOGIC WORKER ---
def processing_key(queue, worker_id=WORKER_ID):
    return f"{queue}:processing:{worker_id}"


def word_already_done(job):
    """Job tạo từ mà từ đã xong (job bị đẩy trùng / được trả về sau khi worker chết): bỏ qua, trừ khi đang tạo lại."""
    if job.get('replace_sense_ids') or not job.get('word_id'):
        return False
    return AIWord.objects.filter(id=job['word_id'], status__in=TERMINAL_WORD_STATUS).exists()


async def handle_task(job, sema, task_type, raw=None, queue=None):
    """Xử lý core logic task, đảm bảo giải phóng semaphore và xác nhận job (xoá khỏi list đang làm) ở khối finally"""
    time_start = timezone.now()
    try:
        if task_type == 'word':
            if await sync_to_async(word_already_done)(job):
                logger.info(f"[WORD] Bỏ qua job {job.get('value')}: từ đã xong")
                return
            await ai_create_new_word_sema(job)
        elif task_type == 'image':
            await get_image_by_keyword(job)
        elif task_type == 'translate':
            # Dịch phần còn thiếu cho 1 từ đã có (user chọn ngôn ngữ mới). Ghi thẳng DB rồi mới nạp lại cache: nếu đi qua
            # buffer 3 giây thì cache bị nạp lại từ dữ liệu cũ và user vẫn thấy thiếu bản dịch.
            word_id = job.get('word_id')
            try:
                updated_contents = await ai_create_translate_sema(job, False)
                if updated_contents:
                    await sync_to_async(save_translated_contents)(list(updated_contents), job.get('language_code'))
                if word_id:
                    await sync_to_async(re_cache_word)(word_id)
            finally:
                # Mở khoá để lần tra sau đẩy lại được nếu bản dịch chưa đủ (việc nền giữ khoá cả ngày, không mở)
                user_language = job.get('user_language_code')
                if word_id and isinstance(user_language, str) and not job.get('lazy'):
                    await redis_client.delete(f"trans_lock:{word_id}:{job.get('language_code')}:{user_language}")

        elif task_type == 'collection_word':
            # Tạo word mới
            results = await ai_create_new_word_sema(job)
            word = job.get('value', None)

            # Từ result có {id, senses:id[]}
            # Lấy list collection trong redis word key
            word_key = f'collection_id:{word}'
            senses = results.get('senses')

            # Kiểm tra nếu senses tồn tại và có ít nhất 1 phần tử
            selected_sense = senses[0] if senses else None

            if not selected_sense:
                return

            action = 'accept' if results.get('is_valid', False) else 'reject'
            
            collection_ids = await redis_client.spop(word_key, count=9999)
            for collection_id in collection_ids:
                await redis_client.rpush("db_buffer:collection:atoms_update", json.dumps({"collection_id": collection_id, "value": word, "sense_id": selected_sense.get("id"), 'action':action}))
                if action == 'accept':
                    await redis_client.rpush("db_buffer:collectionItem:create", json.dumps({"collection_id": collection_id, "value": word, "sense_id": selected_sense.get("id")}))

        logger.info(f"[{task_type.upper()}] Job xử lý thành công trong {round((timezone.now() - time_start).total_seconds(), 2)}s")
    except Exception as e:
        logger.error(f"[AI-ERROR] {task_type} gặp lỗi nghiêm trọng: {e}", exc_info=True)
    finally:
        # CHÌA KHÓA VÀNG: Trả lại ghế trống cho consumer nhặt việc tiếp
        sema.release()
        if raw is not None and queue:
            try:
                await redis_client.lrem(processing_key(queue), 1, raw)
            except Exception as e:
                logger.warning(f"[ACK] Không xoá được job khỏi list đang làm {queue}: {e}")

def save_translated_contents(senses, source_language):
    """
    Ghi bản dịch vừa tạo vào AISense. Gộp vào contents đang có trong DB (khoá dòng) thay vì ghi đè,
    để 2 job dịch 2 ngôn ngữ chạy gần nhau không làm mất bản dịch của nhau.
    """
    ids = [s['id'] for s in senses if s.get('contents')]
    now = timezone.now()
    with transaction.atomic():
        rows = {str(row.id): row for row in AISense.objects.select_for_update().filter(id__in=ids)}
        updated = []
        delta_rows = []
        for sense in senses:
            row = rows.get(str(sense['id']))
            if row is None or not sense.get('contents'):
                continue
            if sense.get('target') == 'delta':
                # Nghĩa riêng dạng delta (ai/translation.py): bản dịch của phần user sửa ghi vào delta của chính nghĩa đó
                row.delta = merge_translations(row.delta, sense['contents'], source_language)
                delta_rows.append(row)
            else:
                row.contents = merge_translations(row.contents, sense['contents'], source_language)
            row.updated_at = now
            updated.append(row)
        if updated:
            AISense.objects.bulk_update([r for r in updated if r not in delta_rows], ['contents', 'updated_at'])
            if delta_rows:
                AISense.objects.bulk_update(delta_rows, ['delta', 'updated_at'])
    return len(updated)


def re_cache_word(word_ids):
    """Nạp lại cache các từ từ DB (bản đầy đủ bản dịch mới nhất)."""
    ids = [word_ids] if isinstance(word_ids, str) else list(word_ids)
    logger.info(f"Re-cache word: {ids}")
    cache_manager = WordCacheManager()

    for word_id in filter(None, ids):
        word_instance = load_word(id=word_id)
        if word_instance is None:
            continue
        data = AIWordSerializer(word_instance).data
        cache_manager.cache_word(word_instance.language_code, word_instance.id, word_instance.value, data.get('senses'), word_fields=data)
        logger.info(f"Re-cache word completed: {word_instance.value}")


async def consume_queue(queue_name, sema, task_type):
    """Consumer thông minh: Chỉ bốc máy nhặt việc từ Redis khi thực sự CÒN GHẾ TRỐNG"""
    logger.info(f"[START] Khởi động hàng chờ Consumer: {queue_name}")
    while True:
        try:
            # Chặn tại đây, nếu hết ghế trống tiến trình sẽ dừng lại không spam Redis
            await sema.acquire()

            # Nhà cung cấp AI đang báo hết hạn mức: chờ hết thời gian nghỉ rồi mới nhận job mới
            provider = QUEUE_PROVIDER.get(queue_name)
            if provider:
                wait_ms = await redis_client.pttl(COOLDOWN_KEY.format(provider))
                if wait_ms and wait_ms > 0:
                    sema.release()
                    logger.info(f"[COOLDOWN] {provider} đang bị giới hạn, {queue_name} tạm dừng {wait_ms / 1000:.1f}s")
                    await asyncio.sleep(min(wait_ms / 1000, 30))
                    continue

            # Nhặt việc sang list đang làm của worker này (làm xong mới xoá), timeout chống treo
            raw = await redis_client.blmove(queue_name, processing_key(queue_name), 5, "LEFT", "RIGHT")
            if not raw:
                sema.release() # Trả lại ghế nếu hàng chờ trống
                continue

            logger.info(f"[CONSUMER-LOOP] Chạy vòng lặp {queue_name}")

            try:
                job = json.loads(raw)
            except ValueError:
                logger.error(f"[BAD-JOB] {queue_name}: job không phải JSON, bỏ: {raw[:200]}")
                await redis_client.lrem(processing_key(queue_name), 1, raw)
                sema.release()
                continue

            # Tạo task chạy ngầm độc lập
            asyncio.create_task(handle_task(job, sema, task_type, raw, queue_name))

        except Exception as e:
            logger.error(f"[LOOP-ERROR] Lỗi vòng lặp Consumer {queue_name}: {e}")
            await asyncio.sleep(1)

LAZY_QUEUE = "redis_trans_lazy"
URGENT_QUEUES = ("redis_word", "redis_collection_word", "redis_image", "redis_trans")


def lazy_should_wait(urgent_lengths, busy_semas):
    """Việc nền chỉ chạy khi mọi hàng đợi gấp rỗng và không việc gấp nào đang chạy (sema nào cũng còn đủ ghế)."""
    return any(length > 0 for length in urgent_lengths) or any(sema.locked() or sema._value < limit for sema, limit in busy_semas)


async def consume_lazy(lazy_sema, busy_semas):
    """
    Dịch nền: mỗi lần 1 việc từ redis_trans_lazy, chỉ khi hệ thống rảnh, cách nhau LAZY_TRANS_INTERVAL giây
    (giới hạn tốc độ để không ảnh hưởng việc gấp). Cùng dạng job với redis_trans nên dùng chung handle_task('translate').
    """
    logger.info(f"[START] Khởi động hàng chờ dịch nền: {LAZY_QUEUE}")
    while True:
        try:
            lengths = [await redis_client.llen(queue) for queue in URGENT_QUEUES]
            if lazy_should_wait(lengths, busy_semas):
                await asyncio.sleep(5)
                continue
            await lazy_sema.acquire()
            raw = await redis_client.blmove(LAZY_QUEUE, processing_key(LAZY_QUEUE), 5, "LEFT", "RIGHT")
            if not raw:
                lazy_sema.release()
                continue
            try:
                job = json.loads(raw)
            except ValueError:
                logger.error(f"[BAD-JOB] {LAZY_QUEUE}: job không phải JSON, bỏ: {raw[:200]}")
                await redis_client.lrem(processing_key(LAZY_QUEUE), 1, raw)
                lazy_sema.release()
                continue
            await handle_task(job, lazy_sema, "translate", raw, LAZY_QUEUE)  # tự trả ghế ở finally
            await asyncio.sleep(settings.LAZY_TRANS_INTERVAL)
        except Exception as e:
            logger.error(f"[LOOP-ERROR] Lỗi vòng lặp dịch nền: {e}")
            await asyncio.sleep(5)


async def heartbeat():
    """Báo worker còn sống; hết hạn (worker chết / bị tắt) thì worker khác thu hồi job đang làm dở."""
    while True:
        try:
            await redis_client.set(HEARTBEAT_KEY.format(WORKER_ID), 1, ex=HEARTBEAT_SECONDS)
        except Exception as e:
            logger.warning(f"[HEARTBEAT] lỗi: {e}")
        await asyncio.sleep(HEARTBEAT_SECONDS / 3)


async def requeue_orphans(client=None):
    """
    Trả job đang làm dở của worker đã chết về đầu hàng đợi. Mỗi lần trả tăng _requeued; quá JOB_MAX_REQUEUES thì bỏ
    (job có thể chính là thứ làm sập worker). Trả số job đã trả về.
    """
    client = client or redis_client
    moved = 0
    async for key in client.scan_iter(match="*:processing:*"):
        queue, worker_id = key.split(":processing:", 1)
        if worker_id == WORKER_ID or await client.exists(HEARTBEAT_KEY.format(worker_id)):
            continue
        while (raw := await client.rpop(key)) is not None:
            try:
                job = json.loads(raw)
            except ValueError:
                continue
            job["_requeued"] = job.get("_requeued", 0) + 1
            if job["_requeued"] > settings.JOB_MAX_REQUEUES:
                logger.error(f"[REQUEUE] Bỏ job {queue} sau {job['_requeued'] - 1} lần trả về: {str(job)[:200]}")
                continue
            await client.lpush(queue, json.dumps(job, default=str))
            moved += 1
    if moved:
        logger.warning(f"[REQUEUE] Trả {moved} job của worker đã dừng về hàng đợi")
    return moved


async def reaper():
    while True:
        try:
            await requeue_orphans()
        except Exception as e:
            logger.error(f"[REQUEUE] lỗi: {e}", exc_info=True)
        await asyncio.sleep(REAP_INTERVAL)


async def flush_buffer_to_db(redis_client, batch_size=BATCH_SIZE):
    """Worker tổng quát: Một mình cân hết tất cả các loại bảng và hành động ghi DB"""
    while True:
        try:
            await asyncio.sleep(3) # Cứ 3 giây dọn dẹp các bảng 1 lần
            
            for config in BUFFER_CONFIGS:
                redis_key = config["key"]
                # logger.info(f"[START] Khởi động bộ Flusher cho key: {redis_key}")
                
                # 1. Hốt một mẻ dữ liệu từ Redis List ra
                batch_data = []
                for _ in range(batch_size):
                    item = await redis_client.lpop(redis_key)
                    if not item:
                        break
                    batch_data.append(json.loads(item))
                
                if not batch_data:
                    continue # Hàng chờ này trống, chuyển sang hàng chờ tiếp theo
                
                # 2. Dùng hàm sync_to_async để gọi hàm thực thi DB đồng bộ phía dưới
                await sync_to_async(execute_bulk_db, thread_sensitive=False)(batch_data, config)
                
        except Exception as e:
            logger.error(f"[GENERIC-FLUSHER-ERROR]: {e}", exc_info=True)

def execute_bulk_db(batch_data, config):
    """Hàm thực thi SQL đồng bộ bằng Django ORM dựa trên cấu hình động"""
    Model = apps.get_model(app_label=config["app"], model_name=config["model_name"])
    action = config["action"]
    
    try:
        if action == "bulk_update":
            # Tạo list các instance ảo dựa trên id và data truyền vào
            objects_to_update = [Model(**item) for item in batch_data]
            Model.objects.bulk_update(objects_to_update, config["fields"])
            logger.info(f"[BULK-UPDATE] Đã cập nhật thành công {len(objects_to_update)} dòng cho bảng {config['model_name']}.")

            return len(objects_to_update)
            
        elif action == "bulk_create":
            # Tạo list các instance mới hoàn toàn để insert
            objects_to_create = [Model(**item) for item in batch_data]
            Model.objects.bulk_create(objects_to_create, ignore_conflicts=True)
            logger.info(f"[BULK-CREATE] Đã chèn mới thành công {len(objects_to_create)} dòng vào bảng {config['model_name']}.")

            return len(objects_to_create)

        elif action == "collection_items_create":
            # Từ chờ AI vừa tạo xong vào bộ: dòng nháp của tác giả (có original_id, thứ tự cuối bộ) như các đường thêm từ khác.
            # Dòng chỉ có collection_id / sense_id thì không ai thấy (live_items lọc theo người tạo) nên không insert thô.
            from ai.models import AISense
            from core.models import Collection, CollectionItem
            from core.views.Collection import make_items

            wanted = defaultdict(list)
            for item in batch_data:
                wanted[item["collection_id"]].append(item["sense_id"])
            created = 0
            for collection_id, sense_ids in wanted.items():
                collection = Collection.objects.filter(pk=collection_id).first()
                if collection is None:
                    continue
                senses = {str(s.id): s for s in AISense.objects.filter(id__in=sense_ids)}
                present = set(CollectionItem.objects.filter(collection=collection).values_list("original_id", flat=True))
                to_add = []
                for sense_id in dict.fromkeys(sense_ids):
                    sense = senses.get(str(sense_id))
                    if sense is not None and (sense.original_id or sense.id) not in present:
                        to_add.append(sense)
                if not to_add:
                    continue
                last = CollectionItem.objects.filter(collection=collection).aggregate(last=Max("display_order"))["last"]
                CollectionItem.objects.bulk_create(make_items(collection.id, to_add, 0 if last is None else last + 1, collection.created_by))
                created += len(to_add)
            logger.info(f"[COLLECTION-ITEMS] Đã thêm {created} dòng vào bộ từ.")
            return created

        elif action == 'atoms_update':
            # case đặc biệt hiện chỉ áp dụng để update bảng collection các cột pending và invalid tự động

            if not batch_data:
                return 0

            # =========================================================
            # STEP 1: GROUP DATA IN PYTHON
            # =========================================================

            grouped = defaultdict(lambda: {
                "remove_pending": [],
                "invalid_words": [],
                "valid_count": 0,
            })

            for item in batch_data:
                collection_id = item["collection_id"]
                word = item["value"]
                action = item.get("action", "approve")

                grouped[collection_id]["remove_pending"].append(word)

                if action == "reject":
                    grouped[collection_id]["invalid_words"].append(word)
                else:
                    grouped[collection_id]["valid_count"] += 1

            # =========================================================
            # STEP 2: BUILD VALUES
            # =========================================================

            values = []

            for collection_id, data in grouped.items():
                values.append((
                    collection_id,
                    data["remove_pending"],
                    data["invalid_words"],
                    data["valid_count"],
                ))

            table = Model._meta.db_table

            # =========================================================
            # STEP 3: SINGLE ATOMIC UPDATE
            # =========================================================

            sql = f"""
                UPDATE {table} AS c
                SET
                    pending_words = ARRAY(
                        SELECT x
                        FROM unnest(
                            COALESCE(c.pending_words, ARRAY[]::varchar[])
                        ) AS x
                        WHERE NOT (x = ANY(v.remove_pending))
                    ),

                    invalid_words = CASE
                        WHEN v.invalid_words IS NOT NULL
                            AND cardinality(v.invalid_words) > 0
                        THEN array_cat(
                            COALESCE(c.invalid_words, ARRAY[]::varchar[]),
                            v.invalid_words
                        )
                        ELSE c.invalid_words
                    END,

                    item_count = c.item_count + v.valid_count

                FROM (
                    VALUES %s
                ) AS v(
                    id,
                    remove_pending,
                    invalid_words,
                    valid_count
                )

                WHERE c.id = v.id
                """

            # =========================================================
            # STEP 4: EXECUTE
            # =========================================================

            from psycopg2.extras import execute_values

            with transaction.atomic():
                with connection.cursor() as cursor:
                    execute_values(
                        cursor,
                        sql,
                        values,
                        template="(%s::uuid, %s::varchar[], %s::varchar[], %s)"  # Nếu ID là UUID
                    )

                    return cursor.rowcount
                
    except Exception as e:
        logger.error(f"[SQL-EXECUTE-ERROR] Lỗi thực thi SQL cho bảng {config['model_name']}: {e}")

# async def flush_buffer_to_db(result_key, save_func, task_type):
#     """Cơ chế trút bộ đệm kết quả vào Postgres theo lô tuần tự"""
#     logger.info(f"[START] Khởi động bộ Flusher cho key: {result_key}")
#     while True:
#         try:
#             await asyncio.sleep(FLUSH_INTERVAL)
            
#             batch_data = []
#             for _ in range(BATCH_SIZE):
#                 item = await redis_client.lpop(result_key)
#                 if not item:
#                     break
#                 batch_data.append(json.loads(item))

#             if batch_data:
#                 # Chuyển context sang ThreadPool để không block Event Loop chính
#                 await sync_to_async(save_func, thread_sensitive=False)(batch_data, task_type)

#         except Exception as e:
#             logger.error(f"[FLUSH-ERROR] Lỗi trút dữ liệu {result_key}: {e}")
#             await asyncio.sleep(1)

# --- KHỞI ĐỘNG HỆ THỐNG ---
import redis.asyncio as redis

async def main():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    
    # Khởi tạo Semaphore riêng cho từng cụm ranh giới hạn mức
    word_sema = asyncio.Semaphore(WORD_SEMA_LIMIT)
    collection_word_sema = asyncio.Semaphore(COLLECTION_WORD_SEMA_LIMIT)
    image_sema = asyncio.Semaphore(IMAGE_SEMA_LIMIT)
    trans_sema = asyncio.Semaphore(TRANS_SEMA_LIMIT)

    # Đăng ký các bánh răng chạy đồng thời
    tasks = [
        consume_queue("redis_word", word_sema, "word"),
        consume_queue("redis_collection_word", collection_word_sema, "collection_word"),
        consume_queue("redis_image", image_sema, "image"),
        consume_queue("redis_trans", trans_sema, "translate"),
        consume_lazy(asyncio.Semaphore(1), [(word_sema, WORD_SEMA_LIMIT), (collection_word_sema, COLLECTION_WORD_SEMA_LIMIT), (image_sema, IMAGE_SEMA_LIMIT), (trans_sema, TRANS_SEMA_LIMIT)]),
        
        # Flusher quản lý trút DB ngầm (Bật lên khi Sơn đưa cấu hình lưu kết quả vào redis)
        flush_buffer_to_db(redis_client),
        heartbeat(),
        reaper(),
    ]

    logger.info("--- ⚡ FLASHWISE ASYNC WORKER ONLINE VÀ SẴN SÀNG ⚡ ---")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Hệ thống đang tắt cấu hình an toàn...")