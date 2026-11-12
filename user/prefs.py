"""Tuỳ chọn người dùng mà server cần biết (đọc từ user_settings, app đồng bộ lên)."""
from user.models import UserSetting

SHOW_OFFENSIVE_KEY = 'show_offensive'


def allow_offensive(user):
    """Người dùng đã bật hiện nội dung thô tục (mặc định tắt): gợi ý từ / bộ từ chứa nghĩa thô tục chỉ hiện cho người đã bật."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    row = UserSetting.objects.filter(user=user, key=SHOW_OFFENSIVE_KEY).exclude(is_deleted=True).first()
    return bool(row and row.value is True)
