import asyncio
import json
import logging
from asgiref.sync import sync_to_async

# --- CẤU HÌNH ---
REDIS_URL = 'redis://redis:6379/0'
# Quota: 2 Worker x 10 Sema = 20 Concurrent (Điều chỉnh theo API Key)
WORD_SEMA_LIMIT = 10 
TRANS_SEMA_LIMIT = 20
BATCH_SIZE = 50
FLUSH_INTERVAL = 3  # Giây

# Setup Logging để dễ debug
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- TẦNG GIAO TIẾP NGOẠI VI (AI & DB) ---

async def call_gemini_stream(job, task_type):
    """Giả lập gọi Gemini API với cơ chế streaming"""
    # Thực tế: await client.models.generate_content(...)
    steps = 3 if task_type == "word" else 2
    for i in range(steps):
        logger.info(f"[AI] {task_type} job {job} | Chunk {i}")
        await asyncio.sleep(0.2)  # Giả lập độ trễ I/O
        yield f"{task_type}_chunk_{i}"

def save_to_postgres_sync(data_list, task_type):
    """Hàm ghi DB đồng bộ - Nơi thực hiện Bulk Update/Create"""
    # Ví dụ: Word.objects.bulk_update(...)
    logger.info(f"[DB-SAVE] {task_type.upper()} | Batch size: {len(data_list)}")

# --- TẦNG XỬ LÝ LOGIC (HANDLERS) ---

async def handle_task(job, sema, task_type):
    """Xử lý từng task riêng lẻ với Semaphore bảo vệ"""
    async with sema:
        try:
            result_chunks = []
            async for chunk in call_gemini_stream(job, task_type):
                result_chunks.append(chunk)
            
            # Đẩy kết quả vào hàng chờ lưu DB (Buffer)
            result_key = f"redis_{task_type}_result"
            # payload = json.dumps({"job_id": job["id"], "result": result_chunks})
            payload = json.dumps({"job_id": job, "result": result_chunks})
            
            # Dùng Redis Client từ instance global
            await redis_client.rpush(result_key, payload)
            
        except Exception as e:
            logger.error(f"[AI-ERROR] {task_type} job {job}: {e}")
            # logger.error(f"[AI-ERROR] {task_type} job {job.get('id')}: {e}")

# --- TẦNG ĐIỀU PHỐI (CONSUMERS & FLUSHERS) ---

async def consume_queue(queue_name, sema, handler_func, task_type):
    """Vòng lặp nhặt việc từ Redis. Chỉ nhặt khi CÒN GHẾ TRỐNG."""
    logger.info(f"[START] Consumer for {queue_name}")
    while True:
        try:
            # CHỐT CHẶN: Kiểm tra Semaphore trước khi bốc máy khỏi Redis
            if sema.locked():
                await asyncio.sleep(0.1)
                continue

            # BLPOP trả về (key, data)
            raw_data = await redis_client.blpop(queue_name, timeout=10)
            if not raw_data:
                continue

            job = json.loads(raw_data[1])
            # Bắn task vào background và quay lại nhặt tiếp ngay lập tức
            asyncio.create_task(handler_func(job))

        except Exception as e:
            logger.error(f"[LOOP-ERROR] {queue_name}: {e}")
            await asyncio.sleep(1)

async def flush_buffer_to_db(result_key, save_func, task_type):
    """Vòng lặp dọn dẹp Redis đổ vào DB theo mẻ (Atomic LPOP)"""
    logger.info(f"[START] Flusher for {result_key}")
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)
            
            batch_data = []
            # Atomic: Lấy ra là mất luôn khỏi Redis, 2 worker không bao giờ trùng nhau
            for _ in range(BATCH_SIZE):
                item = await redis_client.lpop(result_key)
                if not item:
                    break
                batch_data.append(json.loads(item))

            if batch_data:
                # Đẩy sang Thread riêng để không block Event Loop
                await sync_to_async(save_func, thread_sensitive=False)(batch_data, task_type)

        except Exception as e:
            logger.error(f"[FLUSH-ERROR] {result_key}: {e}")
            await asyncio.sleep(1)

# --- KHỞI CHẠY ---

import redis.asyncio as redis
async def main():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    
    # Khởi tạo Semaphore riêng cho từng loại task
    word_sema = asyncio.Semaphore(WORD_SEMA_LIMIT)
    trans_sema = asyncio.Semaphore(TRANS_SEMA_LIMIT)

    # Đăng ký các "bánh răng" vào hệ thống
    tasks = [
        # Nhóm Consumer (AI Workers)
        consume_queue("redis_word", word_sema, 
                      lambda j: handle_task(j, word_sema, "word"), "word"),
        consume_queue("redis_trans", trans_sema, 
                      lambda j: handle_task(j, trans_sema, "translate"), "translate"),
        
        # Nhóm Flusher (DB Workers)
        flush_buffer_to_db("redis_word_result", save_to_postgres_sync, "word"),
        flush_buffer_to_db("redis_translate_result", save_to_postgres_sync, "translate"),
    ]

    logger.info("--- SYSTEM ONLINE ---")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("System shutting down...")