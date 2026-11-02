"""
Đồng bộ app (SQLite, local-first) <-> server (Postgres).

Push: app gửi các dòng đã đổi, mỗi bảng có luật gộp riêng (idempotent, gửi lại vẫn an toàn):
  - user_sense_progress: 1 dòng / (user, original_sense_id), bản có client_updated_at mới hơn thắng
  - review_log: chỉ thêm, trùng id thì bỏ qua; dòng is_fsrs mới cộng vào thống kê chung của nghĩa (tracking/stats.py)
  - day_learn_log: 1 dòng / ngày, mỗi bộ đếm lấy giá trị lớn hơn (bộ đếm chỉ tăng)
  - learn_session, user_note, user_settings: ghi đè, ghi chú / cài đặt so client_updated_at
  - user_profile: chỉ các trường trong PROFILE_WRITABLE
Pull: phân trang theo con trỏ (thời điểm, id) từng bảng, kèm dòng đã xoá mềm (is_deleted) để app xoá theo.
  user_collection chỉ pull (thêm / xoá bộ qua API bộ từ).
"""
import json
import re
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo, available_timezones

from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from ai.models import AISense
from ai.validators import is_lang, is_uuid
from interact.models import LearnSession, UserNote
from core.models import UserCollection
from tracking.models import DayLearnLog, ReviewLog, UserSenseProgress
from tracking.stats import apply_review_stats
from user.models import UserProfile, UserSetting

MAX_ITEMS = 500
MAX_PULL = 1000
MAX_NOTE = 10000
MAX_SESSION_SENSES = 500
MAX_SETTING_BYTES = 2000
SETTING_KEY_RE = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
REVIEW_LOCK_NAMESPACE = 7301  # pg_advisory_xact_lock(namespace, user_id) cho push review_log


# ---------------------------------------------------------------- làm sạch giá trị

def to_time(value):
    """ISO8601 hoặc 'YYYY-MM-DD HH:MM:SS' của SQLite (không múi giờ = UTC) -> datetime có múi giờ, sai thì None."""
    if not isinstance(value, str):
        return None
    try:
        dt = parse_datetime(value)
    except ValueError:
        return None
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


def _num(value, kind, lo, hi):
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, (int, float)):
        raise ValueError
    value = kind(value)
    if value != value or not (lo <= value <= hi):  # value != value: NaN
        raise ValueError
    return value


def _clean(raw, spec):
    """
    spec: {tên: (kiểu, ...)} -> dict chỉ gồm các khoá có trong raw và hợp lệ.
    Kiểu: int/float (lo, hi), bool, time, text (max_len), choice (tập giá trị).
    Raise ValueError('tên') ở khoá sai để báo lý do.
    """
    out = {}
    for name, rule in spec.items():
        if name not in raw:
            continue
        value, kind = raw[name], rule[0]
        try:
            if kind in (int, float):
                out[name] = _num(value, kind, rule[1], rule[2])
            elif kind == 'bool':
                if value not in (0, 1, True, False):
                    raise ValueError
                out[name] = bool(value)
            elif kind == 'time':
                if value is None and len(rule) > 1 and rule[1]:  # cho phép null
                    out[name] = None
                    continue
                dt = to_time(value)
                if dt is None:
                    raise ValueError
                out[name] = dt
            elif kind == 'text':
                if not isinstance(value, str):
                    raise ValueError
                out[name] = value[:rule[1]]
            elif kind == 'choice':
                if value not in rule[1]:
                    raise ValueError
                out[name] = value
        except (ValueError, TypeError, OverflowError):
            raise ValueError(name)
    return out


def _free_id(model, candidate, taken):
    """Dùng id client sinh nếu hợp lệ và chưa có dòng nào khác giữ, không thì để server sinh."""
    if is_uuid(candidate) and str(candidate) not in taken:
        taken.add(str(candidate))
        return {'id': candidate}
    return {}


def _taken_ids(model, items):
    ids = [i.get('id') for i in items if isinstance(i, dict) and is_uuid(i.get('id'))]
    return {str(pk) for pk in model.objects.filter(id__in=ids).values_list('id', flat=True)}


def _result():
    return {'accepted': 0, 'skipped': 0, 'rejected': []}


def _reject(result, item, reason):
    result['rejected'].append({'id': item.get('id') if isinstance(item, dict) else None, 'reason': reason})


# ---------------------------------------------------------------- push từng bảng

