"""
Phần FSRS server cần (app mới là nơi tính lịch ôn bằng ts-fsrs, xem flashwise/docs/learning-design.md).
Tham số mặc định FSRS-6; app và server phải dùng cùng bộ khi so độ khó.
"""
import math

DEFAULT_W = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
    1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


def initial_difficulty(rating, w=DEFAULT_W):
    """Độ khó FSRS của thẻ mới sau lần chấm đầu: D0(G) = w4 - e^(w5 (G - 1)) + 1, kẹp trong 1-10."""
    d0 = w[4] - math.exp(w[5] * (rating - 1)) + 1
    return min(10.0, max(1.0, d0))
