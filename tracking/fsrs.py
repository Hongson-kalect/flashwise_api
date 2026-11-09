"""
FSRS phía server: thư viện `fsrs` (py-fsrs, cùng nhóm tác giả với ts-fsrs của app), tham số mặc định FSRS-6.
Server tính lịch ôn khi xác nhận phiên học (tracking/session.py); app dùng ts-fsrs cùng tham số khi offline.
"""
import math

from fsrs import Scheduler

DEFAULT_W = tuple(Scheduler().parameters)

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4

# "Tôi biết từ này": lên thẳng cấp 5 (Easy), nhưng chỉ là lời khai. Ôn kiểm tra sau KNOWN_VERIFY_DAYS ngày bằng nhớ lại thật (không vuốt)
KNOWN_LEVEL = 5
KNOWN_VERIFY_DAYS = 1

# Tham số FSRS riêng của user (tracking/fsrs_params.py) nằm trong user_setting, đồng bộ xuống app như mọi cài đặt
PARAMS_KEY = 'fsrs_params'
PARAMS_META_KEY = 'fsrs_params_meta'


def is_unverified_known(progress):
    """Thẻ "đã biết" chưa qua lần ôn nào: lên cấp 5 chỉ sau 1 lần chấm (học bình thường chỉ lên tối đa cấp 2 ở lần đầu)."""
    return progress.repetitions == 1 and progress.level == KNOWN_LEVEL


def valid_parameters(value):
    """21 số hữu hạn không âm (FSRS-6), độ dốc đường quên w20 trong [0,1; 0,8]."""
    if not isinstance(value, (list, tuple)) or len(value) != len(DEFAULT_W):
        return False
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0 for x in value):
        return False
    return 0.1 <= value[20] <= 0.8


def user_parameters(user):
    """Tham số FSRS của user nếu đã tối ưu và còn hợp lệ, không thì mặc định. Luôn trả tuple 21 số."""
    from user.models import UserSetting
    row = UserSetting.objects.filter(user=user, key=PARAMS_KEY).exclude(is_deleted=True).first()
    value = row.value if row else None
    return tuple(float(x) for x in value) if valid_parameters(value) else DEFAULT_W


def retrievability(stability, elapsed_days, w=DEFAULT_W):
    """Khả năng còn nhớ sau elapsed_days ngày (FSRS-6): R = (1 + f t / S)^-w20, f chọn để R(S) = 0,9."""
    if not stability or stability <= 0:
        return 0.0
    decay = -w[20]
    factor = 0.9 ** (1 / decay) - 1
    return (1 + factor * max(elapsed_days, 0) / stability) ** decay


def initial_difficulty(rating, w=DEFAULT_W):
    """Độ khó FSRS của thẻ mới sau lần chấm đầu: D0(G) = w4 - e^(w5 (G - 1)) + 1, kẹp trong 1-10."""
    d0 = w[4] - math.exp(w[5] * (rating - 1)) + 1
    return min(10.0, max(1.0, d0))
