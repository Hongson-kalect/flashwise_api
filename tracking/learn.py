"""
Chọn thẻ cho 1 phiên học của 1 bộ từ (GET /api/learn/session/), theo flashwise/docs/learning-design.md mục 5:
- Thẻ đến hạn ôn trước (học lại > học mới): next_review_at trước mốc qua ngày (day_start_hour theo múi giờ user),
  không giới hạn số lượng, khả năng còn nhớ thấp nhất đứng đầu.
- Rồi `count` nghĩa mới theo thứ tự trong bộ: chưa có tiến độ, chưa ẩn, không 2 nghĩa cùng 1 từ trong 1 phiên
  (nghĩa còn lại của từ đó sang phiên sau).
Tiến độ lấy theo bản đã sync lên server; app offline tự chọn bằng cùng luật trên SQLite.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from ai.queries import hidden_sense_ids
from core.queries import live_items
from tracking.fsrs import retrievability
from tracking.models import UserSenseProgress
from user.models import UserProfile, UserSetting

DEFAULT_DAY_START_HOUR = 4


def day_start_hour(user):
    row = UserSetting.objects.filter(user=user, key='day_start_hour').exclude(is_deleted=True).first()
    value = row.value if row else None
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 23 else DEFAULT_DAY_START_HOUR


def day_ends_at(user, now=None):
    """Mốc qua ngày học tiếp theo: day_start_hour giờ địa phương của user (hôm nay nếu chưa tới, không thì ngày mai)."""
    now = now or timezone.now()
    profile = UserProfile.objects.filter(user=user).first()
    try:
        zone = ZoneInfo(profile.time_zone if profile else 'UTC')
    except Exception:
        zone = ZoneInfo('UTC')
    local = now.astimezone(zone)
    boundary = local.replace(hour=day_start_hour(user), minute=0, second=0, microsecond=0)
    if local >= boundary:
        boundary += timedelta(days=1)
    return boundary


def original_key(item):
    """Tiến độ học gắn với nghĩa gốc (user_sense_progress.original_sense_id)."""
    return str(item.original_id or item.sense_id)


def is_new(progress):
    return progress is None or (progress.state == 0 and not progress.repetitions)


def pick_session(user, user_collection, count, now=None):
    """
    Trả (due, new, stats): due / new là danh sách (dòng bộ từ, tiến độ, R) đã sắp; stats gồm due_count,
    new_available (số nghĩa mới còn lại trong bộ, tính cả nghĩa bị dời vì trùng từ) và day_ends_at.
    """
    now = now or timezone.now()
    cutoff = day_ends_at(user, now)
    items = live_items(user_collection.collection_id, user_collection.version, user)
    hidden = hidden_sense_ids(user, {i.sense_id for i in items} | {i.original_id for i in items if i.original_id})
    items = [i for i in items if str(i.sense_id) not in hidden and str(i.original_id) not in hidden]

    progresses = {
        str(p.original_sense_id): p
        for p in UserSenseProgress.objects.filter(user=user, original_sense_id__in=[original_key(i) for i in items]).exclude(is_deleted=True)
    }

    due, fresh, words = [], [], set()
    new_available = 0
    for item in items:
        progress = progresses.get(original_key(item))
        if is_new(progress):
            new_available += 1
            if len(fresh) < count and item.sense.word_id not in words:
                words.add(item.sense.word_id)
                fresh.append((item, progress, None))
            continue
        if progress.is_hidden or progress.is_avoid or not progress.next_review_at or progress.next_review_at >= cutoff:
            continue
        elapsed = (now - progress.last_reviewed_at).total_seconds() / 86400 if progress.last_reviewed_at else 0
        due.append((item, progress, retrievability(progress.stability, elapsed)))

    due.sort(key=lambda entry: entry[2])
    return due, fresh, {'due_count': len(due), 'new_available': new_available, 'day_ends_at': cutoff}
