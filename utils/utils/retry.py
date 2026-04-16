import asyncio
import logging
from functools import wraps

logger = logging.getLogger(__name__)

def retry_async(max_retries=4, initial_delay=2, backoff_factor=2):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # Kiểm tra nếu là lỗi 503 hoặc các lỗi mạng tạm thời
                    if "503" in str(e) or "quota" in str(e).lower() or attempt == max_retries:
                        if attempt == max_retries:
                            logger.error(f"Thất bại sau {max_retries} lần thử. Lỗi: {e}")
                            raise e
                        
                        logger.warning(f"Lần thử {attempt} thất bại (503). Đang đợi {delay}s để thử lại...")
                        await asyncio.sleep(delay)
                        delay *= backoff_factor # Đợi 2s, 4s, 8s...
                    else:
                        # Nếu là lỗi logic (404, 401...) thì raise luôn không retry
                        raise e
        return wrapper
    return decorator