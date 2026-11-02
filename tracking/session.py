"""
Xác nhận phiên học (flashwise/docs/learning-design.md mục 5), tách 2 việc:
- Học mới (POST /api/learn/new/complete/): khởi tạo tiến độ từng nghĩa. Thẻ FSRS mới theo điểm chấm, độ khó ban đầu
  trộn với độ khó chung của nghĩa (7 phần phiên / tối đa 3 phần toàn server, ít người học thì giảm), cấp 1 (Easy 2,
  Again 0, "Đã biết" 5).
- Ôn tập (POST /api/learn/review/complete/): chấm tiếp thẻ đã có, kiểu quick (vuốt / đọc) dùng mục tiêu nhớ cao hơn
  (swipe_retention) và chỉ đưa cấp tối đa tới 5.
Cả 2: ghi review_log (bước trong phiên is_fsrs=0, lần chấm FSRS is_fsrs=1), learn_session, cộng thống kê chung của nghĩa,
day_learn_log (theo ngày học của user) rồi tính lại streak / XP. Gửi lại cùng id phiên (mạng chập chờn, hàng đợi offline)
không áp lần 2: trả kết quả đang có với duplicate=true.
"""
import uuid
from datetime import timedelta, timezone as dt_timezone

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from fsrs import Card, Rating, Scheduler, State

from ai.queries import visible_senses
from ai.validators import is_uuid
from core.models import UserCollection
from interact.models import LearnSession
from tracking.fsrs import AGAIN, DEFAULT_W, EASY, HARD, initial_difficulty
from tracking.learn import NEW_MODES, REVIEW_MODES, collection_cards, day_ends_at, is_new, setting
from tracking.models import DayLearnLog, ReviewLog, UserSenseProgress
from tracking.stats import apply_review_stats
from tracking.sync import PULL_TABLES, _clean, refresh_stats
from user.models import UserSetting

SESSION_LOCK_NAMESPACE = 7302
MAX_CARDS = 200
MAX_STEPS = 20
MAX_LEVEL = 7
KNOWN_LEVEL = 5  # "Đã biết" lên thẳng cấp này
QUICK_MAX_LEVEL = 5  # vuốt / đọc chỉ đưa tới cấp này, cao hơn phải qua nhớ lại thật (gõ / nói)
NEW_LEVEL = {AGAIN: 0, HARD: 1, 3: 1, EASY: 2}

# Độ khó chung của nghĩa chiếm tối đa 3 phần, giảm theo số người học: w = 0,3 n / (n + 20), dưới 5 người thì bỏ
GLOBAL_WEIGHT = 0.3
GLOBAL_PRIOR = 20
GLOBAL_MIN_LEARNERS = 5

# XP tạm, chỉnh khi test thật
XP_NEW = 10
XP_KNOWN = 2
XP_REVIEW = {'normal': 5, 'quick': 2}

ENABLE_FUZZING = True  # rải ngày ôn ±vài % để thẻ học cùng ngày không dồn về cùng 1 ngày (test tắt đi)

LOG_SPEC = {
    'rating': (int, 1, 4), 'mode': ('text', 40), 'reviewed_at': ('time',),
    'hint_count': (int, 0, 100), 'attempts': (int, 0, 100), 'duration_ms': (int, 0, 3600000),
}
SESSION_SPEC = {'start_at': ('time',), 'end_at': ('time',), 'time': (int, 0, 86400)}


class SessionError(ValueError):
    pass


def float_setting(user, key, default, lo, hi):
    row = UserSetting.objects.filter(user=user, key=key).exclude(is_deleted=True).first()
    value = row.value if row else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi else default


def scheduler_for(user, mode):
    """Lịch FSRS của user: bước học cùng ngày first_review_hours (mặc định 6h), mục tiêu nhớ theo kiểu học."""
    hours = setting(user, 'first_review_hours', 6, 1, 24)
    if mode == 'quick':
        retention = float_setting(user, 'swipe_retention', 0.93, 0.7, 0.99)
    else:
        retention = float_setting(user, 'request_retention', 0.9, 0.7, 0.99)
    return Scheduler(
        desired_retention=retention,
        learning_steps=(timedelta(minutes=10), timedelta(hours=hours)),
        relearning_steps=(timedelta(hours=hours),),
        enable_fuzzing=ENABLE_FUZZING,
    )


