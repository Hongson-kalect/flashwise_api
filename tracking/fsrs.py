"""
FSRS phía server: thư viện `fsrs` (py-fsrs, cùng nhóm tác giả với ts-fsrs của app), tham số mặc định FSRS-6.
Server tính lịch ôn khi xác nhận phiên học (tracking/session.py); app dùng ts-fsrs cùng tham số khi offline.
"""
import math

from fsrs import Scheduler

DEFAULT_W = tuple(Scheduler().parameters)

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


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
