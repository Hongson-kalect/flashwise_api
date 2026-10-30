import logging

logger = logging.getLogger(__name__)


def best_effort(func, *args, default=None, **kwargs):
    """Gọi thao tác phụ (cache Redis...) mà lỗi không được làm hỏng việc chính: ghi log rồi trả default."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.warning('%s lỗi, bỏ qua: %s', getattr(func, '__name__', func), e)
        return default
