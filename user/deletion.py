"""
Xoá tài khoản (DELETE /api/me/) và xuất dữ liệu (GET /api/me/export/).

Xoá: dữ liệu riêng mất hẳn (tiến độ, log, phiên, ghi chú, cài đặt, thiết bị, like, report... đều CASCADE theo auth.User).
Nội dung đã công khai mà người khác đang dùng (bộ đã phát hành, nghĩa đã đóng băng trong bộ đó) được giữ lại
nhưng bỏ tên tác giả (created_by -> NULL theo SET_NULL của BaseModel). Nội dung riêng chưa công khai bị xoá.
Liên kết Apple được thu hồi (Apple yêu cầu) nếu có đủ key.
"""
from django.db import transaction
from django.db.models import Q

from ai.models import AISense
from core.models import Collection, CollectionItem
from user.models import AccountProvider
from user.providers import apple_revoke


def _private_senses(user):
    """Nghĩa riêng của user mà không ai khác đang dùng (không nằm trong dòng đã phát hành / dòng của người khác)."""
    used = CollectionItem.objects.filter(Q(released=True) | ~Q(created_by=user)).filter(
        Q(sense__created_by=user) | Q(original__created_by=user)
    )
    used_ids = set(used.values_list('sense_id', flat=True)) | set(used.values_list('original_id', flat=True))
    based_on = set(AISense.objects.filter(original__created_by=user).exclude(created_by=user).values_list('original_id', flat=True))
    return AISense.objects.filter(created_by=user, is_official=False).exclude(id__in=used_ids | based_on)


def delete_account(user):
    """Xoá user và dữ liệu riêng. Trả số liệu đã xoá / giữ lại để app báo cho người dùng."""
    revoked = [apple_revoke(row.access_token) for row in AccountProvider.objects.filter(user=user, provider='apple') if row.access_token]
    with transaction.atomic():
        drafts = CollectionItem.objects.filter(created_by=user, released=False)
        private_collections = Collection.objects.filter(created_by=user, released=False)
        CollectionItem.objects.filter(collection__in=private_collections).delete()
        kept_collections = Collection.objects.filter(created_by=user, released=True).count()
        result = {
            'deleted_draft_items': drafts.delete()[0],
            'deleted_collections': private_collections.delete()[0],
            'deleted_senses': _private_senses(user).delete()[0],
            'kept_public_collections': kept_collections,
            'apple_revoked': all(revoked) if revoked else None,
        }
        orphan_media = _unused_media(user)
        user.delete()
    # File lưu ngoài DB: xoá sau khi giao dịch xong (lỗi xoá file không làm hỏng việc xoá tài khoản)
    result['deleted_media'] = sum(_delete_file(media) for media in orphan_media)
    return result


def _unused_media(user):
    """File user tải lên mà nội dung công khai còn giữ lại (nghĩa đóng băng, bộ đã phát hành) không dùng tới."""
    from asset.models import MediaFile
    used = set()
    for sense in AISense.objects.filter(created_by=user).only('audio', 'image_preview'):
        used.update(filter(None, (sense.audio, sense.image_preview)))
    for collection in Collection.objects.filter(created_by=user, released=True).only('image_url', 'metadatas'):
        used.add(collection.image_url)
        used.update(meta.get('image') for meta in collection.metadatas or [] if isinstance(meta, dict))
    return list(MediaFile.objects.filter(created_by=user).exclude(key__in=[k for k in used if k]))


def _delete_file(media):
    from asset.media import delete_media
    try:
        delete_media(media)
        return 1
    except Exception:
        return 0


def _rows(queryset, exclude=()):
    return [{k: v for k, v in row.items() if k not in exclude} for row in queryset.values()]


def export_data(user):
    """Toàn bộ dữ liệu của user dạng {bảng: [dòng]} (bỏ token / mã bí mật)."""
    from interact.models import Feedback, LearnSession, Report, UserInteraction, UserNote
    from core.models import UserCollection
    from tracking.models import DayLearnLog, ReviewLog, UserSenseProgress
    from asset.models import MediaFile
    from core.storages import media_url
    from user.models import Device, UserProfile, UserSetting

    # Uy tín là chỉ số ẩn (user/reputation.py) nên không xuất
    secret = ('access_token', 'push_token', 'refresh_token_jti')
    return {
        'account': {'username': user.username, 'email': user.email, 'date_joined': user.date_joined, 'last_login': user.last_login},
        'profile': _rows(UserProfile.objects.filter(user=user), exclude=('reputation', 'report_accepted', 'report_rejected')),
        'login_methods': _rows(AccountProvider.objects.filter(user=user), exclude=secret),
        'devices': _rows(Device.objects.filter(user=user), exclude=secret),
        'user_settings': _rows(UserSetting.objects.filter(user=user)),
        'user_sense_progress': _rows(UserSenseProgress.objects.filter(user=user)),
        'review_log': _rows(ReviewLog.objects.filter(user=user)),
        'day_learn_log': _rows(DayLearnLog.objects.filter(user=user)),
        'learn_session': _rows(LearnSession.objects.filter(user=user)),
        'user_note': _rows(UserNote.objects.filter(user=user)),
        'user_collection': _rows(UserCollection.objects.filter(user=user)),
        'collections': _rows(Collection.objects.filter(created_by=user)),
        'collection_items': _rows(CollectionItem.objects.filter(created_by=user)),
        'senses': _rows(AISense.objects.filter(created_by=user)),
        'interactions': _rows(UserInteraction.objects.filter(user=user)),
        'reports': _rows(Report.objects.filter(user=user), exclude=('weight',)),
        'feedback': _rows(Feedback.objects.filter(user=user)),
        'media': [{**row, 'url': media_url(row['key'])} for row in _rows(MediaFile.objects.filter(created_by=user))],
    }
