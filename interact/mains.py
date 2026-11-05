"""
Nghĩa chính của một từ do người dùng đặt (UserInteraction interaction_type='main', target_type='sense'):
- riêng: mỗi người, mỗi từ tối đa 1 nghĩa chính, hiện đứng đầu ở trang từ của chính họ (ai/queries.py personalize_word);
- chung: mỗi tài khoản (không tính khách) cộng 1 vào AISense.main_count của nghĩa chính thức đó, score tính lại (ai/scores.py).
Đặt nghĩa khác thì phiếu cũ được thu hồi (main_count nghĩa cũ giảm). Nghĩa riêng của chính mình đặt làm chính chỉ có tác dụng riêng.
"""
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest

from ai.models import AISense, AIWord
from ai.scores import refresh_sense_scores
from interact.models import UserInteraction


def counts_globally(user):
    """Khách đặt nghĩa chính chỉ có tác dụng riêng; phiếu chung chỉ tính tài khoản thật (device-login tạo vô hạn)."""
    from user.models import UserProfile
    tier = UserProfile.objects.filter(user=user).values_list('tier', flat=True).first()
    return tier not in (None, 'guest')


def main_sense_id(user, sense_ids):
    """Nghĩa chính user đã đặt trong số các nghĩa này (str) hoặc None."""
    target = (
        UserInteraction.objects.filter(user=user, interaction_type='main', target_type='sense', status=True, target_id__in=list(sense_ids))
        .values_list('target_id', flat=True).first()
    )
    return str(target) if target else None


def set_main(user, sense, want):
    """
    Đặt (want=True) hoặc bỏ (want=False) `sense` làm nghĩa chính của từ cho user. Khoá dòng từ nên hai request đồng thời
    không cộng / trừ main_count hai lần. Trả (có đang là nghĩa chính không, main_count hiện tại, id các nghĩa đổi điểm).
    """
    with transaction.atomic():
        AIWord.objects.select_for_update().filter(pk=sense.word_id).exists()
        siblings = dict(AISense.objects.filter(word_id=sense.word_id).values_list('id', 'is_official'))
        active = list(UserInteraction.objects.filter(
            user=user, interaction_type='main', target_type='sense', target_id__in=list(siblings), status=True,
        ))
        counted = counts_globally(user)
        touched = set()

        def drop(rows):
            for row in rows:
                UserInteraction.objects.filter(pk=row.pk).update(status=False)
                if counted and siblings.get(row.target_id):
                    AISense.objects.filter(pk=row.target_id).update(main_count=Greatest(F('main_count') - 1, 0))
                    touched.add(row.target_id)

        if want:
            already = any(row.target_id == sense.id for row in active)
            drop([row for row in active if row.target_id != sense.id])
            if not already:
                UserInteraction.objects.update_or_create(
                    user=user, interaction_type='main', target_type='sense', target_id=sense.id, version=0,
                    defaults={'status': True, 'created_by': user},
                )
                if counted and sense.is_official:
                    AISense.objects.filter(pk=sense.pk).update(main_count=F('main_count') + 1)
                    touched.add(sense.id)
        else:
            drop([row for row in active if row.target_id == sense.id])

        if touched:
            refresh_sense_scores(pk__in=list(touched))
        main_count = AISense.objects.filter(pk=sense.pk).values_list('main_count', flat=True).first() or 0
    return want, main_count, touched