PROGRESS_SPEC = {
    'level': (int, 0, 7), 'max_level': (int, 0, 7),
    'streak_correct': (int, 0, 10**6), 'forget_count': (int, 0, 10**6),
    'remember_count': (int, 0, 10**6), 'repetitions': (int, 0, 10**6),
    'state': (int, 0, 3), 'stability': (float, 0, 10**5), 'difficulty': (float, 0, 10),
    'scheduled_days': (float, 0, 10**5), 'learning_steps': (int, 0, 100),
    'failed_at': ('time', True), 'last_seen_at': ('time', True),
    'last_reviewed_at': ('time', True), 'next_review_at': ('time', True),
    'is_mastered': ('bool',), 'is_hidden': ('bool',), 'is_avoid': ('bool',), 'is_deleted': ('bool',),
}


def push_progress(user, items):
    result = _result()
    items = [i for i in items if isinstance(i, dict)]
    existing = {
        str(p.original_sense_id): p
        for p in UserSenseProgress.objects.filter(user=user, original_sense_id__in=[
            i['original_sense_id'] for i in items if is_uuid(i.get('original_sense_id'))
        ])
    }
    taken = _taken_ids(UserSenseProgress, items)
    now = timezone.now()
    for item in items:
        sense_id = item.get('original_sense_id')
        if not is_uuid(sense_id):
            _reject(result, item, 'original_sense_id')
            continue
        try:
            data = _clean(item, PROGRESS_SPEC)
        except ValueError as e:
            _reject(result, item, f'invalid:{e}')
            continue
        client_at = min(to_time(item.get('updated_at')) or now, now + timedelta(minutes=5))
        row = existing.get(str(sense_id).lower()) or existing.get(str(sense_id))
        if row is None:
            row = UserSenseProgress(user=user, original_sense_id=sense_id, **_free_id(UserSenseProgress, item.get('id'), taken))
        else:
            reviewed_later = data.get('last_reviewed_at') and (row.last_reviewed_at is None or data['last_reviewed_at'] > row.last_reviewed_at)
            if row.client_updated_at and client_at < row.client_updated_at and not reviewed_later:
                result['skipped'] += 1
                continue
        for name, value in data.items():
            setattr(row, name, value)
        row.client_updated_at = client_at
        row.save()
        existing[str(row.original_sense_id)] = row
        result['accepted'] += 1
    return result


REVIEW_SPEC = {
    'rating': (int, 1, 4), 'state': (int, 0, 3),
    'duration_ms': (int, 0, 3600000),
    'elapsed_days': (float, 0, 10**5), 'scheduled_days': (float, 0, 10**5),
    'stability': (float, 0, 10**5), 'difficulty': (float, 0, 10),
    'mode': ('text', 40), 'reviewed_at': ('time',),
    'is_fsrs': ('bool',), 'level': (int, 0, 7), 'hint_count': (int, 0, 100), 'attempts': (int, 0, 100),
}


def push_review_logs(user, items):
    result = _result()
    rows, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if not is_uuid(item.get('id')) or not is_uuid(item.get('original_sense_id')) or 'rating' not in item:
            _reject(result, item, 'id, original_sense_id, rating')
            continue
        try:
            data = _clean(item, REVIEW_SPEC)
        except ValueError as e:
            _reject(result, item, f'invalid:{e}')
            continue
        for name in ('sense_id', 'session_id'):
            data[name] = item[name] if is_uuid(item.get(name)) else None
        if str(item['id']) in seen:
            continue
        seen.add(str(item['id']))
        rows.append(ReviewLog(id=item['id'], user=user, original_sense_id=item['original_sense_id'], **data))
    if not rows:
        return result
    with transaction.atomic():
        # Khoá theo user: 2 lần push cùng lúc (gửi lại khi mạng chập chờn) không cùng thấy 1 dòng là "mới" rồi cộng thống kê 2 lần
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [REVIEW_LOCK_NAMESPACE, user.pk])
        existing = set(ReviewLog.objects.filter(id__in=[row.id for row in rows]).values_list('id', flat=True))
        # Chỉ thêm, trùng id (gửi lại) bỏ qua
        ReviewLog.objects.bulk_create(rows, ignore_conflicts=True)
        fresh = [row for row in rows if uuid.UUID(str(row.id)) not in existing and row.is_fsrs]
        apply_review_stats(user, fresh)
    result['accepted'] = len(rows)
    return result


DAY_SPEC = {
    'words_learned': (int, 0, 10**5), 'words_relearned': (int, 0, 10**5),
    'learn_time': (int, 0, 86400), 'app_time': (int, 0, 86400),
    'xp_earned': (int, 0, 10**7), 'sessions_count': (int, 0, 10**4),
}


