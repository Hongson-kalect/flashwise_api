"""
Ảnh minh hoạ do AI vẽ cho từng nghĩa, qua hàng đợi Redis `IMAGE_GEN_QUEUE`.

Luồng:
  1. Nghĩa mới lưu xong (hoặc `manage.py queue_sense_images`): `enqueue_senses` đọc chủ thể ở
     `metadata.image_metadata = {subject, kind}` (do bước metadata sinh, tiếng Anh) -> khoá `image_key`.
  2. Khoá đã có ảnh (ImageContext provider=comfyui) -> gắn luôn cho nghĩa, KHÔNG vào hàng đợi (nghĩa của ngôn ngữ khác
     cùng chủ thể dùng lại ảnh ở đây). Chưa có -> ghi nghĩa vào set chờ `image_gen:wait:<khoá>`; chỉ nghĩa đầu tiên của
     khoá đó đẩy 1 job {key, subject, kind} (khoá `image_gen:job:<khoá>` chống đẩy trùng), các nghĩa sau chỉ vào set chờ.
  3. Worker (`utils/celery/worker.py`, `process_job`) vẽ bằng ComfyUI, lưu WebP vào storage (đĩa server tạm, sau này R2),
     tạo ImageLibrary + ImageContext, rồi gắn ảnh cho MỌI nghĩa trong set chờ, nạp lại cache từ và báo PARTIAL_IMAGE.
  ComfyUI chưa bật: job trả về đầu hàng đợi và hàng đợi nghỉ `ai_cooldown:comfyui` (job cứ xếp chồng, bật lại là chạy tiếp).
"""
import asyncio
import logging
import time

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

from ai.models.AISense import AISense
from core.models.ImageLibrary import ImageContext, ImageLibrary, ImageLibraryContext
from utils.ai.rate_limit import set_cooldown
from utils.image_gen import comfy
from utils.image_gen.subject import KINDS, build_prompt, image_key, provider_id, seed_for
from utils.redis.client import acquire_lock, get_queue_client, push_job
from utils.utils.best_effort import best_effort

logger = logging.getLogger(__name__)

PROVIDER = 'comfyui'
COOLDOWN_PROVIDER = 'comfyui'
OFFLINE_COOLDOWN = 30     # giây hàng đợi nghỉ khi ComfyUI chưa bật
MAX_ATTEMPTS = 3          # lỗi vẽ (không phải chưa bật) quá số lần này thì bỏ job
NON_DRAWABLE_POS = {'article', 'determiner', 'preposition', 'postposition', 'conjunction', 'pronoun', 'particle',
                    'auxiliary', 'auxiliary verb', 'modal verb', 'interjection', 'numeral', 'classifier', 'counter',
                    'prefix', 'suffix', 'affix', 'abbreviation'}
# Chủ thể quá chung chung (model nhỏ bịa khi không biết vẽ gì): vẽ ra cũng vô nghĩa
GENERIC_SUBJECTS = {'object', 'specific object', 'thing', 'item', 'something', 'concept', 'abstract concept', 'symbol', 'idea'}
WAIT_TTL = 7 * 24 * 3600
JOB_LOCK_TTL = 3600


def _wait_key(key):
    return f'image_gen:wait:{key}'


def _job_key(key):
    return f'image_gen:job:{key}'


def sense_subject(sense):
    """(chủ thể, loại) của nghĩa từ metadata; ('', '') nếu không vẽ được."""
    meta = sense.metadata.image_metadata if sense.metadata_id and sense.metadata else None
    meta = meta or {}
    subject, kind = meta.get('subject'), meta.get('kind')
    key = image_key(subject)
    if kind not in KINDS or not key or key in GENERIC_SUBJECTS:
        return '', ''
    return subject, kind


def sense_prompt(sense):
    """Mô tả vẽ (image_prompt) của nghĩa; '' nếu chưa có (từ tạo trước khi có trường này: vẽ theo chủ thể ngắn)."""
    meta = sense.metadata.image_metadata if sense.metadata_id and sense.metadata else None
    value = (meta or {}).get('prompt')
    return value.strip()[:400] if isinstance(value, str) else ''


def find_ready(key):
    """(ImageContext, url) nếu khoá đã có ảnh, không thì None."""
    context = ImageContext.objects.filter(description=key[:255], provider=PROVIDER).first()
    if context is None:
        return None
    first = context.images.exclude(url__isnull=True).exclude(url='').order_by('imagelibrarycontext__order').first()
    return (context, first.url) if first else None