def blended_difficulty(session_difficulty, sense):
    n = sense.learner_count or 0
    if n < GLOBAL_MIN_LEARNERS or not sense.global_difficulty:
        return session_difficulty
    weight = GLOBAL_WEIGHT * n / (n + GLOBAL_PRIOR)
    return (1 - weight) * session_difficulty + weight * sense.global_difficulty


def next_level(level, rating, mode):
    if rating == AGAIN:
        return max(0, level - 2)
    if rating == HARD:
        return level
    up = level + 1
    if mode == 'quick':
        up = min(up, max(level, QUICK_MAX_LEVEL))
    return min(up, MAX_LEVEL)


def card_from_progress(progress, now):
    """Thẻ fsrs từ tiến độ đã lưu. Dòng cũ / app gửi thiếu độ bền, độ khó thì lấy giá trị của thẻ mới chấm Good."""
    state = State(progress.state) if progress.state in (1, 2, 3) else State.Learning
    return Card(
        state=state,
        step=progress.learning_steps if state != State.Review else None,
        stability=progress.stability or DEFAULT_W[2],
        difficulty=progress.difficulty or initial_difficulty(3),
        due=progress.next_review_at or now,
        last_review=progress.last_reviewed_at,
    )


# ---------------------------------------------------------------- đọc dữ liệu

def parse_session(raw, now):
    if not isinstance(raw, dict) or not is_uuid(raw.get('id')):
        raise SessionError('session.id')
    try:
        data = _clean(raw, SESSION_SPEC)
    except ValueError as e:
        raise SessionError(f'session.{e}')
    collection_id = raw.get('collection_id')
    if collection_id is not None and not is_uuid(collection_id):
        raise SessionError('session.collection_id')
    methods = raw.get('methods', [])
    if not isinstance(methods, list) or len(methods) > 20 or not all(isinstance(m, str) and 0 < len(m) <= 40 for m in methods):
        raise SessionError('session.methods')
    data['end_at'] = min(data.get('end_at') or now, now + timedelta(minutes=5))
    return {'id': uuid.UUID(str(raw['id'])), 'collection_id': collection_id, 'methods': methods, **data}


def parse_card(raw, now, new):
    """Raise SessionError(lý do) nếu sai. Thẻ: sense_id, original_sense_id, log_id, rating, steps[...] (+ known khi học mới)."""
    if not isinstance(raw, dict):
        raise SessionError('card')
    for name in ('sense_id', 'original_sense_id', 'log_id'):
        if not is_uuid(raw.get(name)):
            raise SessionError(name)
    known = bool(raw.get('known')) if new else False
    raw = {**raw, 'rating': EASY} if known else raw
    if 'rating' not in raw:
        raise SessionError('rating')
    try:
        data = _clean(raw, LOG_SPEC)
    except ValueError as e:
        raise SessionError(f'invalid:{e}')
    steps = raw.get('steps', [])
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise SessionError('steps')
    clean_steps = []
    for step in steps:
        if not isinstance(step, dict) or not is_uuid(step.get('id')) or 'rating' not in step:
            raise SessionError('steps.id, steps.rating')
        try:
            clean_steps.append({'id': step['id'], **_clean(step, LOG_SPEC)})
        except ValueError as e:
            raise SessionError(f'invalid:steps.{e}')
    # fsrs chỉ nhận giờ UTC
    data['reviewed_at'] = min(data.get('reviewed_at') or now, now + timedelta(minutes=5)).astimezone(dt_timezone.utc)
    for step in clean_steps:
        if step.get('reviewed_at'):
            step['reviewed_at'] = step['reviewed_at'].astimezone(dt_timezone.utc)
    return {
        'sense_id': str(raw['sense_id']), 'original_sense_id': str(uuid.UUID(str(raw['original_sense_id']))),
        'log_id': raw['log_id'], 'known': known, 'steps': clean_steps, **data,
    }


# ---------------------------------------------------------------- xử lý