def push_day_logs(user, items):
    result = _result()
    items = [i for i in items if isinstance(i, dict)]
    taken = _taken_ids(DayLearnLog, items)
    limit_day = timezone.now().date() + timedelta(days=2)  # chừa lệch múi giờ
    for item in items:
        day = parse_date(item['date']) if isinstance(item.get('date'), str) else None
        if day is None or day > limit_day:
            _reject(result, item, 'date')
            continue
        try:
            data = _clean(item, DAY_SPEC)
        except ValueError as e:
            _reject(result, item, f'invalid:{e}')
            continue
        row = DayLearnLog.objects.filter(user=user, date=day).first()
        if row is None:
            DayLearnLog.objects.create(user=user, date=day, **_free_id(DayLearnLog, item.get('id'), taken), **data)
        else:
            # Bộ đếm chỉ tăng: 2 máy cùng ngày thì lấy giá trị lớn hơn
            for name, value in data.items():
                setattr(row, name, max(getattr(row, name), value))
            row.save()
        result['accepted'] += 1
    return result


SESSION_SPEC = {
    'type': ('choice', {c[0] for c in LearnSession.TYPE_CHOICES}),
    'start_at': ('time',), 'end_at': ('time', True),
    'time': (int, 0, 86400), 'sense_count': (int, 0, 10**4), 'is_deleted': ('bool',),
    'status': ('choice', {c[0] for c in LearnSession.STATUS_CHOICES}),
    'new_count': (int, 0, 10**4), 'review_count': (int, 0, 10**4),
    'correct_count': (int, 0, 10**5), 'wrong_count': (int, 0, 10**5), 'xp_earned': (int, 0, 10**6),
}
MAX_SESSION_METHODS = 20


def push_sessions(user, items):
    result = _result()
    for item in items:
        if not isinstance(item, dict) or not is_uuid(item.get('id')):
            _reject(result, item, 'id')
            continue
        try:
            data = _clean(item, SESSION_SPEC)
        except ValueError as e:
            _reject(result, item, f'invalid:{e}')
            continue
        senses = item.get('senses', [])
        if not isinstance(senses, list) or len(senses) > MAX_SESSION_SENSES or not all(is_uuid(s) for s in senses):
            _reject(result, item, 'invalid:senses')
            continue
        data['senses'] = [str(s) for s in senses]
        data.setdefault('sense_count', len(senses))
        if 'collection_id' in item:
            if item['collection_id'] is not None and not is_uuid(item['collection_id']):
                _reject(result, item, 'invalid:collection_id')
                continue
            data['collection_id'] = item['collection_id']
        if 'methods' in item:
            methods = item['methods']
            if isinstance(methods, str):  # SQLite lưu JSON dạng chuỗi
                try:
                    methods = json.loads(methods)
                except ValueError:
                    methods = None
            if not isinstance(methods, list) or len(methods) > MAX_SESSION_METHODS or not all(isinstance(m, str) and 0 < len(m) <= 40 for m in methods):
                _reject(result, item, 'invalid:methods')
                continue
            data['methods'] = methods
        row = LearnSession.objects.filter(pk=item['id']).first()
        if row is not None and row.user_id != user.pk:
            _reject(result, item, 'id_taken')
            continue
        if row is None:
            if 'start_at' not in data:
                _reject(result, item, 'start_at')
                continue
            row = LearnSession(id=item['id'], user=user)
        for name, value in data.items():
            setattr(row, name, value)
        row.save()
        result['accepted'] += 1
    return result


def push_notes(user, items):
    result = _result()
    items = [i for i in items if isinstance(i, dict)]
    taken = _taken_ids(UserNote, items)
    known = set(str(s) for s in AISense.objects.filter(id__in=[i['sense_id'] for i in items if is_uuid(i.get('sense_id'))]).values_list('id', flat=True))
    now = timezone.now()
    for item in items:
        sense_id = item.get('sense_id')
        content = item.get('content')
        deleting = bool(item.get('is_deleted'))
        if not is_uuid(sense_id) or str(sense_id).lower() not in known:
            _reject(result, item, 'sense_id')
            continue
        if not deleting and (not isinstance(content, str) or not content.strip()):
            _reject(result, item, 'content')
            continue
        client_at = min(to_time(item.get('updated_at')) or now, now + timedelta(minutes=5))
        row = UserNote.objects.filter(user=user, sense_id=sense_id, is_deleted=False).first()
        if row is not None and row.client_updated_at and client_at < row.client_updated_at:
            result['skipped'] += 1
            continue
        if row is None:
            if deleting:
                result['accepted'] += 1  # đã không còn gì để xoá
                continue
            row = UserNote(user=user, sense_id=sense_id, **_free_id(UserNote, item.get('id'), taken))
        if deleting:
            row.is_deleted, row.deleted_at = True, now
        else:
            row.content = content.strip()[:MAX_NOTE]
        row.version = (row.version or 0) + 1
        row.client_updated_at = client_at
        row.save()
        result['accepted'] += 1
    return result


