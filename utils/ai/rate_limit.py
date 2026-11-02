"""
Xử lý khi nhà cung cấp AI báo quá hạn mức / quá tải (429, 529, 5xx, mất kết nối).

- Lỗi tạm thời thì chờ rồi thử lại (theo retry-after nếu có, không thì luỹ thừa 2 có trần), tối đa AI_RATE_LIMIT_RETRIES lần.
- Mỗi lần gặp 429 / quá tải thì đặt "thời gian nghỉ" chung cho nhà cung cấp đó trong Redis (ai_cooldown:<provider>):
  worker thấy còn thời gian nghỉ thì tạm không nhận job tạo từ mới, tránh dồn thêm request vào lúc đang bị chặn.
"""
import asyncio
import logging
import random

from django.conf import settings

logger = logging.getLogger(__name__)

COOLDOWN_KEY = 'ai_cooldown:{}'
TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def status_of(error):
    """Mã HTTP của lỗi SDK (anthropic: status_code, google-genai: code), None nếu không có."""
    for name in ('status_code', 'code'):
        value = getattr(error, name, None)
        if isinstance(value, int):
            return value
    return None


def is_transient(error):
    """Lỗi đáng thử lại: hạn mức, quá tải, lỗi máy chủ, mất kết nối / quá hạn. Lỗi logic (400, 401, 404) thì không."""
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    status = status_of(error)
    if status is not None:
        return status in TRANSIENT_STATUS
    name = type(error).__name__
    if name in ('APIConnectionError', 'APITimeoutError', 'RateLimitError', 'OverloadedError', 'InternalServerError'):
        return True
    text = str(error)
    return any(mark in text for mark in ('429', '503', 'RESOURCE_EXHAUSTED', 'UNAVAILABLE', 'overloaded'))


def is_rate_limited(error):
    status = status_of(error)
    return status in (429, 529) or type(error).__name__ in ('RateLimitError', 'OverloadedError') or 'RESOURCE_EXHAUSTED' in str(error)


def retry_after(error):
    """Số giây nhà cung cấp bảo chờ (header retry-after), None nếu không có."""
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', None) or {}
    try:
        value = float(headers.get('retry-after'))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def backoff_seconds(error, attempt):
    """Thời gian chờ lần thử thứ attempt (1, 2, ...): retry-after nếu có, không thì base * 2^(attempt-1), có trần + nhiễu."""
    wait = retry_after(error)
    if wait is None:
        wait = settings.AI_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return min(wait, settings.AI_BACKOFF_MAX_SECONDS) + random.uniform(0, 1)


def _redis():
    from utils.redis.client import get_queue_client
    return get_queue_client()


def set_cooldown(provider, seconds):
    """Đặt thời gian nghỉ chung (không rút ngắn thời gian nghỉ đang có). Lỗi Redis thì bỏ qua."""
    try:
        ms = int(seconds * 1000)
        client = _redis()
        if client.pttl(COOLDOWN_KEY.format(provider)) < ms:
            client.set(COOLDOWN_KEY.format(provider), 1, px=ms)
    except Exception as e:
        logger.warning('Không đặt được thời gian nghỉ %s: %s', provider, e)


def cooldown_remaining(provider):
    """Số giây nghỉ còn lại của nhà cung cấp (0 nếu không nghỉ hoặc Redis lỗi)."""
    try:
        ttl = _redis().pttl(COOLDOWN_KEY.format(provider))
    except Exception:
        return 0
    return max(ttl, 0) / 1000


async def wait_before_retry(error, attempt, provider, retry_timeouts=True):
    """
    Gặp lỗi tạm thời lần thứ attempt: True nếu nên thử lại (đã chờ xong), False nếu hết lượt hoặc lỗi không thử lại.
    retry_timeouts=False: quá hạn thời gian thì thôi (lệnh gọi treo đã tốn trọn thời gian chờ, thử lại chỉ kéo dài thêm).
    """
    if not is_transient(error) or attempt > settings.AI_RATE_LIMIT_RETRIES:
        return False
    if not retry_timeouts and isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return False
    wait = backoff_seconds(error, attempt)
    if is_rate_limited(error):
        set_cooldown(provider, wait)
    logger.warning('%s lỗi tạm thời (%s), chờ %.1fs rồi thử lại lần %s', provider, status_of(error) or type(error).__name__, wait, attempt)
    await asyncio.sleep(wait)
    return True


async def call_with_backoff(func, provider, retry_timeouts=True):
    """Gọi await func(), lỗi tạm thời thì chờ và gọi lại; hết lượt thì ném lỗi cuối."""
    attempt = 0
    while True:
        try:
            return await func()
        except Exception as e:
            attempt += 1
            if not await wait_before_retry(e, attempt, provider, retry_timeouts):
                raise