def attach(sense_ids, context, url):
    """Gắn ảnh cho các nghĩa chưa có ảnh. Trả [{sense_id, word_id, word_value, language_code, url}] của nghĩa vừa gắn."""
    rows = list(AISense.objects.filter(id__in=list(sense_ids)).filter(Q(image_preview__isnull=True) | Q(image_preview=''))
                .values('id', 'word_id', 'word_value', 'language_code'))
    if not rows:
        return []
    AISense.objects.filter(id__in=[r['id'] for r in rows]).update(image_context=context, image_preview=url, updated_at=timezone.now())
    return [{'sense_id': str(r['id']), 'word_id': str(r['word_id']), 'word_value': r['word_value'],
             'language_code': r['language_code'], 'url': url} for r in rows]


def create_image(key, subject, description=None, still_wanted=None):
    """Vẽ + lưu ảnh cho 1 khoá. Trả (ImageContext, url). Lỗi ComfyUI ném ComfyUnavailable / ComfyError."""
    png = comfy.generate(build_prompt(subject, description), seed_for(key))
    if still_wanted is not None and not still_wanted():
        return None  # refine đã loại hết nghĩa chờ ảnh này trong lúc vẽ: không lưu
    webp, size = comfy.to_webp(png, transparent=settings.IMAGE_GEN_TRANSPARENT)
    pid = provider_id(key)
    image, _ = ImageLibrary.objects.get_or_create(
        provider=PROVIDER, provider_id=pid,
        defaults={
            'metadata': {'width': size[0], 'height': size[1], 'subject': subject, 'prompt': description or '', 'key': key, 'kind': 'illustration',
                         'style_version': settings.IMAGE_GEN_STYLE_VERSION, 'steps': settings.IMAGE_GEN_STEPS, 'lora': settings.COMFYUI_LORA, 'lora_strength': settings.COMFYUI_LORA_STRENGTH if settings.COMFYUI_LORA else 0,
                         'transparent': settings.IMAGE_GEN_TRANSPARENT,
                         'model': settings.COMFYUI_UNET, 'bytes': len(webp)},
            'attribution': {'generator': 'FLUX.2 klein 4B (Black Forest Labs, Apache-2.0)', 'license': 'Apache-2.0'},
        },
    )
    if not image.file:
        image.file.save(f'{PROVIDER}_{pid}.webp', ContentFile(webp), save=True)
    context, _ = ImageContext.objects.get_or_create(description=key[:255], provider=PROVIDER)
    ImageLibraryContext.objects.get_or_create(image=image, context=context, defaults={'order': 0})
    return context, image.url


def enqueue_senses(senses):
    """
    Với mỗi nghĩa chưa có ảnh: gắn ngay nếu khoá đã có ảnh, không thì xếp chờ (xem đầu file).
    senses: iterable AISense (nên select_related('metadata')). Trả bộ đếm.
    """
    stats = {'attached': 0, 'queued': 0, 'joined': 0, 'skipped': 0}
    redis = get_queue_client()
    for sense in senses:
        if sense.image_preview:
            stats['skipped'] += 1
            continue
        subject, kind = sense_subject(sense)
        if not subject:
            stats['skipped'] += 1
            continue
        key = image_key(subject)
        ready = find_ready(key)
        if ready:
            stats['attached'] += len(attach([sense.id], *ready))
            continue
        redis.sadd(_wait_key(key), str(sense.id))
        redis.expire(_wait_key(key), WAIT_TTL)
        if acquire_lock(_job_key(key), JOB_LOCK_TTL):
            # Vẽ theo chính khoá (đã chuẩn hoá) chứ không theo cách viết của nghĩa nào: cùng khoá luôn cùng prompt, cùng ảnh
            push_job(settings.IMAGE_GEN_QUEUE, {'key': key, 'subject': key, 'prompt': sense_prompt(sense), 'kind': kind, 'attempts': 0})
            stats['queued'] += 1
        else:
            stats['joined'] += 1
    return stats


BACKFILL_COOLDOWN = 600  # giây: tra lại 1 từ chưa đủ ảnh thì cách nhau ít nhất chừng này mới xếp việc vẽ bù lần nữa


def _needs_image(sense):
    """Nghĩa còn đáng xếp vẽ: mọi nghĩa đều cần 1 ảnh (kể cả từ ngữ pháp, nghĩa trừu tượng: brief vẽ cảnh người que theo câu ví dụ)."""
    return not sense.image_preview