def complete_session(user, kind, mode, body):
    """
    kind: 'learn' (học mới) | 'review' (ôn tập). Trả dict kết quả; raise SessionError nếu body / phiên sai.
    Thẻ sai không chặn thẻ khác (nằm trong rejected).
    """
    now = timezone.now()
    modes = NEW_MODES if kind == 'learn' else REVIEW_MODES
    if mode not in modes:
        raise SessionError('mode')
    session_data = parse_session(body.get('session'), now)
    raw_cards = body.get('cards')
    if not isinstance(raw_cards, list) or len(raw_cards) > MAX_CARDS:
        raise SessionError('cards')

    with transaction.atomic():
        # Khoá theo user: gửi lại cùng phiên song song không áp 2 lần
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [SESSION_LOCK_NAMESPACE, user.pk])
        existing = LearnSession.objects.filter(pk=session_data['id']).first()
        if existing is not None and existing.user_id != user.pk:
            raise SessionError('session.id_taken')
        if existing is not None and existing.status == 'completed':
            originals = [str(c.get('original_sense_id')) for c in raw_cards if isinstance(c, dict) and is_uuid(c.get('original_sense_id'))]
            return {'duplicate': True, 'session': session_row(existing), 'progress': progress_rows(user, originals), 'rejected': []}
        result = _apply(user, kind, mode, session_data, raw_cards, now, existing)

    refresh_stats(user)
    return result


def _apply(user, kind, mode, session_data, raw_cards, now, existing):
    new = kind == 'learn'
    rejected, cards, seen = [], [], set()
    for raw in raw_cards:
        try:
            card = parse_card(raw, now, new)
        except SessionError as e:
            rejected.append({'sense_id': raw.get('sense_id') if isinstance(raw, dict) else None, 'reason': str(e)})
            continue
        if card['original_sense_id'] in seen:
            rejected.append({'sense_id': card['sense_id'], 'reason': 'duplicate_card'})
            continue
        seen.add(card['original_sense_id'])
        cards.append(card)

    senses = {str(s.id): s for s in visible_senses(user).filter(id__in=[c['sense_id'] for c in cards])}
    progresses = {
        str(p.original_sense_id): p
        for p in UserSenseProgress.objects.select_for_update().filter(user=user, original_sense_id__in=[c['original_sense_id'] for c in cards]).exclude(is_deleted=True)
    }
    scheduler = scheduler_for(user, mode)
    logs, fsrs_logs, done = [], [], []
    xp = learned = reviewed = 0

    for card in sorted(cards, key=lambda c: c['reviewed_at']):
        sense = senses.get(card['sense_id'])
        if sense is None or str(sense.original_id or sense.id) != card['original_sense_id']:
            rejected.append({'sense_id': card['sense_id'], 'reason': 'sense'})
            continue
        progress = progresses.get(card['original_sense_id'])
        at = card['reviewed_at']
        if new:
            if not is_new(progress):
                rejected.append({'sense_id': card['sense_id'], 'reason': 'already_learned'})
                continue
            progress = progress or UserSenseProgress(user=user, original_sense_id=card['original_sense_id'])
            level_before = 0
            fsrs_card, _ = scheduler.review_card(Card(), Rating(card['rating']), at)
            fsrs_card.difficulty = blended_difficulty(fsrs_card.difficulty, sense)
            progress.level = KNOWN_LEVEL if card['known'] else NEW_LEVEL[card['rating']]
            elapsed = 0
            learned += 1
            xp += XP_KNOWN if card['known'] else XP_NEW
        else:
            if is_new(progress):
                rejected.append({'sense_id': card['sense_id'], 'reason': 'not_learned'})
                continue
            if progress.last_reviewed_at and at < progress.last_reviewed_at:
                rejected.append({'sense_id': card['sense_id'], 'reason': 'stale'})  # máy khác đã chấm sau thời điểm này
                continue
            level_before = progress.level
            elapsed = (at - progress.last_reviewed_at).total_seconds() / 86400 if progress.last_reviewed_at else 0
            was_review = progress.state == State.Review.value
            fsrs_card, _ = scheduler.review_card(card_from_progress(progress, now), Rating(card['rating']), at)
            progress.level = next_level(progress.level, card['rating'], mode)
            if card['rating'] == AGAIN and was_review:
                progress.forget_count += 1
            reviewed += 1
            xp += XP_REVIEW.get(mode, XP_REVIEW['normal'])

        good = card['rating'] != AGAIN
        progress.state = fsrs_card.state.value
        progress.stability = fsrs_card.stability
        progress.difficulty = fsrs_card.difficulty
        progress.learning_steps = fsrs_card.step or 0
        progress.next_review_at = fsrs_card.due
        progress.scheduled_days = max(0.0, (fsrs_card.due - at).total_seconds() / 86400)
        progress.last_reviewed_at = progress.last_seen_at = at
        progress.repetitions += 1
        progress.remember_count += 1 if good else 0
        progress.streak_correct = progress.streak_correct + 1 if good else 0
        progress.failed_at = progress.failed_at if good else at
        progress.max_level = max(progress.max_level, progress.level)
        progress.is_mastered = progress.level >= MAX_LEVEL
        progress.client_updated_at = now
        progress.save()
        done.append(card['original_sense_id'])

        common = {'user': user, 'original_sense_id': card['original_sense_id'], 'sense_id': card['sense_id'], 'session_id': session_data['id'], 'level': level_before}
        for step in card['steps']:
            logs.append(ReviewLog(id=step['id'], is_fsrs=False, reviewed_at=step.get('reviewed_at') or at, **common,
                                  **{k: v for k, v in step.items() if k not in ('id', 'reviewed_at')}))
        fsrs_log = ReviewLog(
            id=card['log_id'], is_fsrs=True, rating=card['rating'], reviewed_at=at,
            mode=card.get('mode') or ('learn:known' if card['known'] else f'{kind}:{mode}'),
            hint_count=card.get('hint_count', 0), attempts=card.get('attempts', 1), duration_ms=card.get('duration_ms', 0),
            state=progress.state, elapsed_days=elapsed, scheduled_days=progress.scheduled_days,
            stability=progress.stability, difficulty=progress.difficulty, **common,
        )
        logs.append(fsrs_log)
        fsrs_logs.append(fsrs_log)

    # review_log chỉ thêm; dòng đã có (app push trước qua sync) không cộng thống kê lần 2
    taken = set(ReviewLog.objects.filter(id__in=[log.id for log in logs]).values_list('id', flat=True))
    ReviewLog.objects.bulk_create(logs, ignore_conflicts=True)
    apply_review_stats(user, [log for log in fsrs_logs if uuid.UUID(str(log.id)) not in taken])

    correct = sum(1 for log in logs if log.rating != AGAIN)
    session = existing or LearnSession(id=session_data['id'], user=user)
    session.type, session.mode, session.status = kind, mode, 'completed'
    session.start_at = session_data.get('start_at') or min((c['reviewed_at'] for c in cards), default=session_data['end_at'])
    session.end_at = session_data['end_at']
    session.time = session_data.get('time', 0)
    session.collection_id = session_data['collection_id']
    session.methods = session_data['methods']
    session.senses = done
    session.sense_count = len(done)
    session.new_count, session.review_count = learned, reviewed
    session.correct_count, session.wrong_count = correct, len(logs) - correct
    session.xp_earned = xp
    session.save()

    add_day_log(user, session)
    if new and session.collection_id:
        refresh_learned_count(user, session.collection_id)
    return {'duplicate': False, 'session': session_row(session), 'progress': progress_rows(user, done), 'rejected': rejected}


