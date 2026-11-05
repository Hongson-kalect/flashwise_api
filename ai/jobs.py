import logging

from django.conf import settings

from utils.redis.client import acquire_lock, get_queue_client, push_job

logger = logging.getLogger(__name__)

# Một lần dịch bù cho (từ, ngôn ngữ) chỉ được đẩy vào hàng đợi 1 lần trong khoảng này; worker mở khoá sớm khi dịch xong
TRANSLATE_LOCK_SECONDS = 120


def queue_translation(word_id, language_code, user_language_code, missing, current_senses):
    """
    Đẩy việc dịch phần còn thiếu cho worker (hàng đợi redis_trans).
    missing / current_senses phải là dữ liệu thuần JSON (dict), không phải model instance.
    """
    try:
        if not acquire_lock(f"trans_lock:{word_id}:{language_code}:{user_language_code}", TRANSLATE_LOCK_SECONDS):
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


LAZY_QUEUE = "redis_trans_lazy"


def is_popular_pair(language_code, user_language_code):
    """Cả ngôn ngữ của nội dung và của user đều trong POPULAR_LANGUAGES: đáng dịch nền."""
    return language_code in settings.POPULAR_LANGUAGES and user_language_code in settings.POPULAR_LANGUAGES


def lazy_queue_length():
    try:
        return int(get_queue_client().llen(LAZY_QUEUE))
    except Exception:
        return 0


def queue_lazy_translation(word_id, language_code, user_language_code, missing, current_senses):
    """
    Xếp việc dịch NỀN (cùng dạng job với queue_translation): worker chỉ làm khi mọi hàng đợi gấp rỗng, chậm và từng việc một,
    để thư viện được bổ sung dần mà không chen việc của người đang chờ. Cặp ngôn ngữ không phổ biến, hàng đợi nền đầy hoặc
    (từ, nguồn, đích) đã xếp trong LAZY_LOCK_SECONDS thì bỏ qua.
    """
    try:
        if not settings.LAZY_TRANS_ENABLED or not is_popular_pair(language_code, user_language_code):
            return False
        if lazy_queue_length() >= settings.LAZY_TRANS_MAX_QUEUE:
            return False
        if not acquire_lock(f"trans_lazy:{word_id}:{language_code}:{user_language_code}", settings.LAZY_LOCK_SECONDS):
            return False
        push_job(LAZY_QUEUE, {
            "word_id": str(word_id),
            "language_code": language_code,
            "user_language_code": user_language_code,
            "missing_translate": missing,
            "current_senses": current_senses,
            "lazy": True,
        })
        return True
    except Exception:
        logger.exception("Không xếp được việc dịch nền cho word %s", word_id)
        return False

