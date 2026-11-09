"""
Chạy một loạt lệnh gọi Claude theo 2 cách có cùng dạng kết quả:
  - batch: Message Batches API (giảm giá 50%, xong trong vài phút đến 24 giờ); submit rồi hỏi lại bằng id.
  - online: gọi từng lệnh song song (cho lô nhỏ / thử, hoặc khi batch lỗi).
Mỗi request: {'custom_id', 'params'} với params giống hệt tham số messages.create. Kết quả: {custom_id: Outcome dict}.
"""
import asyncio
import json
import logging

from anthropic import Anthropic
from django.conf import settings

from utils.ai import claude

logger = logging.getLogger(__name__)

# Giá niêm yết đã ghi trong CLAUDE.md (Haiku 5.5 $0,10 / $0,50 mỗi triệu token); batch = 50%
PRICE_IN, PRICE_OUT = 0.10, 0.50


def cost(tokens_in, tokens_out, batch=False):
    return (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6 * (0.5 if batch else 1)


def outcome_from_message(message):
    """Message của SDK -> {'ok', 'text', 'stop_reason', 'tokens_in', 'tokens_out'}."""
    text = next((b.text for b in message.content if getattr(b, 'type', '') == 'text'), '')
    return {'ok': bool(text), 'text': text, 'stop_reason': message.stop_reason,
            'tokens_in': message.usage.input_tokens, 'tokens_out': message.usage.output_tokens, 'error': ''}


def failed(error):
    return {'ok': False, 'text': '', 'stop_reason': '', 'tokens_in': 0, 'tokens_out': 0, 'error': str(error)[:300]}


# ----------------------------- batch -----------------------------

def _sync_client():
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=2)


def submit_batch(requests):
    batch = _sync_client().messages.batches.create(requests=requests)
    return batch.id


def batch_status(batch_id):
    """('in_progress' | 'ended' | ..., {'processing', 'succeeded', 'errored', 'canceled', 'expired'})"""
    b = _sync_client().messages.batches.retrieve(batch_id)
    counts = b.request_counts
    return b.processing_status, {k: getattr(counts, k, 0) for k in ('processing', 'succeeded', 'errored', 'canceled', 'expired')}


def batch_results(batch_id):
    out = {}
    client = _sync_client()  # giữ tham chiếu: client bị thu gom thì kết nối đóng giữa chừng khi đang đọc kết quả (Bad file descriptor)
    for item in client.messages.batches.results(batch_id):
        result = item.result
        if result.type == 'succeeded':
            out[item.custom_id] = outcome_from_message(result.message)
        else:
            error = getattr(getattr(result, 'error', None), 'error', None)
            out[item.custom_id] = failed(f'{result.type}: {getattr(error, "message", "") or ""}')
    return out


# ----------------------------- online -----------------------------

async def _one(client, request, semaphore, attempts=4):
    async with semaphore:
        for attempt in range(attempts):
            try:
                return outcome_from_message(await client.messages.create(**request['params']))
            except Exception as e:  # 429 / 5xx / quá tải: chờ rồi thử lại
                if attempt == attempts - 1:
                    logger.warning('Claude lỗi %s: %s', request['custom_id'], e)
                    return failed(e)
                await asyncio.sleep(4 * (attempt + 1))


async def run_online(requests, concurrency=5):
    semaphore = asyncio.Semaphore(concurrency)
    async with claude.client() as client:
        results = await asyncio.gather(*[_one(client, r, semaphore) for r in requests])
    return {r['custom_id']: res for r, res in zip(requests, results)}


def parse_json(outcome):
    """JSON từ outcome hoặc None (lỗi, hết token, từ chối)."""
    if not outcome or not outcome['ok'] or outcome['stop_reason'] in ('max_tokens', 'refusal'):
        return None
    try:
        data = json.loads(outcome['text'])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
