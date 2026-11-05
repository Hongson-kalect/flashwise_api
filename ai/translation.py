"""
Thiếu bản dịch của nghĩa cho 1 ngôn ngữ người xem: phát hiện, đếm và đẩy việc dịch (ai/jobs.queue_translation).

- Nghĩa thường: bản dịch thiếu của chính contents.
- Nghĩa riêng dạng delta (user sửa 1 nghĩa có sẵn, contents rỗng, trỏ về `original`): phần user sửa (definition / usage /
  translations trong delta) chỉ có ngôn ngữ user viết, người ngôn ngữ khác xem thì thiếu. Phần này dịch rồi GHI VÀO delta của chính
  nghĩa riêng (job mang `target: 'delta'`, worker ghi vào sense.delta), không đụng contents của nghĩa gốc dùng chung.
  Phần user không sửa vẫn do bản dịch của nghĩa gốc lo như cũ.
- Dịch lười: chỉ đẩy việc khi gọi `queue_gaps` (chọn thẻ học, tra từ, endpoint prefetch), không đẩy khi chỉ tải bộ.
"""
from collections import defaultdict
from dataclasses import dataclass

from ai.jobs import queue_lazy_translation, queue_translation
from ai.serializers.AISense import AISenseSerializer
from utils.utils.sense_handle import get_user_lang_sense

DELTA_TRANSLATED_FIELDS = ('definition', 'usage', 'translations')  # examples của delta không dịch tự động (khác ngôn ngữ nguồn)


@dataclass
class Gap:
    sense_id: str  # nghĩa đang xem bị thiếu
    target: object  # nghĩa chứa nội dung cần dịch (chính nó, nghĩa gốc hoặc nghĩa riêng dạng delta)
    source: str  # ngôn ngữ của nội dung gốc cần dịch
    missing: dict
    data: dict
    delta: bool = False


def _delta_view(sense):
    """Phần user sửa trong delta (kèm id / từ loại) để dịch, cùng ngôn ngữ nguồn của nó. (None, None) nếu không có gì để dịch."""
    delta = sense.delta if isinstance(sense.delta, dict) else {}
    view = {key: delta[key] for key in DELTA_TRANSLATED_FIELDS if isinstance(delta.get(key), dict) and delta[key]}
    definition = view.get('definition')
    if not definition:
        return None, None
    # Ngôn ngữ nguồn: ngôn ngữ của từ nếu user viết bằng nó, không thì ngôn ngữ đầu tiên có chữ trong định nghĩa
    candidates = [sense.language_code, *definition.keys()]
    source = next((lang for lang in candidates if isinstance(definition.get(lang), dict) and definition[lang].get('value')), None)
    return (view, source) if source else (None, None)


def sense_gaps(sense, user_language_code):
    """Các chỗ thiếu bản dịch (0, 1 hoặc 2 Gap) của 1 nghĩa với 1 ngôn ngữ."""
    gaps = []
    target = sense if sense.contents else sense.original
    if target is not None and target.contents and user_language_code != target.language_code:
        _, missing = get_user_lang_sense(target.language_code, user_language_code, target.contents, str(target.id))
        if missing:
            gaps.append(Gap(str(sense.id), target, target.language_code, missing, AISenseSerializer(target).data))

    if not sense.contents and sense.original_id:
        view, source = _delta_view(sense)
        if view and source != user_language_code:
            _, missing = get_user_lang_sense(source, user_language_code, view, str(sense.id))
            if missing:
                data = {'id': str(sense.id), 'word_value': sense.word_value, 'pos': sense.pos, 'contents': view, 'target': 'delta'}
                gaps.append(Gap(str(sense.id), sense, source, missing, data, delta=True))
    return gaps


def collect_gaps(senses, user_language_code):
    gaps = []
    for sense in senses:
        gaps.extend(sense_gaps(sense, user_language_code))
    return gaps


def missing_sense_ids(gaps):
    """Id các nghĩa (đang xem) còn thiếu bản dịch, giữ thứ tự, không trùng."""
    return list(dict.fromkeys(gap.sense_id for gap in gaps))


def queue_gaps(gaps, user_language_code, lazy=False):
    """
    Đẩy việc dịch theo (từ, ngôn ngữ nguồn); nhiều nghĩa delta cùng 1 bản gốc chỉ dịch bản gốc 1 lần.
    lazy=True: xếp vào hàng đợi nền (chậm, chỉ chạy khi hệ thống rảnh, chỉ cặp ngôn ngữ phổ biến) thay vì hàng đợi gấp.
    """
    push = queue_lazy_translation if lazy else queue_translation
    groups = defaultdict(dict)
    for gap in gaps:
        groups[(gap.target.word_id, gap.source)][(str(gap.target.id), gap.delta)] = gap
    for (word_id, source), by_target in groups.items():
        rows = list(by_target.values())
        push(word_id, source, user_language_code, [g.missing for g in rows], [g.data for g in rows])
