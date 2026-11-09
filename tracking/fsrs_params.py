"""
Tối ưu tham số FSRS cho từng user từ review_log (is_fsrs = True), bằng fsrs-rs (fsrs_rs_python, bản Rust của nhóm FSRS).

- Mỗi thẻ (original_sense_id) cho các "mục" FSRS: lần chấm 2, 3, ... kèm lịch sử trước đó, delta_t = số ngày học giữa 2 lần chấm
  (qua ngày lúc day_start_hour theo múi giờ user, giống app). fsrs-rs chỉ dùng mục mà lần chấm cuối cách lần trước ít nhất 1 ngày.
- Chống tối ưu quá khớp: chia theo thẻ, 20% thẻ bắt đầu học muộn nhất để kiểm (không đưa vào huấn luyện). Chỉ nhận tham số mới khi
  log loss trên phần kiểm thấp hơn tham số đang dùng ít nhất MIN_GAIN, rồi mới huấn luyện lại trên toàn bộ dữ liệu.
- Kết quả lưu ở user_setting: `fsrs_params` (21 số) và `fsrs_params_meta` (thời điểm, số mục, log loss trước / sau, trạng thái).
  App kéo về qua sync/pull như mọi cài đặt; server dùng trong tracking/session.py (scheduler_for).
"""
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from tracking.fsrs import DEFAULT_W, PARAMS_KEY, PARAMS_META_KEY, valid_parameters
from tracking.learn import DEFAULT_DAY_START_HOUR, setting
from tracking.models import ReviewLog
from user.models import UserProfile, UserSetting

logger = logging.getLogger(__name__)

MIN_ITEMS = 300  # tổng số mục dùng được; dưới đó giữ mặc định (Anki khuyên cỡ 1.000 lần ôn)
MIN_TEST_ITEMS = 60
MIN_NEW_ITEMS = 100  # số mục tăng thêm so với lần tối ưu trước thì mới tối ưu lại
MIN_GAIN = 0.002  # log loss phần kiểm phải giảm ít nhất chừng này
HOLDOUT = 0.2


def _zone(user):
    profile = UserProfile.objects.filter(user=user).first()
    try:
        return ZoneInfo(profile.time_zone if profile else 'UTC')
    except Exception:
        return ZoneInfo('UTC')


def card_histories(user):
    """[(thời điểm chấm đầu, [(rating, chỉ số ngày học), ...])]: mỗi thẻ một mục, các lần chấm theo thứ tự thời gian."""
    zone = _zone(user)
    start_hour = setting(user, 'day_start_hour', DEFAULT_DAY_START_HOUR, 0, 23)
    rows = (ReviewLog.objects.filter(user=user, is_fsrs=True)
            .order_by('original_sense_id', 'reviewed_at').values_list('original_sense_id', 'rating', 'reviewed_at'))
    cards, current, last_key = [], [], None
    for key, rating, at in rows.iterator():
        day = (at.astimezone(zone) - timedelta(hours=start_hour)).date().toordinal()
        if key != last_key and current:
            cards.append(current)
            current = []
        last_key = key
        current.append((at, rating, day))
    if current:
        cards.append(current)
    return [(c[0][0], [(r, d) for _, r, d in c]) for c in cards]


def items_of(history):
    """Các mục FSRS (fsrs_rs_python.FSRSItem) của 1 thẻ. Bỏ mục có lần chấm cuối cùng ngày với lần trước (fsrs-rs không dùng được)."""
    import fsrs_rs_python as rs
    reviews, items = [], []
    for k, (rating, day) in enumerate(history):
        delta = 0 if k == 0 else max(0, day - history[k - 1][1])
        reviews.append(rs.FSRSReview(rating=rating, delta_t=delta))
        if k >= 1 and delta > 0:
            items.append(rs.FSRSItem(reviews=list(reviews)))
    return items


def current_parameters(user):
    row = UserSetting.objects.filter(user=user, key=PARAMS_KEY).exclude(is_deleted=True).first()
    return list(row.value) if row and valid_parameters(row.value) else list(DEFAULT_W)


def current_meta(user):
    row = UserSetting.objects.filter(user=user, key=PARAMS_META_KEY).exclude(is_deleted=True).first()
    return row.value if row and isinstance(row.value, dict) else {}


def _save(user, key, value):
    row, _ = UserSetting.objects.get_or_create(user=user, key=key)
    row.value, row.client_updated_at, row.is_deleted = value, timezone.now(), False
    row.save()


def _loss(parameters, items):
    import fsrs_rs_python as rs
    return rs.FSRS(parameters=list(parameters)).evaluate(items).log_loss


def fit_user(user, force=False):
    """
    Tối ưu và (nếu tốt hơn) lưu tham số FSRS của user. Trả dict {status, n_items, loss_before?, loss_after?}.
    status: not_enough (ít dữ liệu), up_to_date (chưa có thêm dữ liệu mới), kept (tham số mới không tốt hơn), updated.
    """
    import fsrs_rs_python as rs
    meta = current_meta(user)
    per_card = sorted(((first, items_of(h)) for first, h in card_histories(user)), key=lambda x: x[0])
    per_card = [(first, items) for first, items in per_card if items]
    n_items = sum(len(items) for _, items in per_card)
    if n_items < MIN_ITEMS:
        return {'status': 'not_enough', 'n_items': n_items, 'min_items': MIN_ITEMS}
    if not force and meta.get('status') in ('updated', 'kept') and n_items - int(meta.get('n_items') or 0) < MIN_NEW_ITEMS:
        return {'status': 'up_to_date', 'n_items': n_items}

    split = max(1, int(len(per_card) * (1 - HOLDOUT)))
    train = [it for _, items in per_card[:split] for it in items]
    test = [it for _, items in per_card[split:] for it in items]
    if len(test) < MIN_TEST_ITEMS or len(train) < MIN_ITEMS * 0.6:
        return {'status': 'not_enough', 'n_items': n_items, 'min_items': MIN_ITEMS}

    base = current_parameters(user)
    candidate = list(rs.FSRS(parameters=base).compute_parameters(train))
    now = timezone.now().isoformat()
    if not valid_parameters(candidate):
        _save(user, PARAMS_META_KEY, {**meta, 'checked_at': now, 'n_items': n_items, 'status': 'kept'})
        return {'status': 'kept', 'n_items': n_items}
    loss_base, loss_new = _loss(base, test), _loss(candidate, test)
    if loss_new > loss_base - MIN_GAIN:
        _save(user, PARAMS_META_KEY, {**meta, 'checked_at': now, 'n_items': n_items, 'status': 'kept', 'loss_before': loss_base, 'loss_after': loss_new})
        return {'status': 'kept', 'n_items': n_items, 'loss_before': loss_base, 'loss_after': loss_new}

    final = list(rs.FSRS(parameters=base).compute_parameters(train + test))
    if not valid_parameters(final):
        final = candidate
    _save(user, PARAMS_KEY, [round(float(x), 4) for x in final])
    _save(user, PARAMS_META_KEY, {'fitted_at': now, 'checked_at': now, 'n_items': n_items, 'status': 'updated', 'loss_before': loss_base, 'loss_after': loss_new})
    logger.info('FSRS user %s: %d mục, log loss kiểm %.4f -> %.4f', user.pk, n_items, loss_base, loss_new)
    return {'status': 'updated', 'n_items': n_items, 'loss_before': loss_base, 'loss_after': loss_new}
