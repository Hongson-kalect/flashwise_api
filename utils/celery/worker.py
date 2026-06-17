# 1. Thiết lập Django
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'flashcardApi.settings')
django.setup()
import asyncio
import json
import logging
from django.utils import timezone
from asgiref.sync import sync_to_async

from collections import defaultdict
from django.db import connection, transaction
from django.apps import apps # Tuyệt chiêu đọc Model động của Django
from django.db.models import Prefetch, Case, When, F, Value, Func, ExpressionWrapper, IntegerField
from django.contrib.postgres.fields import ArrayField
from django.db.models import CharField

from ai.models import AIWord, AISense
from core.models import Collection
from ai.serializers import AIWordSerializer
from utils.redis.word_init import WordCacheManager

from utils.ai.word_render import ai_create_new_word_sema
from utils.ai.translate import ai_create_translate_sema
from utils.celery.fetch_image import get_image_by_keyword
from utils.utils.sense_handle import serialize_entries

# --- CẤU HÌNH ---
REDIS_URL = 'redis://redis:6379/0'
WORD_SEMA_LIMIT = 5 
COLLECTION_WORD_SEMA_LIMIT = 5 
IMAGE_SEMA_LIMIT = 10 
TRANS_SEMA_LIMIT = 20
BATCH_SIZE = 500
FLUSH_INTERVAL = 3 
# Cấu hình danh sách các hàng đợi mà Flusher cần quét qua
BUFFER_CONFIGS = [
    {"key": "db_buffer:sense:update", "model_name": "AISense", "action": "bulk_update", "fields": ["contents"], "app":"ai"},
    {"key": "db_buffer:word:update", "model_name": "AIWord", "action": "bulk_update", "fields": ["text", "updated_at"], "app":"ai"},
    {"key": "db_buffer:collection:atoms_update", "model_name": "Collection", "action": "atoms_update", "fields": [], "app":"core"},
    {"key": "db_buffer:collectionItem:create", "model_name": "CollectionItem", "action": "bulk_create", "fields": [], "app":"core"},
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- TẦNG GIAO TIẾP DB TRÚT BUFFER ---
def save_to_postgres_sync(batch_data, task_type):
    """Thực thi ghi DB theo mẻ (Bulk Operations)"""
    logger.info(f"[DB-SAVE] {task_type.upper()} | Trút thành công mẻ gồm: {len(batch_data)} records vào Postgres.")
    # Thực tế: Model.objects.bulk_create(...) hoặc bulk_update(...)

# --- TẦNG XỬ LÝ LOGIC WORKER ---
async def handle_task(job, sema, task_type):
    """Xử lý core logic task, đảm bảo giải phóng semaphore ở khối finally"""
    time_start = timezone.now()
    try:
        if task_type == 'word':
            await ai_create_new_word_sema(job)
        elif task_type == 'image':
            await get_image_by_keyword(job)
        elif task_type == 'translate':
            # print('translate job', job)
                # Hàm này nhận job (chứa id và contents cũ), gọi AI dịch và trả về contents đã update
            updated_contents = await ai_create_translate_sema(job, False)

            payloads = []
            word_id =[]

            for contents in updated_contents:
                payload = {
                    "id": contents.get("id"),
                    "contents": contents.get("contents"),
                    "updated_at": timezone.now().isoformat()
                }
                payloads.append(json.dumps(payload))
                word_id.append(contents.get("word_id"))

            redis_key = "db_buffer:sense:update"
            await redis_client.rpush(redis_key, *payloads)

            if word_id:
                await sync_to_async(re_cache_word)(word_id)     
        
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

def re_cache_word(word_ids):
    logger.info(f"Re-cache word: {word_ids}")
    list = word_ids
    cache_manager = WordCacheManager()
    sense_qs = AISense.objects.filter(is_official=True).select_related('metadata').order_by('is_official')
    if isinstance(word_ids, str):
        list = [word_ids]

    # 4. Gộp vào query chính
    word_instances = AIWord.objects.filter(id__in=list).prefetch_related(
        Prefetch('senses', queryset=sense_qs, to_attr='prefetched_senses')
    ).all()

    for word_instance in word_instances:

        senses_instance = word_instance.prefetched_senses
        entries = serialize_entries(senses_instance)
        word_instance.processed_entries = entries

        data = AIWordSerializer(word_instance).data

        cache_manager.cache_word(word_instance.language_code, word_instance.id, word_instance.value, data.get('senses'))
        logger.info(f"Re-cache word completed: {word_instance.value}")

# --- TẦNG ĐIỀU PHỐI (CONSUMER VÀ FLUSHER) ---
async def consume_queue(queue_name, sema, task_type):
    """Consumer thông minh: Chỉ bốc máy nhặt việc từ Redis khi thực sự CÒN GHẾ TRỐNG"""
    logger.info(f"[START] Khởi động hàng chờ Consumer: {queue_name}")
    while True:
        try:
            # Chặn tại đây, nếu hết ghế trống tiến trình sẽ dừng lại không spam Redis
            await sema.acquire()

            # Nhặt việc từ Redis với timeout chống treo
            raw_data = await redis_client.blpop(queue_name, timeout=5)
            if not raw_data:
                sema.release() # Trả lại ghế nếu hàng chờ trống
                continue

            logger.info(f"[CONSUMER-LOOP] Chạy vòng lặp {queue_name}")

            job = json.loads(raw_data[1])
            
            # Tạo task chạy ngầm độc lập
            asyncio.create_task(handle_task(job, sema, task_type))

        except Exception as e:
            logger.error(f"[LOOP-ERROR] Lỗi vòng lặp Consumer {queue_name}: {e}")
            await asyncio.sleep(1)

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
        
        # Flusher quản lý trút DB ngầm (Bật lên khi Sơn đưa cấu hình lưu kết quả vào redis)
        flush_buffer_to_db(redis_client),
    ]

    logger.info("--- ⚡ FLASHWISE ASYNC WORKER ONLINE VÀ SẴN SÀNG ⚡ ---")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Hệ thống đang tắt cấu hình an toàn...")