def add_day_log(user, session):
    """Cộng phiên vào day_learn_log của ngày học (qua ngày lúc day_start_hour theo múi giờ user)."""
    day = (day_ends_at(user, session.end_at) - timedelta(days=1)).date()
    row = DayLearnLog.objects.filter(user=user, date=day).first() or DayLearnLog.objects.create(user=user, date=day)
    DayLearnLog.objects.filter(pk=row.pk).update(
        words_learned=F('words_learned') + session.new_count,
        words_relearned=F('words_relearned') + session.review_count,
        learn_time=F('learn_time') + session.time,
        xp_earned=F('xp_earned') + session.xp_earned,
        sessions_count=F('sessions_count') + 1,
        updated_at=timezone.now(),
    )


def refresh_learned_count(user, collection_id):
    user_collection = UserCollection.objects.filter(user=user, collection_id=collection_id, is_deleted=False, collection__isnull=False).first()
    if user_collection is None:
        return
    keys = list(collection_cards(user, user_collection))
    learned = sum(1 for p in UserSenseProgress.objects.filter(user=user, original_sense_id__in=keys).exclude(is_deleted=True) if not is_new(p))
    UserCollection.objects.filter(pk=user_collection.pk).update(learned_count=learned, updated_at=timezone.now())


def session_row(session):
    return {name: getattr(session, name) for name in (
        'id', 'type', 'mode', 'status', 'collection_id', 'methods', 'start_at', 'end_at', 'time', 'senses', 'sense_count',
        'new_count', 'review_count', 'correct_count', 'wrong_count', 'xp_earned')}


def progress_rows(user, originals):
    columns = PULL_TABLES['user_sense_progress'][2]
    return list(UserSenseProgress.objects.filter(user=user, original_sense_id__in=list(originals)).values(*columns))
