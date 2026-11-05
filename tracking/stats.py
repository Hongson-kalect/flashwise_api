"""
Thống kê học chung của từng nghĩa (mọi user), cộng dồn khi app push review_log.
Chỉ tính dòng is_fsrs mới chèn (push lại cùng dòng không cộng lần 2); ghi lên mọi bản AISense cùng original
để bản nào của nghĩa cũng mang cùng số liệu.
"""
from collections import defaultdict

from django.db.models import F, Q

from ai.models import AISense
from ai.scores import refresh_sense_scores
from tracking.fsrs import AGAIN, initial_difficulty
from tracking.models import ReviewLog


def first_reviews(user, rows):
    """
    Trong các dòng is_fsrs vừa chèn, dòng nào là lần chấm FSRS đầu tiên của user với nghĩa đó (theo reviewed_at).
    Trả {original_sense_id: dòng}. Nghĩa đã có dòng is_fsrs cũ hơn (đã học từ trước) thì không tính.
    """
    earliest = {}
    for row in rows:
        key = str(row.original_sense_id)
        if key not in earliest or row.reviewed_at < earliest[key].reviewed_at:
            earliest[key] = row
    if not earliest:
        return {}
    new_ids = [row.id for row in rows]
    learned_before = set(
        str(sid) for sid in ReviewLog.objects.filter(user=user, is_fsrs=True, original_sense_id__in=list(earliest))
        .exclude(id__in=new_ids).values_list('original_sense_id', flat=True)
    )
    return {key: row for key, row in earliest.items() if key not in learned_before}


def apply_review_stats(user, rows):
    """
    rows: các ReviewLog is_fsrs vừa chèn của 1 user. Cộng số lần quên / nhớ, và với lần chấm đầu tiên thì gộp
    độ khó ban đầu (theo điểm chấm, initial_difficulty) vào trung bình chung global_difficulty, tăng learner_count.
    """
    counts = defaultdict(lambda: [0, 0])  # original_sense_id -> [quên, nhớ]
    for row in rows:
        counts[str(row.original_sense_id)][0 if row.rating == AGAIN else 1] += 1
    firsts = first_reviews(user, rows)

    for sense_id, (forget, remember) in counts.items():
        update = {
            'global_forget_count': F('global_forget_count') + forget,
            'global_remember_count': F('global_remember_count') + remember,
        }
        first = firsts.get(sense_id)
        if first is not None:
            # Postgres tính mọi vế SET theo giá trị cũ của dòng nên trung bình cộng dồn đúng trong 1 lệnh
            d0 = initial_difficulty(first.rating)
            update['global_difficulty'] = (F('global_difficulty') * F('learner_count') + d0) / (F('learner_count') + 1)
            update['learner_count'] = F('learner_count') + 1
        AISense.objects.filter(Q(id=sense_id) | Q(original_id=sense_id)).update(**update)
        if first is not None:
            refresh_sense_scores(Q(id=sense_id) | Q(original_id=sense_id))