def push_settings(user, items):
    result = _result()
    now = timezone.now()
    for item in items:
        key = item.get('key') if isinstance(item, dict) else None
        if not isinstance(key, str) or not SETTING_KEY_RE.match(key):
            _reject(result, {'id': key}, 'key')
            continue
        try:
            raw = json.dumps(item.get('value'))
        except (TypeError, ValueError):
            _reject(result, {'id': key}, 'value')
            continue
        if len(raw) > MAX_SETTING_BYTES:
            _reject(result, {'id': key}, 'value_too_large')
            continue
        client_at = min(to_time(item.get('updated_at')) or now, now + timedelta(minutes=5))
        row = UserSetting.objects.filter(user=user, key=key).first()
        if row is not None and row.client_updated_at and client_at < row.client_updated_at:
            result['skipped'] += 1
            continue
        if row is None:
            row = UserSetting(user=user, key=key)
        row.value, row.client_updated_at, row.is_deleted = item.get('value'), client_at, False
        row.save()
        result['accepted'] += 1
    return result


# ---------------------------------------------------------------- profile

def _valid_zone(value):
    return isinstance(value, str) and value in available_timezones()


def clean_profile(data):
    """Chỉ trường người dùng được tự đổi. Trả (dữ liệu sạch, lỗi theo trường). Điểm, streak, tier, ban do server giữ."""
    clean, errors = {}, {}
    if not isinstance(data, dict):
        return clean, {'profile': 'object'}
    simple = {'full_name': 255, 'country_code': 20, 'native_language': 50}
    for name, max_len in simple.items():
        if name in data:
            if isinstance(data[name], str):
                clean[name] = data[name].strip()[:max_len]
            else:
                errors[name] = 'text'
    if 'avatar_url' in data:
        url = data['avatar_url']
        if isinstance(url, str) and (url == '' or (url.startswith(('http://', 'https://')) and len(url) <= 512)):
            clean['avatar_url'] = url
        else:
            errors['avatar_url'] = 'url'
    if 'age' in data:
        try:
            clean['age'] = None if data['age'] is None else _num(data['age'], int, 0, 150)
        except ValueError:
            errors['age'] = 'range'
    if 'gender' in data:
        if data['gender'] in ('', 'male', 'female', 'other'):
            clean['gender'] = data['gender']
        else:
            errors['gender'] = 'choice'
    if 'dob' in data:
        dob = parse_date(data['dob']) if isinstance(data['dob'], str) else None
        if data['dob'] is None or (dob and dob < timezone.now().date()):
            clean['dob'] = dob
        else:
            errors['dob'] = 'date'
    if 'learning_languages' in data:
        langs = data['learning_languages']
        if isinstance(langs, list) and len(langs) <= 20 and all(is_lang(code) for code in langs):
            clean['learning_languages'] = list(dict.fromkeys(langs))
        else:
            errors['learning_languages'] = 'languages'
    if 'fav_time' in data:
        t = parse_time(data['fav_time']) if isinstance(data['fav_time'], str) else None
        if data['fav_time'] is None or t:
            clean['fav_time'] = t
        else:
            errors['fav_time'] = 'time'
    if 'time_zone' in data:
        if _valid_zone(data['time_zone']):
            clean['time_zone'] = data['time_zone']
        else:
            errors['time_zone'] = 'zone'
    if 'zone_num' in data:
        try:
            clean['zone_num'] = _num(data['zone_num'], int, -12, 14)
        except ValueError:
            errors['zone_num'] = 'range'
    return clean, errors


def push_profile(user, data):
    result = _result()
    clean, errors = clean_profile(data)
    if errors:
        result['rejected'] = [{'id': 'profile', 'reason': f'invalid:{name}'} for name in errors]
        return result
    profile = UserProfile.objects.get(user=user)
    for name, value in clean.items():
        setattr(profile, name, value)
    profile.save()
    result['accepted'] = 1
    return result


