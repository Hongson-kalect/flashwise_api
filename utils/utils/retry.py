import logging
from functools import wraps

logger = logging.getLogger(__name__)


def retry_async(provider='gemini'):
    """
    Thử lại hàm async khi nhà cung cấp AI báo lỗi tạm thời (429 / 503 / mất kết nối...), chờ theo
    utils/ai/rate_limit.py (retry-after hoặc luỹ thừa 2, tối đa AI_RATE_LIMIT_RETRIES lần, 429 thì đặt thời gian nghỉ chung).
    Lỗi logic (400, 401, 404...) và quá hạn thời gian (AI_GEMINI_TIMEOUT, đã thử hết model) ném ngay.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            from utils.ai.rate_limit import call_with_backoff
            return await call_with_backoff(lambda: func(*args, **kwargs), provider, retry_timeouts=False)
        return wrapper
    return decorator
