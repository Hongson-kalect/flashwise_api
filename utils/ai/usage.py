"""
Bộ đếm lệnh gọi và token AI theo loại (draft, refine, metadata, translate, brief), ghi vào Redis hash `ai_usage` để đo chi phí thật
(giá theo token nhân với số đo, xem docs). Chỉ đếm, không bao giờ làm hỏng lệnh gọi AI: mọi lỗi ghi Redis đều bị nuốt.

record_claude(kind, response) / record_gemini(kind, response): đọc `usage` của phản hồi. snapshot() trả
{kind: {provider, calls, input, output}}; reset() xoá để đo một đợt mới.
"""
import logging

from utils.redis.client import get_queue_client

logger = logging.getLogger(__name__)
HASH = 'ai_usage'


def _add(kind, provider, tokens_in, tokens_out):
    try:
        redis = get_queue_client()
        redis.hincrby(HASH, f'{kind}|{provider}|calls', 1)
        redis.hincrby(HASH, f'{kind}|{provider}|input', int(tokens_in or 0))
        redis.hincrby(HASH, f'{kind}|{provider}|output', int(tokens_out or 0))
    except Exception as e:  # đếm hỏng không được ảnh hưởng việc tạo từ
        logger.debug('Không ghi được usage AI: %s', e)


def record_claude(kind, response):
    usage = getattr(response, 'usage', None)
    if usage is not None:
        _add(kind, 'claude', getattr(usage, 'input_tokens', 0), getattr(usage, 'output_tokens', 0))


def record_gemini(kind, response):
    usage = getattr(response, 'usage_metadata', None)
    if usage is not None:
        out = (getattr(usage, 'candidates_token_count', 0) or 0) + (getattr(usage, 'thoughts_token_count', 0) or 0)
        _add(kind, 'gemini', getattr(usage, 'prompt_token_count', 0), out)


def snapshot():
    redis = get_queue_client()
    out = {}
    for field, value in redis.hgetall(HASH).items():
        kind, provider, metric = (field.decode() if isinstance(field, bytes) else field).split('|')
        row = out.setdefault(kind, {'provider': provider, 'calls': 0, 'input': 0, 'output': 0})
        row['calls' if metric == 'calls' else metric] = int(value)
    return out


def reset():
    get_queue_client().delete(HASH)
