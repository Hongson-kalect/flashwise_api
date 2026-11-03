"""
Lưu / dựng dữ liệu cho đề xuất nghĩa mới của người dùng (endpoint ai-senses/propose, cổng AI ở utils/ai/sense_gate.py).
Nghĩa được tạo với is_frozen=True và ai_review['proposal'] ghi lại phán quyết của cổng:
  status 'approved'  -> nghĩa chính thức ngay (AI chấp nhận)
  status 'pending'   -> nghĩa riêng của tác giả, chờ admin duyệt (ai.queries.visible_sense_q giấu khỏi người khác)
"""
from django.db import transaction

from ai.models.AISense import AISense
from ai.models.AISenseMetadata import AISenseMetadata
from ai.validators import clean_contents, clean_metadata
from utils.ai import sense_gate
from utils.redis.word_init import WordCacheManager
from utils.utils.best_effort import best_effort


def existing_senses_of(word):
    """Các nghĩa chính thức hiện có của từ, dạng gửi cho cổng AI (kèm id để đổi 'trùng #n' thành id nghĩa)."""
    rows = []
    senses = AISense.objects.filter(word=word, is_official=True).exclude(is_deleted=True).exclude(is_active=False).order_by('id')
    for sense in senses:
        contents = sense.contents or {}
        examples = contents.get('examples') or [{}]
        rows.append({
            'id': str(sense.id),
            'pos': sense.pos,
            'definition': (contents.get('definition') or {}).get(word.language_code, {}).get('value', ''),
            'example': ((examples[0] or {}).get(word.language_code) or {}).get('value', ''),
        })
    return rows


def duplicate_sense(result):
    """Nghĩa đã có mà cổng báo trùng (theo số thứ tự trong danh sách đã gửi), hoặc None."""
    index = result['claude'].get('duplicate_of') or 0
    shown = result['shown']
    if 1 <= index <= len(shown):
        return AISense.objects.filter(pk=shown[index - 1]['id']).first()
    return None


def proposal_review(result, text, user_language_code):
    """Ghi vào ai_review: văn bản gốc của người dùng + phán quyết từng lớp để admin xem."""
    def brief(layer):
        return None if layer is None else {k: layer[k] for k in ('verdict', 'confidence', 'reason', 'model', 'injection_attempt', 'is_offensive_or_spam')}

    return {
        'proposal': {
            'status': 'approved' if result['decision'] == 'official' else 'pending',
            'source': 'user',
            'text': text,
            'user_language': user_language_code,
            'decision': result['decision'],
            'reasons': result['reasons'],
            'claude': brief(result['claude']),
            'gemini': brief(result['gemini']),
            'gemini_status': result['gemini_status'],
            'translation_problem': result['claude'].get('translation_problem'),
        },
    }


def create_proposed_sense(user, word, result, text, user_language_code):
    """Tạo nghĩa từ kết quả cổng. decision official -> chính thức; admin_review -> riêng của tác giả, chờ duyệt."""
    claude = result['claude']
    built = sense_gate.assemble_sense(word.language_code, user_language_code, claude['sense'], claude['sense']['translation'], claude['sense']['metadata'])
    official = result['decision'] == 'official'
    with transaction.atomic():
        metadata = AISenseMetadata.objects.create(**clean_metadata(built['metadata']), created_by=user)
        sense = AISense.objects.create(
            word=word, word_value=word.value, language_code=word.language_code, metadata=metadata,
            contents=clean_contents(built['contents']), pos=(built['pos'] or '')[:20], level=(built['level'] or '')[:20],
            register=[str(r)[:50] for r in built['register']][:5], ipas=built['ipas'],
            is_official=official, is_frozen=True, is_ai_created=False, created_by=user,
            quality_status='draft', ai_pass=1, ai_model=claude['model'], ai_review=proposal_review(result, text, user_language_code),
        )
    if official:
        best_effort(WordCacheManager().cache_word_clear_specific, word.language_code, word.value)
    return sense