def refresh_stats(user):
    """Tính lại tổng XP và streak từ day_learn_log (server là nguồn đúng, app chỉ hiển thị)."""
    profile = UserProfile.objects.filter(user=user).first()
    if profile is None:
        return
    try:
        zone = ZoneInfo(profile.time_zone)
    except Exception:
        zone = ZoneInfo('UTC')
    logs = DayLearnLog.objects.filter(user=user).exclude(is_deleted=True)
    total_xp = logs.aggregate(total=Sum('xp_earned'))['total'] or 0
    days = set(logs.filter(Q(words_learned__gt=0) | Q(words_relearned__gt=0) | Q(learn_time__gt=0)).values_list('date', flat=True))

    today = timezone.now().astimezone(zone).date()
    day = today if today in days else today - timedelta(days=1)  # hôm nay chưa học thì streak vẫn giữ tới hết hôm nay
    current = 0
    while day in days:
        current += 1
        day -= timedelta(days=1)

    longest = run = 0
    previous = None
    for d in sorted(days):
        run = run + 1 if previous and d - previous == timedelta(days=1) else 1
        longest, previous = max(longest, run), d

    profile.total_xp = min(total_xp, 2**31 - 1)
    profile.current_streak = current
    profile.max_streak = max(profile.max_streak, longest, current)
    profile.save(update_fields=['total_xp', 'current_streak', 'max_streak', 'updated_at'])


PUSH_HANDLERS = {
    'user_sense_progress': push_progress,
    'review_log': push_review_logs,
    'day_learn_log': push_day_logs,
    'learn_session': push_sessions,
    'user_note': push_notes,
    'user_settings': push_settings,
}


# ---------------------------------------------------------------- pull

# bảng -> (model, trường làm con trỏ, các cột trả về)
PULL_TABLES = {
    'user_sense_progress': (UserSenseProgress, 'updated_at', [
        'id', 'original_sense_id', 'level', 'max_level', 'streak_correct', 'forget_count', 'remember_count', 'repetitions',
        'state', 'stability', 'difficulty', 'scheduled_days', 'learning_steps', 'failed_at',
        'is_mastered', 'is_hidden', 'is_avoid', 'is_deleted',
        'last_seen_at', 'last_reviewed_at', 'next_review_at', 'created_at', 'updated_at']),
    'review_log': (ReviewLog, 'created_at', [
        'id', 'original_sense_id', 'sense_id', 'session_id', 'rating', 'mode', 'is_fsrs', 'level', 'hint_count', 'attempts',
        'duration_ms', 'state', 'elapsed_days', 'scheduled_days', 'stability', 'difficulty', 'reviewed_at', 'created_at']),
    'day_learn_log': (DayLearnLog, 'updated_at', [
        'id', 'date', 'words_learned', 'words_relearned', 'learn_time', 'app_time', 'xp_earned', 'sessions_count',
        'created_at', 'updated_at']),
    'learn_session': (LearnSession, 'updated_at', [
        'id', 'type', 'status', 'collection_id', 'methods', 'start_at', 'end_at', 'time', 'senses', 'sense_count',
        'new_count', 'review_count', 'correct_count', 'wrong_count', 'xp_earned', 'is_deleted', 'created_at', 'updated_at']),
    'user_note': (UserNote, 'updated_at', [
        'id', 'sense_id', 'content', 'version', 'is_deleted', 'created_at', 'updated_at']),
    'user_settings': (UserSetting, 'updated_at', ['id', 'key', 'value', 'is_deleted', 'created_at', 'updated_at']),
    # Chỉ danh sách bộ từ của user (chỉ pull); dòng từ + nội dung lấy qua GET user-collections/{id}/bundle (có etag)
    'user_collection': (UserCollection, 'updated_at', [
        'id', 'collection_id', 'name', 'description', 'learned_count', 'version', 'is_active', 'is_deleted',
        'created_at', 'updated_at']),
}


def parse_cursor(raw):
    """'<thời điểm ISO>|<id>' -> (datetime, id). Raise ValueError nếu sai."""
    if not raw:
        return None
    stamp, _, pk = str(raw).partition('|')
    dt = to_time(stamp)
    if dt is None or not is_uuid(pk):
        raise ValueError(raw)
    return dt, pk


def make_cursor(row, field):
    return f"{row[field].isoformat()}|{row['id']}"


def pull_table(user, table, cursor, limit):
    model, field, columns = PULL_TABLES[table]
    qs = model.objects.filter(user=user)
    if cursor:
        ts, pk = cursor
        qs = qs.filter(Q(**{f'{field}__gt': ts}) | Q(**{field: ts, 'id__gt': pk}))
    rows = list(qs.order_by(field, 'id').values(*columns)[:limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        'items': rows,
        'has_more': has_more,
        'cursor': make_cursor(rows[-1], field) if rows else (f'{cursor[0].isoformat()}|{cursor[1]}' if cursor else None),
    }