def request_missing_images(word_id, payload=None):
    """
    Gọi khi get-word trả từ đã hoàn thành: còn nghĩa chưa có ảnh (từ tạo trước khi bật ảnh, ảnh bị xoá, lần vẽ trước lỗi)
    thì xếp 1 việc {'type': 'prepare'} vào hàng đợi vẽ: worker viết brief còn thiếu rồi xếp vẽ từng khoá ảnh, xong thì báo
    PARTIAL_IMAGE cho người đang xem. Mỗi từ tối đa 1 lần / BACKFILL_COOLDOWN giây. Trả True nếu đã xếp việc.

    `payload` (dữ liệu từ get-word sắp trả): mọi nghĩa đã có ảnh thì thoát ngay, không đụng DB. Còn lại lấy khoá chống lặp TRƯỚC
    khi truy vấn DB, nên chi phí DB tối đa 1 lần / BACKFILL_COOLDOWN giây mỗi từ (kể cả từ chỉ còn nghĩa không vẽ).
    """
    if not settings.IMAGE_GEN_ENABLED or not word_id:
        return False
    if isinstance(payload, dict):
        rows = payload.get('senses')
        rows = list(rows.values()) if isinstance(rows, dict) else list(rows or [])
        if rows and all(isinstance(r, dict) and r.get('image_preview') for r in rows):
            return False
    if not acquire_lock(f'image_gen:backfill:{word_id}', BACKFILL_COOLDOWN):
        return False
    senses = AISense.objects.filter(word_id=word_id).select_related('metadata')
    if not any(_needs_image(s) for s in senses):
        return False
    push_job(settings.IMAGE_GEN_QUEUE, {'type': 'prepare', 'word_id': str(word_id), 'attempts': 0})
    return True


def early_enqueue(preset):
    """
    Đẩy việc vẽ NGAY sau refine, trước khi nghĩa lưu vào DB (đỡ ~1-3s): preset = {sense_id: brief}. Nghĩa vào set chờ của khoá
    ảnh và chỉ nghĩa đầu của khoá đẩy 1 job. Khoá đã có ảnh thì bỏ qua (gắn sau khi lưu, ở enqueue_senses). Worker đợi nghĩa lưu xong
    mới gắn ảnh (run_job). Trả số job đã đẩy.
    """
    redis = get_queue_client()
    pushed = 0
    for sense_id, brief in preset.items():
        key = image_key(brief.get('subject'))
        if not key or key in GENERIC_SUBJECTS or brief.get('kind') not in KINDS or find_ready(key):
            continue
        redis.sadd(_wait_key(key), str(sense_id))
        redis.expire(_wait_key(key), WAIT_TTL)
        if acquire_lock(_job_key(key), JOB_LOCK_TTL):
            push_job(settings.IMAGE_GEN_QUEUE, {'key': key, 'subject': key, 'prompt': (brief.get('prompt') or '')[:400],
                                                'kind': brief['kind'], 'attempts': 0})
            pushed += 1
    return pushed


def prune_draft_senses(drafts, keep_ids):
    """
    Sau refine (chế độ vẽ trước refine): nghĩa nháp KHÔNG còn trong `keep_ids` (bị xoá, hoặc đổi loại từ) bị gỡ khỏi set chờ của khoá ảnh
    để ảnh không gắn cho nghĩa không tồn tại; khoá không còn ai chờ thì job vẽ (nếu chưa chạy) bị bỏ ở run_job. Trả số nghĩa đã gỡ.
    """
    redis = get_queue_client()
    removed = 0
    keep = {str(i) for i in keep_ids}
    for sense_id, entry in (drafts or {}).items():
        if str(sense_id) in keep:
            continue
        key = image_key(((entry or {}).get('brief') or {}).get('subject'))
        if key:
            removed += redis.srem(_wait_key(key), str(sense_id))
    return removed


def _apply_preset(senses, preset):
    """Ghi brief dựng sẵn (cùng lúc với refine) vào metadata của nghĩa vừa lưu."""
    from utils.image_gen.brief import needs_brief, provider
    for sense in senses:
        brief = preset.get(str(sense.id))
        if brief and needs_brief(sense):
            meta = sense.metadata
            meta.image_metadata = {**(meta.image_metadata or {}), 'subject': brief['subject'], 'prompt': brief['prompt'],
                                   'kind': brief['kind'], 'source': provider()}
            meta.save(update_fields=['image_metadata'])


async def prepare_and_enqueue(word_id, preset=None):
    """Sau khi từ lưu xong: Haiku viết brief ảnh cho nghĩa chưa có (utils/image_gen/brief.py), rồi xếp việc vẽ. Lỗi chỉ ghi log."""
    from utils.image_gen.brief import ensure_briefs
    try:
        senses = await sync_to_async(lambda: list(AISense.objects.filter(word_id=word_id).select_related('metadata')))()
        if preset:
            await sync_to_async(_apply_preset)(senses, {str(k): v for k, v in preset.items()})
        await ensure_briefs(senses)
    except Exception as e:
        logger.warning('[IMAGE-BRIEF] Không viết được brief ảnh cho từ %s: %s', word_id, e)
    await sync_to_async(best_effort)(enqueue_word_images, word_id)


