import logging

from ai.models.AIWord import AIWord
from utils.redis.client import push_job

logger = logging.getLogger(__name__)


class GenerationError(Exception):
    """Không khởi tạo được việc tạo từ (hàng đợi / DB lỗi); trạng thái đã được dọn lại."""


def start_word_generation(cache_manager, word, language_code, user_language_code, get_user, existing=None, queue='redis_word', allow=None, extra=None):
    """
    Nhận việc tạo 1 từ mới bằng AI: dựng cache PROCESSING, tạo (hoặc dùng lại) bản ghi AIWord và đẩy job cho worker.
    Dùng chung cho get-word (queue redis_word) và bulk-create (queue redis_collection_word).

    existing: bản ghi AIWord cũ (FAILED / treo) để dùng lại, tạo mới sẽ vi phạm unique (value, language_code).
    get_user: hàm trả user đứng tên bản ghi, chỉ gọi khi thật sự tạo mới.
    allow: hàm kiểm tra hạn mức (vd throttle), chỉ gọi khi mình là người khởi tạo; False thì huỷ.
    extra: thêm vào job, vd replace_sense_ids (tạo lại từ: nghĩa cũ thôi chính thức khi bản mới lưu xong).

    Trả (trạng thái, dữ liệu cache): 'joined' đã có người khởi tạo trước (chỉ đăng ký thêm ngôn ngữ),
    'throttled' vượt hạn mức, 'started' đã đẩy job. Lỗi thì ném GenerationError.
    """
    created, init_data = cache_manager.cache_word_init(language_code, word, user_language_code)
    cached_word = init_data.get('word')

    if not created:
        if user_language_code not in (init_data.get('langs') or []):
            cache_manager.cache_word_add_translate(language_code, word, user_language_code)
        return 'joined', init_data

    if allow is not None and not allow():
        cache_manager.cache_word_clear_specific(language_code, word)
        return 'throttled', None

    try:
        user = get_user()
        if existing is None:
            word_instance = AIWord.objects.create(**cached_word, created_by=user, status="PROCESSING")
        else:
            existing.status = "PROCESSING"
            existing.save(update_fields=['status', 'updated_at'])
            word_instance = existing
            cached_word['id'] = str(existing.id)
            cache_manager.cache_word_set_word(language_code, word, cached_word)

        push_job(queue, {
            "user_id": str(user.id),
            "word_id": str(word_instance.id),
            "value": word,
            "language_code": language_code,
            "user_language_code": user_language_code,
            **(extra or {}),
        })
    except Exception as e:
        logger.exception("Không khởi tạo được việc tạo từ %s", word)
        cache_manager.cache_word_clear_specific(language_code, word)
        AIWord.objects.filter(value=word, language_code=language_code).exclude(status='COMPLETED').update(status='FAILED')
        raise GenerationError(word) from e

    return 'started', {**init_data, 'word': cached_word}
