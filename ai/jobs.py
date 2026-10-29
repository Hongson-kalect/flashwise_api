import logging

from utils.redis.client import acquire_lock, push_job

logger = logging.getLogger(__name__)

# Một lần dịch bù cho (từ, ngôn ngữ) chỉ được đẩy vào hàng đợi 1 lần trong khoảng này; worker mở khoá sớm khi dịch xong
TRANSLATE_LOCK_SECONDS = 120


def queue_translation(word_id, language_code, user_language_code, missing, current_senses):
    """
    Đẩy việc dịch phần còn thiếu cho worker (hàng đợi redis_trans).
    missing / current_senses phải là dữ liệu thuần JSON (dict), không phải model instance.
    """
    try:
        if not acquire_lock(f"trans_lock:{word_id}:{user_language_code}", TRANSLATE_LOCK_SECONDS):
            return False
        push_job("redis_trans", {
            "word_id": str(word_id),
            "language_code": language_code,
            "user_language_code": user_language_code,
            "missing_translate": missing,
            "current_senses": current_senses,
        })
        return True
    except Exception:
        logger.exception("Không đẩy được job dịch cho word %s", word_id)
        return False
