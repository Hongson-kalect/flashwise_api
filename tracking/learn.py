"""
Chọn thẻ cho phiên học, theo flashwise/docs/learning-design.md mục 5. Hai việc tách riêng:
- Học mới (GET /api/learn/new/): `count` nghĩa chưa có tiến độ của 1 bộ, theo thứ tự trong bộ, bỏ nghĩa ẩn,
  không 2 nghĩa cùng 1 từ trong 1 phiên (nghĩa còn lại của từ đó sang phiên sau).
- Ôn tập (GET /api/learn/review/): thẻ có next_review_at trước mốc qua ngày (day_start_hour theo múi giờ user),
  của 1 bộ hoặc mọi nghĩa đang học, khả năng còn nhớ thấp nhất trước. Mỗi kiểu ôn (REVIEW_MODES) có số thẻ / cấp riêng.
Tiến độ lấy theo bản đã sync lên server; app offline tự chọn bằng cùng luật trên SQLite.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from ai.models import AISense
from ai.queries import hidden_sense_ids
from core.queries import live_items
from tracking.fsrs import retrievability
from tracking.models import UserSenseProgress
from user.models import UserProfile, UserSetting

DEFAULT_DAY_START_HOUR = 4
DEFAULT_SWIPE_MIN_LEVEL = 3

# Kiểu học mới / ôn: số thẻ mặc định, tối đa, cấp tối thiểu (None = không lọc; tên setting = lấy theo cài đặt user)
NEW_MODES = {
    'normal': {'count': 6, 'max': 50},
}
REVIEW_MODES = {
    'normal': {'count': 6, 'max': 50, 'min_level': None},  # như học mới: đi qua các bước theo cấp
    'quick': {'count': 30, 'max': 200, 'min_level': 'swipe_min_level'},  # vuốt / đọc: nhanh nên lấy nhiều thẻ, chỉ thẻ đã đủ cấp
}


def setting(user, key, default, lo, hi):
    row = UserSetting.objects.filter(user=user, key=key).exclude(is_deleted=True).first()
    value = row.value if row else None
    return value if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi else default


def day_ends_at(user, now=None):
    """Mốc qua ngày học tiếp theo: day_start_hour giờ địa phương của user (hôm nay nếu chưa tới, không thì ngày mai)."""
    now = now or timezone.now()
    profile = UserProfile.objects.filter(user=user).first()
    try:
        zone = ZoneInfo(profile.time_zone if profile else 'UTC')
    except Exception:
        zone = ZoneInfo('UTC')
    local = now.astimezone(zone)
    boundary = local.replace(hour=setting(user, 'day_start_hour', DEFAULT_DAY_START_HOUR, 0, 23), minute=0, second=0, microsecond=0)
    if local >= boundary:
        boundary += timedelta(days=1)
    return boundary


def min_level_for(user, mode):
    rule = REVIEW_MODES[mode]['min_level']
    if rule == 'swipe_min_level':
        return setting(user, 'swipe_min_level', DEFAULT_SWIPE_MIN_LEVEL, 0, 7)
    return rule or 0


def original_key(item):
    """Tiến độ học gắn với nghĩa gốc (user_sense_progress.original_sense_id)."""
    return str(item.original_id or item.sense_id)


def is_new(progress):
    return progress is None or (progress.state == 0 and not progress.repetitions)


def collection_cards(user, user_collection):
    """Dòng bộ từ user đang thấy, bỏ nghĩa đã ẩn, theo thứ tự hiển thị: {original_key: dòng}."""
    items = live_items(user_collection.collection_id, user_collection.version, user)
    hidden = hidden_sense_ids(user, {i.sense_id for i in items} | {i.original_id for i in items if i.original_id})
    return {original_key(i): i for i in items if str(i.sense_id) not in hidden and str(i.original_id) not in hidden}


def pick_new(user, user_collection, count):
    """Trả (các dòng nghĩa mới, new_available = số nghĩa mới còn lại trong bộ, tính cả nghĩa bị dời vì trùng từ)."""
    cards = collection_cards(user, user_collection)
    learned = {
        str(p.original_sense_id) for p in UserSenseProgress.objects.filter(user=user, original_sense_id__in=list(cards)).exclude(is_deleted=True)
        if not is_new(p)
    }
    picked, words, available = [], set(), 0
    for key, item in cards.items():
        if key in learned:
            continue
        available += 1
        if len(picked) < count and item.sense.word_id not in words:
            words.add(item.sense.word_id)
            picked.append(item)
    return picked, available


def resolve_senses(user, originals):
    """Nghĩa để ôn khi không theo bộ: bản riêng mới nhất của user nếu có, không thì chính nghĩa gốc. {original: sense_id}."""
    resolved = {key: key for key in originals}
    customs = (
        AISense.objects.filter(original_id__in=list(originals), created_by=user, is_official=False)
        .exclude(is_deleted=True).exclude(is_active=False).order_by('original_id', '-id').distinct('original_id')
    )
    for sense in customs:
        resolved[str(sense.original_id)] = str(sense.id)
    return resolved


def pick_review(user, mode, count, user_collection=None, now=None):
    """
    Thẻ đến hạn ôn, khả năng còn nhớ (R) thấp nhất trước. Theo bộ thì chỉ nghĩa đang có trong bộ, không thì mọi nghĩa đang học.
    Trả (các thẻ [(tiến độ, sense_id, dòng bộ từ hoặc None, R)] tối đa count, due_total, due_count, day_ends_at):
    due_total = mọi thẻ đến hạn, due_count = thẻ đến hạn hợp với kiểu ôn (đủ cấp).
    """
    now = now or timezone.now()
    cutoff = day_ends_at(user, now)
    progresses = UserSenseProgress.objects.filter(user=user, next_review_at__lt=cutoff, is_hidden=False, is_avoid=False).exclude(is_deleted=True)
    cards = None
    if user_collection is not None:
        cards = collection_cards(user, user_collection)
        progresses = progresses.filter(original_sense_id__in=list(cards))
    progresses = [p for p in progresses if not is_new(p)]
    if cards is None:
        hidden = hidden_sense_ids(user, [p.original_sense_id for p in progresses])
        progresses = [p for p in progresses if str(p.original_sense_id) not in hidden]

    min_level = min_level_for(user, mode)
    eligible = []
    for p in progresses:
        if p.level < min_level:
            continue
        elapsed = (now - p.last_reviewed_at).total_seconds() / 86400 if p.last_reviewed_at else 0
        eligible.append((p, retrievability(p.stability, elapsed)))
    eligible.sort(key=lambda entry: entry[1])
    chosen = eligible[:count]

    keys = [str(p.original_sense_id) for p, _ in chosen]
    senses = {key: str(cards[key].sense_id) for key in keys} if cards is not None else resolve_senses(user, keys)
    picked = [(p, senses[str(p.original_sense_id)], cards[str(p.original_sense_id)] if cards is not None else None, r) for p, r in chosen]
    return picked, len(progresses), len(eligible), cutoff
