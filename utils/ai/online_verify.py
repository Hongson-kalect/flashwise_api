"""
Kiểm chéo bản dịch ngay sau khi luồng online (get-word) lưu từ: cùng bước `verify` của luồng batch (ai/seedflow/verify_db), chỉ khác là
gọi Haiku online cho 1 từ thay vì Message Batches. Bản dịch bị sửa / gắn cờ ghi thẳng vào AISense; cache Redis được nạp lại.
"""
import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from utils.utils.best_effort import best_effort

logger = logging.getLogger(__name__)


def enabled():
    return bool(getattr(settings, 'SEED_VERIFY_ENABLED', True) and getattr(settings, 'ONLINE_VERIFY_ENABLED', True))


def _requests(word_id, language_code, langs):
    from ai.seedflow import verify_db
    return verify_db.build_requests([word_id], language_code, langs)


def _apply(meta, results, language_code):
    from ai.seedflow import verify_db
    return verify_db.apply_results(meta, results, language_code)


async def verify_word_online(word_id, language_code, langs):
    """Trả bản đếm {'ok','fixed','flagged',...} hoặc None khi không có gì để kiểm / tắt. Lỗi gọi AI không ném ra (chỉ ghi log)."""
    if not enabled():
        return None
    from ai.seedflow import llm
    requests, meta = await sync_to_async(_requests)(word_id, language_code, list(langs))
    if not requests:
        return None
    results = await llm.run_online(requests, concurrency=1)
    counts = await sync_to_async(_apply)(meta, results, language_code)
    if counts.get('fixed'):
        from utils.image_gen.service import _recache_word
        await sync_to_async(best_effort)(_recache_word, word_id)  # bản dịch vừa sửa phải có trong cache get-word
    logger.info('verify online %s: %s', word_id, counts)
    return counts