def enqueue_word_images(word_id):
    """Sau khi từ lưu xong: xếp ảnh cho các nghĩa của từ."""
    senses = AISense.objects.filter(word_id=word_id).select_related('metadata')
    return enqueue_senses(senses)


def run_job(job):
    """Xử lý 1 job (đồng bộ, chạy ở thread): vẽ nếu khoá chưa có ảnh, rồi gắn cho mọi nghĩa đang chờ. Trả danh sách `attach`."""
    key, subject = job['key'], job['subject']
    redis = get_queue_client()

    def waiters():
        return redis.scard(_wait_key(key))

    ready = find_ready(key)
    if not ready and not waiters():
        redis.delete(_job_key(key))  # vẽ trước refine mà refine đã loại hết nghĩa chờ: bỏ job, không tốn lượt vẽ
        logger.info('[IMAGE-GEN] Bỏ job "%s" trước khi vẽ: không còn nghĩa nào chờ', key)
        return []
    created = ready or create_image(key, subject, job.get('prompt'), still_wanted=waiters)
    if created is None:
        redis.delete(_job_key(key))
        logger.info('[IMAGE-GEN] Vẽ xong "%s" nhưng không còn nghĩa nào chờ, không lưu ảnh (vẽ thừa)', key)
        return []
    context, url = created

    waiting = [m.decode() if isinstance(m, bytes) else m for m in redis.smembers(_wait_key(key))]
    # Việc vẽ được đẩy ngay sau refine (early_enqueue) nên nghĩa có thể chưa kịp lưu DB lúc ảnh vẽ xong: chờ tối đa ~6s cho đủ dòng
    for _ in range(6):
        if AISense.objects.filter(id__in=waiting).count() >= len(waiting):
            break
        time.sleep(1)
    attached = attach(waiting, context, url)
    if waiting:
        redis.srem(_wait_key(key), *waiting)
    redis.delete(_job_key(key))
    return attached


def _recache_word(word_id):
    from ai.queries import load_word
    from ai.serializers import AIWordSerializer
    from utils.redis.word_init import WordCacheManager

    word = load_word(id=word_id)
    if word is None:
        return
    data = AIWordSerializer(word).data
    WordCacheManager().cache_word(word.language_code, word.id, word.value, data.get('senses'), word_fields=data)


async def notify(attached):
    """Nạp lại cache các từ có nghĩa vừa có ảnh và báo người đang xem (PARTIAL_IMAGE). Lỗi chỉ ghi log."""
    from utils.utils.socket import get_safe_room_id, socket_message

    by_word = {}
    for row in attached:
        by_word.setdefault(row['word_id'], []).append(row)
    for word_id, rows in by_word.items():
        await sync_to_async(best_effort)(_recache_word, word_id)
        first = rows[0]
        try:
            room = get_safe_room_id(first['word_value'] or '', first['language_code'])
            await socket_message(room, {'type': 'PARTIAL_IMAGE', 'payload': {r['sense_id']: r['url'] for r in rows}})
        except Exception as e:
            logger.warning('Gửi socket PARTIAL_IMAGE cho từ %s lỗi: %s', word_id, e)


async def process_job(job):
    """
    Việc của worker cho 1 job. Trả (kết quả, job): 'done', 'offline' (ComfyUI chưa bật: worker trả job về đầu hàng đợi),
    'retry' (lỗi vẽ, job đã tăng attempts: worker trả về hàng đợi), 'dropped' (quá MAX_ATTEMPTS: bỏ, mở khoá cho lần sau).
    """
    if job.get('type') == 'prepare':
        await prepare_and_enqueue(job['word_id'])  # viết brief còn thiếu rồi xếp job vẽ; lỗi bên trong chỉ ghi log
        return 'done', job
    try:
        attached = await sync_to_async(run_job, thread_sensitive=False)(job)
    except comfy.ComfyUnavailable as e:
        logger.warning('[IMAGE-GEN] %s; giữ job "%s", nghỉ %ss', e, job.get('key'), OFFLINE_COOLDOWN)
        await sync_to_async(set_cooldown)(COOLDOWN_PROVIDER, OFFLINE_COOLDOWN)
        return 'offline', job
    except Exception as e:
        attempts = int(job.get('attempts', 0)) + 1
        logger.error('[IMAGE-GEN] Vẽ "%s" lỗi lần %s: %s', job.get('key'), attempts, e)
        if attempts >= MAX_ATTEMPTS:
            await sync_to_async(best_effort)(get_queue_client().delete, _job_key(job['key']))
            return 'dropped', job
        await asyncio.sleep(2 * attempts)
        return 'retry', {**job, 'attempts': attempts}
    await notify(attached)
    return 'done', job
