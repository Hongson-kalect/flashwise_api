"""
Bổ sung các bước còn thiếu cho nghĩa do người dùng đề xuất (ai-senses/propose) sau khi được chấp nhận, để nghĩa đó đầy đủ như nghĩa
của luồng tạo từ (draft → refine → dịch → lưu → verify → ảnh → concept): dịch các ngôn ngữ chính, verify bản dịch, nối concept, viết brief
ảnh và xếp việc vẽ. Chạy ở worker (hàng đợi `redis_sense_post`, `finish_sense`), không chặn phản hồi của propose: người dùng thấy nghĩa ngay
(đã có en + ngôn ngữ của họ), phần còn lại về dần qua cache / socket. Mỗi bước lỗi riêng thì ghi log rồi làm tiếp bước sau.
"""
import asyncio
import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from ai.models import AISense
from utils.redis.client import push_job
from utils.utils.best_effort import best_effort

logger = logging.getLogger(__name__)

QUEUE = 'redis_sense_post'
STEP_TIMEOUT = 180


def queue_finish(sense_id):
    """Xếp việc hoàn thiện cho nghĩa vừa được chấp nhận (propose official hoặc admin promote). Lỗi Redis không làm hỏng phản hồi."""
    try:
        push_job(QUEUE, {'sense_id': str(sense_id)})
        return True
    except Exception:
        logger.exception('Không xếp được việc hoàn thiện nghĩa %s', sense_id)
        return False


def missing_languages(sense):
    """Ngôn ngữ chính (ONLINE_BASE_LANGUAGES) mà định nghĩa của nghĩa chưa có bản dịch."""
    definition = (sense.contents or {}).get('definition') or {}
    return [lang for lang in settings.ONLINE_BASE_LANGUAGES if lang != sense.language_code and not definition.get(lang)]


def _load(sense_id):
    return AISense.objects.select_related('word').filter(id=sense_id).exclude(is_deleted=True).first()  # is_deleted có thể NULL


async def _step(name, sense_id, coro):
    try:
        return await asyncio.wait_for(coro, timeout=STEP_TIMEOUT)
    except Exception as e:
        logger.warning('Hoàn thiện nghĩa %s: bước %s lỗi: %s', sense_id, name, e)
        return None


async def finish_sense(job):
    """Dịch → verify → nạp lại cache → ảnh → concept cho 1 nghĩa. Bỏ qua nếu nghĩa không còn chính thức."""
    sense_id = job.get('sense_id')
    sense = await sync_to_async(_load)(sense_id)
    if sense is None or not sense.is_official:
        logger.info('Hoàn thiện nghĩa %s: bỏ qua (không còn / chưa chính thức)', sense_id)
        return
    word, language_code, word_id = sense.word, sense.language_code, str(sense.word_id)

    # 1. Dịch các ngôn ngữ chính còn thiếu (cùng hàm dịch của luồng tạo từ), ghi gộp vào contents như job `translate` của worker
    missing = missing_languages(sense)
    if missing:
        from utils.ai.translate import ai_create_translate_sema
        from utils.celery.worker import save_translated_contents
        item = {'id': str(sense.id), 'pos': sense.pos, 'contents': sense.contents}
        payload = {'word_value': word.value, 'language_code': language_code, 'user_language_code': missing,
                   'missing_translate': [item], 'current_senses': [item], 'base_languages': list(settings.ONLINE_BASE_LANGUAGES)}
        updated = await _step('dịch', sense_id, ai_create_translate_sema(payload, False))
        if updated:
            await sync_to_async(save_translated_contents)(list(updated), language_code)

    # 2. Verify bản dịch (chỉ nghĩa chưa có verify_state cho đủ ngôn ngữ, tức nghĩa mới này)
    from utils.ai.online_verify import verify_word_online
    await _step('verify', sense_id, verify_word_online(word_id, language_code, list(settings.ONLINE_BASE_LANGUAGES)))

    # 3. Nạp lại cache để get-word trả bản dịch / verify mới nhất
    from utils.image_gen.service import _recache_word
    await sync_to_async(best_effort)(_recache_word, word_id)

    # 4. Brief ảnh + xếp việc vẽ (chỉ nghĩa chưa có ảnh)
    if settings.IMAGE_GEN_ENABLED:
        from utils.image_gen.service import prepare_and_enqueue
        await _step('ảnh', sense_id, prepare_and_enqueue(word_id))

    # 5. Nối concept (embedding + judge), báo app qua socket PARTIAL_LINKS
    if settings.CONCEPT_LINKING_ENABLED:
        from utils.concept.notify import link_word_and_notify
        await _step('concept', sense_id, link_word_and_notify(word_id))
    logger.info('Hoàn thiện nghĩa %s [%s] xong', sense_id, word.value)
