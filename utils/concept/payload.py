"""Dữ liệu concept gửi cho app: nhãn + các nghĩa cùng concept ở ngôn ngữ khác (và cùng ngôn ngữ nếu thật sự thay nhau được)."""
from ai.models.AISense import AISense

MAX_EQUIVALENTS = 12


def concept_payload(sense):
    if not getattr(sense, 'concept_id', None):
        return None
    concept = sense.concept
    rows = (AISense.objects.filter(concept_id=sense.concept_id, is_official=True, is_deleted=False).exclude(id=sense.id)
            .order_by('language_code', 'word_value').values('id', 'word_id', 'word_value', 'language_code', 'pos')[:MAX_EQUIVALENTS])
    return {
        'id': str(concept.id), 'label': concept.label, 'status': concept.status,
        'equivalents': [{'id': str(r['id']), 'word_id': str(r['word_id']), 'word': r['word_value'], 'language_code': r['language_code'], 'pos': r['pos']} for r in rows],
    }
