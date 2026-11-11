"""
Hàng đợi nối concept trong Redis (db 0), một hàng mỗi ngôn ngữ của nghĩa: `concept_arr:<lang>`.

Luồng seed (stage concept, SEED_CONCEPT_QUEUE=true) chỉ đẩy id nghĩa đã lưu vào đây rồi đi tiếp; consumer (`seed_flow concept-worker --lang X`)
lấy lần lượt và gọi link_sense (embedding + judge online, ~2 giây / nghĩa). Mỗi ngôn ngữ MỘT consumer vì ứng viên của nghĩa sau phụ thuộc
quyết định của nghĩa trước cùng ngôn ngữ.

Độ tin cậy: lấy bằng LMOVE sang `concept_arr_busy:<lang>` rồi LREM khi xong; consumer chết giữa chừng thì lần khởi động sau `requeue_busy` trả
các nghĩa dở về đầu hàng. Nghĩa đã có concept thì link_sense trả 'already' nên đẩy trùng hay chạy lại đều an toàn.
Lỗi khi nối: ghi vào `concept_arr_failed:<lang>` (id + lỗi) và đi tiếp, không chặn hàng.
"""
import json
import logging
import time

from utils.redis.client import get_queue_client

logger = logging.getLogger(__name__)

PREFIX = 'concept_arr'


def _key(kind, lang):
    return f'{PREFIX}{kind}:{lang}'


def queue_key(lang):
    return _key('', lang)


def busy_key(lang):
    return _key('_busy', lang)


def failed_key(lang):
    return _key('_failed', lang)


def stats_key(lang):
    return _key('_stats', lang)


def push(lang, sense_ids, client=None):
    """Đẩy id nghĩa (theo thứ tự) vào hàng của ngôn ngữ. Trả số id đã đẩy."""
    ids = [str(i) for i in sense_ids]
    if ids:
        (client or get_queue_client()).rpush(queue_key(lang), *ids)
    return len(ids)


def claim(lang, client=None):
    """Lấy id đầu hàng sang hàng "đang làm"; None khi hàng trống."""
    raw = (client or get_queue_client()).lmove(queue_key(lang), busy_key(lang), 'LEFT', 'RIGHT')
    return raw.decode() if isinstance(raw, bytes) else raw


def ack(lang, sense_id, outcome=None, client=None):
    client = client or get_queue_client()
    client.lrem(busy_key(lang), 1, sense_id)
    if outcome:
        client.hincrby(stats_key(lang), outcome, 1)


def fail(lang, sense_id, error, client=None):
    client = client or get_queue_client()
    client.rpush(failed_key(lang), json.dumps({'id': sense_id, 'error': str(error)[:300]}, ensure_ascii=False))
    client.lrem(busy_key(lang), 1, sense_id)
    client.hincrby(stats_key(lang), 'failed', 1)


def requeue_busy(lang, client=None):
    """Trả các nghĩa đang làm dở (consumer trước chết) về ĐẦU hàng, giữ thứ tự cũ. Trả số nghĩa."""
    client = client or get_queue_client()
    n = 0
    while client.lmove(busy_key(lang), queue_key(lang), 'RIGHT', 'LEFT') is not None:
        n += 1
    return n


def requeue_failed(lang, client=None):
    """Đưa lại các nghĩa nối lỗi vào cuối hàng để thử lại (vd sau khi sửa khoá API). Trả số nghĩa."""
    client = client or get_queue_client()
    n = 0
    while (raw := client.lpop(failed_key(lang))) is not None:
        push(lang, [json.loads(raw)['id']], client)
        n += 1
    return n


def languages(client=None):
    """Các ngôn ngữ đang có hàng (đang chờ, đang làm hoặc lỗi)."""
    client = client or get_queue_client()
    found = set()
    for kind in ('', '_busy', '_failed'):
        for raw in client.scan_iter(f'{PREFIX}{kind}:*'):
            found.add((raw.decode() if isinstance(raw, bytes) else raw).split(':', 1)[1])
    return sorted(found)


def depth(lang, client=None):
    client = client or get_queue_client()
    stats = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in client.hgetall(stats_key(lang)).items()}
    return {'waiting': client.llen(queue_key(lang)), 'busy': client.llen(busy_key(lang)), 'failed': client.llen(failed_key(lang)), 'done': stats}


def consume(lang, link_fn, client=None, stop_when_empty=True, idle_seconds=5, limit=None, sleep=time.sleep, on_item=None):
    """
    Vòng lấy việc: link_fn(sense_id) -> dict có 'outcome' (hoặc ném lỗi). Trả {'processed', 'outcomes', 'seconds'}.
    stop_when_empty=False: hàng trống thì chờ idle_seconds rồi hỏi lại (chạy dài hạn, dừng bằng Ctrl+C).
    """
    client = client or get_queue_client()
    requeued = requeue_busy(lang, client)
    if requeued:
        logger.info('concept %s: trả %s nghĩa dở về hàng', lang, requeued)
    outcomes, processed, started = {}, 0, time.monotonic()
    while limit is None or processed < limit:
        sense_id = claim(lang, client)
        if sense_id is None:
            if stop_when_empty:
                break
            sleep(idle_seconds)
            continue
        try:
            result = link_fn(sense_id)
            outcome = (result or {}).get('outcome') or 'done'
            ack(lang, sense_id, outcome, client)
        except Exception as e:  # nối hỏng không chặn hàng; thử lại bằng requeue_failed
            logger.warning('Nối concept lỗi cho nghĩa %s: %s', sense_id, e)
            fail(lang, sense_id, e, client)
            outcome = 'failed'
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        processed += 1
        if on_item:
            on_item(sense_id, outcome, processed)
    return {'processed': processed, 'outcomes': outcomes, 'seconds': round(time.monotonic() - started, 1)}
