"""
Kết nối Redis dùng chung cho cả web và worker (mỗi process 1 pool, không tạo client mới mỗi request).
db 0: hàng đợi job AI. db 1: cache từ (WordCacheManager).
"""
import json
import threading

import redis
from django.conf import settings

_lock = threading.Lock()
_clients = {}


def _get_client(db, decode_responses=False):
    key = (db, decode_responses)
    client = _clients.get(key)
    if client is None:
        with _lock:
            client = _clients.get(key)
            if client is None:
                client = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=db, decode_responses=decode_responses)
                _clients[key] = client
    return client


def get_queue_client():
    return _get_client(0)


def get_cache_client():
    return _get_client(1, decode_responses=True)


def acquire_lock(name, ttl):
    """True nếu giành được khoá (chưa ai giữ). Dùng để 1 việc không bị đẩy vào hàng đợi nhiều lần trong ttl giây."""
    return bool(get_queue_client().set(name, 1, nx=True, ex=ttl))


def push_job(queue, payload):
    """Đẩy 1 job (dict) vào hàng đợi, worker (utils/celery/worker.py) sẽ lấy ra xử lý."""
    get_queue_client().rpush(queue, json.dumps(payload))
