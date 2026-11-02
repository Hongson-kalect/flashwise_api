"""
Chế tài của admin: ban / gỡ ban user, bộ từ, từ, nghĩa. Mỗi lần làm ghi 1 dòng ModerationAction (kèm trạng thái cũ để gỡ).
- user (target_id = UserProfile.id): UserProfile.is_active=False + ban_reason / ban_expires_at. Hạn chế tối thiểu: vẫn học,
  sync, tra từ, sửa nháp riêng; chỉ chặn việc người khác nhìn thấy (phát hành bộ, like, report), xem NotBanned.
  hide_content=True thì ẩn luôn bộ đã phát hành và nghĩa đã đóng băng của họ (gỡ ban thì hiện lại đúng những cái đó).
- collection / sense: is_active=False, ai cũng không thấy (kể cả tác giả); tác giả bị trừ uy tín.
- word: status BANNED, get-word trả 404 và không tạo lại bằng AI.
"""
from django.db import transaction
from django.utils import timezone

from ai.models import AISense, AIWord
from core.models import Collection
from interact.models import ModerationAction
from user.models import UserProfile
from user.reputation import record
from utils.redis.word_init import WordCacheManager
from utils.utils.best_effort import best_effort

TARGETS = ('user', 'collection', 'word', 'sense')


class ModerationError(Exception):
    pass


def find_target(target_type, target_id):
    model = {'user': UserProfile, 'collection': Collection, 'word': AIWord, 'sense': AISense}.get(target_type)
    if model is None:
        return None
    queryset = model.objects.filter(pk=target_id)
    if target_type == 'user':
        queryset = queryset.select_related('user')
    return queryset.first()


def target_author(target_type, target):
    """Người chịu trách nhiệm nội dung (để trừ uy tín). Nội dung chính thức do AI / admin tạo thì không ai."""
    if target_type == 'user':
        return target.user
    if target_type == 'collection' and not target.is_official:
        return target.created_by
    if target_type == 'sense' and not target.is_official:
        return target.created_by
    return None


def _clear_word_cache(word):
    if word is not None:
        best_effort(WordCacheManager().cache_word_clear_specific, word.language_code, word.value)


def ban(admin, target_type, target_id, reason='', expires_at=None, hide_content=False, report=None):
    target = find_target(target_type, target_id)
    if target is None:
        raise ModerationError('not_found')
    affected = {}
    with transaction.atomic():
        if target_type == 'user':
            if target.user.is_staff:
                raise ModerationError('cannot_ban_staff')
            UserProfile.objects.filter(pk=target.pk).update(is_active=False, ban_reason=reason, ban_expires_at=expires_at)
            if hide_content:
                collections = Collection.objects.filter(created_by=target.user, released=True, is_active=True).exclude(is_deleted=True)
                senses = AISense.objects.filter(created_by=target.user, is_official=False, is_frozen=True).exclude(is_active=False)
                affected = {'collections': [str(i) for i in collections.values_list('id', flat=True)],
                            'senses': [str(i) for i in senses.values_list('id', flat=True)]}
                Collection.objects.filter(id__in=affected['collections']).update(is_active=False)
                AISense.objects.filter(id__in=affected['senses']).update(is_active=False)
        elif target_type == 'word':
            affected = {'status': target.status if target.status != 'BANNED' else 'COMPLETED'}
            AIWord.objects.filter(pk=target.pk).update(status='BANNED', updated_at=timezone.now())
        else:
            affected = {'is_active': target.is_active is not False}
            type(target).objects.filter(pk=target.pk).update(is_active=False, updated_at=timezone.now())

        author = target_author(target_type, target)
        if author is not None and not author.is_staff:
            record(author, 'user_banned' if target_type == 'user' else 'content_banned', target_type, target.pk, report=report, by=admin)
            affected['penalized'] = author.pk
        action = ModerationAction.objects.create(
            target_type=target_type, target_id=target.pk, action='ban', reason=reason, expires_at=expires_at,
            affected=affected, created_by=admin,
        )
    if target_type == 'word':
        _clear_word_cache(target)
    elif target_type == 'sense':
        _clear_word_cache(target.word)
    return action


def unban(admin, target_type, target_id, reason=''):
    target = find_target(target_type, target_id)
    if target is None:
        raise ModerationError('not_found')
    last = ModerationAction.objects.filter(target_type=target_type, target_id=target.pk).order_by('-created_at').first()
    if last is None or last.action != 'ban':
        raise ModerationError('not_banned')
    affected = last.affected or {}
    with transaction.atomic():
        if target_type == 'user':
            UserProfile.objects.filter(pk=target.pk).update(is_active=True, ban_reason=None, ban_expires_at=None)
            Collection.objects.filter(id__in=affected.get('collections') or []).update(is_active=True)
            AISense.objects.filter(id__in=affected.get('senses') or []).update(is_active=True)
        elif target_type == 'word':
            AIWord.objects.filter(pk=target.pk).update(status=affected.get('status') or 'COMPLETED', updated_at=timezone.now())
        else:
            type(target).objects.filter(pk=target.pk).update(is_active=affected.get('is_active', True), updated_at=timezone.now())

        penalized = affected.get('penalized')
        if penalized:
            from django.contrib.auth.models import User
            user = User.objects.filter(pk=penalized).first()
            record(user, 'user_unbanned' if target_type == 'user' else 'content_unbanned', target_type, target.pk, by=admin)
        action = ModerationAction.objects.create(
            target_type=target_type, target_id=target.pk, action='unban', reason=reason, affected={}, created_by=admin,
        )
    if target_type == 'word':
        _clear_word_cache(target)
    elif target_type == 'sense':
        _clear_word_cache(target.word)
    return action
