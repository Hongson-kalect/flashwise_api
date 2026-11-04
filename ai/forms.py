"""
Bảng dạng biến cách (WordForm): dựng từ thư viện biến cách (utils/morph) và tra chuỗi gõ vào về từ gốc.

  build_forms(word)                 sinh bảng chia của 1 từ gốc cho từng loại từ của nghĩa chính thức (lặp lại an toàn)
  forms_of(word_id)                 các dạng của 1 từ để trả cho app; chưa có thì dựng rồi trả
  resolve_form(form, lang)          chuỗi đã chuẩn hoá -> các (từ gốc, loại từ, nhãn) mà nó là dạng biến cách; chưa có trong bảng thì
                                    nhờ thư viện nhận ra, chỉ ghi khi từ gốc đã có trong DB (dữ liệu đã kiểm chứng, không bao giờ ghi vì người dùng gõ)
"""
from dataclasses import dataclass

from ai.models import AISense, AIWord, WordForm
from utils import morph
from utils.utils.normalize import normalize_lookup

MAX_CANDIDATES = 5


@dataclass(frozen=True)
class Candidate:
    word: AIWord
    pos: str
    tag: str


def _live_words():
    return AIWord.objects.filter(status='COMPLETED').exclude(is_deleted=True).exclude(is_active=False)


def build_forms(word):
    """Sinh dạng chia của `word` cho mọi loại từ của nghĩa chính thức. Ngôn ngữ chưa hỗ trợ sinh dạng thì không làm gì."""
    if not morph.supports_paradigm(word.language_code):
        return 0
    poss = {
        morph.normalize_pos(pos)
        for pos in AISense.objects.filter(word=word, is_official=True).exclude(is_deleted=True).values_list('pos', flat=True)
    } - {None}
    rows = [
        WordForm(word=word, language_code=word.language_code, form=entry.form.lower(), pos=pos, tag=entry.tag, irregular=entry.irregular, source='lib')
        for pos in sorted(poss) for entry in morph.paradigm(word.value, pos, word.language_code)
    ]
    WordForm.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def forms_of(word):
    """Các dạng của `word` dạng [{form, pos, tag, irregular}] để trả cho app; chưa có dòng nào thì dựng trước."""
    rows = list(WordForm.objects.filter(word=word))
    if not rows and build_forms(word):
        rows = list(WordForm.objects.filter(word=word))
    return [{'form': r.form, 'pos': r.pos, 'tag': r.tag, 'irregular': r.irregular} for r in rows]


def resolve_form(form, language_code, analyze=True):
    """
    `form` (đã normalize_lookup) là dạng biến cách của những từ gốc nào. Từ gốc phải đã COMPLETED trong DB.
    analyze=False chỉ đọc bảng (nhẹ, dùng khi từ đã có mục riêng); True thì thêm bước thư viện nhận dạng và ghi lại dòng đã kiểm chứng.
    """
    found = [
        Candidate(r.word, r.pos, r.tag)
        for r in WordForm.objects.filter(
            language_code=language_code, form=form, word__status='COMPLETED', word__is_deleted=False, word__is_active=True,
        ).exclude(word__value=form).select_related('word')[:MAX_CANDIDATES * 4]
    ]
    if not found and analyze:
        for guess in morph.analyze(form, language_code):
            word = _live_words().filter(value=normalize_lookup(guess.lemma), language_code=language_code).first()
            if word is None:
                continue
            WordForm.objects.get_or_create(
                word=word, pos=guess.pos or '', tag=guess.tag, form=form,
                defaults={'language_code': language_code, 'source': 'lookup'},
            )
            found.append(Candidate(word, guess.pos or '', guess.tag))
    return list(dict.fromkeys(found))[:MAX_CANDIDATES]


def form_of_payload(form, candidates, own_entry):
    first = candidates[0]
    return {
        'form': form, 'lemma': first.word.value, 'pos': first.pos, 'tag': first.tag, 'own_entry': own_entry,
        'candidates': [{'lemma': c.word.value, 'pos': c.pos, 'tag': c.tag} for c in candidates],
    }
