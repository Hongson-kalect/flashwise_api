"""Chạy nối concept cho một từ ở nền (sau khi từ đã lưu), nạp lại cache và báo app qua socket PARTIAL_LINKS (+ PARTIAL_IMAGE nếu ảnh đổi)."""
import logging

from asgiref.sync import sync_to_async

from ai.models.AISense import AISense
from utils.concept.linker import link_word
from utils.image_gen.service import _recache_word
from utils.concept.payload import concept_payload
from utils.utils.best_effort import best_effort

logger = logging.getLogger(__name__)


def _word_payloads(word_id, sense_ids):
    from ai.models.AIWord import AIWord
    word = AIWord.objects.filter(id=word_id).first()
    senses = {str(s.id): s for s in AISense.objects.filter(id__in=sense_ids).select_related('concept')}
    return word, {sid: concept_payload(s) for sid, s in senses.items() if s.concept_id}, {sid: s.image_preview for sid, s in senses.items() if s.image_preview}


async def link_word_and_notify(word_id):
    results = await sync_to_async(link_word)(word_id)
    linked = [r['sense_id'] for r in results if r.get('concept_id')]
    if not linked:
        return results
    from utils.utils.socket import get_safe_room_id, socket_message
    word, payloads, images = await sync_to_async(_word_payloads)(word_id, linked)
    if word is None:
        return results
    await sync_to_async(best_effort)(_recache_word, word_id)
    try:
        room = get_safe_room_id(word.value, word.language_code)
        await socket_message(room, {'type': 'PARTIAL_LINKS', 'payload': payloads})
    except Exception as e:
        logger.warning('Gửi socket PARTIAL_LINKS cho từ %s lỗi: %s', word_id, e)
    # Ảnh dùng chung trong concept có thể vừa đổi ở nghĩa của từ KHÁC: nạp lại cache và báo từ đó
    changed = [sid for r in results for sid in (r.get('image_changed') or []) if sid not in linked]
    if changed:
        await sync_to_async(_notify_images)(changed)
    return results


def _notify_images(sense_ids):
    import asyncio
    from utils.utils.socket import get_safe_room_id, socket_message
    by_word = {}
    for s in AISense.objects.filter(id__in=sense_ids).select_related('word'):
        by_word.setdefault(s.word_id, []).append(s)
    for wid, senses in by_word.items():
        best_effort(_recache_word, wid)
        try:
            room = get_safe_room_id(senses[0].word.value, senses[0].word.language_code)
            asyncio.run(socket_message(room, {'type': 'PARTIAL_IMAGE', 'payload': {str(s.id): s.image_preview for s in senses}}))
        except Exception as e:
            logger.warning('Báo ảnh dùng chung cho từ %s lỗi: %s', wid, e)
