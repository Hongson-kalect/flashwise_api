"""
Biến cách (inflection) đa ngôn ngữ, chạy theo luật / từ điển, không tốn AI.

Hai việc:
  analyze(form, lang)           chuỗi gõ vào -> các (từ gốc, loại từ, nhãn dạng) có thể: 'ran' -> run / verb / verb.past
  paradigm(lemma, pos, lang)    từ gốc + loại từ -> các dạng chia: run / verb -> [(verb.past, ran), (verb.ing, running), ...]

Mỗi ngôn ngữ dùng công cụ tốt nhất có sẵn, thư viện nào chưa cài thì ngôn ngữ đó bị bỏ qua (không lỗi):
  en           lemminflect: cả hai chiều
  ru, uk       pymorphy3:   cả hai chiều, nhãn đầy đủ (giống, số, cách, thì, ngôi)
  ja           fugashi (UniDic): chỉ analyze (nhận dạng chia từ chuỗi), không sinh bảng chia
  ko           kiwipiepy:   chỉ analyze
  còn lại      simplemma:   chỉ analyze (từ gốc, không biết loại từ), ~50 ngôn ngữ châu Âu và vài ngôn ngữ khác
  zh vi th ...: không biến cách, không có gì để làm

Nhãn dạng (`tag`) là chuỗi chữ thường ghép bằng dấu chấm, bắt đầu bằng loại từ: noun.plural, verb.past, verb.pp (phân từ quá khứ),
verb.ing, verb.3sg, adj.comparative, adj.superlative; tiếng Nga: verb.past.masc.sing, noun.gent.plur...; chưa rõ dạng thì `form`.
App ánh xạ nhãn sang tên hiển thị theo ngôn ngữ người dùng, không lưu tên hiển thị ở đây.
"""
from dataclasses import dataclass
from functools import lru_cache
import logging

logger = logging.getLogger(__name__)

NO_INFLECTION = {'zh', 'vi', 'th', 'lo', 'km', 'my', 'id', 'ms'}
GENERIC_TAG = 'form'


@dataclass(frozen=True)
class Analysis:
    lemma: str
    pos: str | None  # noun | verb | adj | adv | None (không rõ)
    tag: str


@dataclass(frozen=True)
class FormEntry:
    tag: str
    form: str
    irregular: bool = False


POS_ALIASES = {
    'noun': 'noun', 'verb': 'verb', 'adjective': 'adj', 'adj': 'adj', 'adverb': 'adv', 'adv': 'adv',
}


def normalize_pos(pos):
    """pos của nghĩa (AI trả 'adjective', 'adverb'...) -> noun | verb | adj | adv, loại khác (phrase, idiom, ...) -> None."""
    return POS_ALIASES.get((pos or '').strip().lower())


def base_lang(lang):
    return (lang or '').split('-')[0].lower()


# ---------------------------------------------------------------- tiếng Anh (lemminflect)
_PENN_TAG = {
    'NNS': 'noun.plural', 'VBZ': 'verb.3sg', 'VBD': 'verb.past', 'VBN': 'verb.pp', 'VBG': 'verb.ing',
    'JJR': 'adj.comparative', 'JJS': 'adj.superlative', 'RBR': 'adv.comparative', 'RBS': 'adv.superlative',
}
_UPOS = {'noun': 'NOUN', 'verb': 'VERB', 'adj': 'ADJ', 'adv': 'ADV'}
_UPOS_BACK = {v: k for k, v in _UPOS.items()}


def _en_regular(lemma, tag, form):
    """Dạng chia theo luật thông thường (+s/es/ies, +ed/d/ied, +ing, +er/est, nhân đôi phụ âm cuối); khác thì coi là bất quy tắc."""
    stems = {lemma, lemma[:-1], lemma + (lemma[-1] if lemma else '')}
    suffixes = {
        'noun.plural': ('s', 'es', 'ies'), 'verb.3sg': ('s', 'es', 'ies'), 'verb.past': ('ed', 'd', 'ied'),
        'verb.pp': ('ed', 'd', 'ied'), 'verb.ing': ('ing',), 'adj.comparative': ('er', 'r', 'ier'),
        'adj.superlative': ('est', 'st', 'iest'), 'adv.comparative': ('er', 'r', 'ier'), 'adv.superlative': ('est', 'st', 'iest'),
    }.get(tag, ())
    if lemma.endswith('y') and lemma[:-1] + 'i' in {form[:len(lemma)]}:
        return True
    return any(form == stem + suffix for stem in stems for suffix in suffixes)


def _en_analyze(form):
    from lemminflect import getAllInflections, getAllLemmas

    out = []
    for upos, lemmas in getAllLemmas(form).items():
        pos = _UPOS_BACK.get(upos)
        if not pos:
            continue
        for lemma in lemmas:
            if lemma.lower() == form.lower():
                continue
            for penn, forms in getAllInflections(lemma, upos=upos).items():
                tag = _PENN_TAG.get(penn)
                if tag and form in forms:
                    out.append(Analysis(lemma, pos, tag))
    return out


def _en_paradigm(lemma, pos):
    from lemminflect import getAllInflections

    out = []
    for penn, forms in getAllInflections(lemma, upos=_UPOS[pos]).items():
        tag = _PENN_TAG.get(penn)
        if not tag:
            continue
        for form in forms:
            if form.lower() != lemma.lower():
                out.append(FormEntry(tag, form, irregular=not _en_regular(lemma, tag, form)))
    return out


# ---------------------------------------------------------------- tiếng Nga / Ukraina (pymorphy3)
_PM_POS = {'NOUN': 'noun', 'VERB': 'verb', 'INFN': 'verb', 'ADJF': 'adj', 'ADJS': 'adj', 'COMP': 'adj', 'ADVB': 'adv'}
_PM_ORDER = ['past', 'pres', 'futr', 'impr', 'indc', '1per', '2per', '3per', 'masc', 'femn', 'neut', 'sing', 'plur',
             'nomn', 'gent', 'datv', 'accs', 'ablt', 'loct', 'voct', 'gen2', 'acc2', 'loc2', 'Supr', 'Cmp2']


@lru_cache(maxsize=4)
def _pymorphy(lang):
    import pymorphy3

    return pymorphy3.MorphAnalyzer(lang=lang)


def _pm_tag(pos, grammemes):
    parts = [g for g in _PM_ORDER if g in grammemes]
    return '.'.join([pos, *parts]) if parts else f'{pos}.{GENERIC_TAG}'


def _pm_analyze(form, lang):
    out = []
    for p in _pymorphy(lang).parse(form):
        pos = _PM_POS.get(p.tag.POS)
        if not pos or p.normal_form == form.lower():
            continue
        out.append(Analysis(p.normal_form, pos, _pm_tag(pos, set(p.tag.grammemes))))
    return list(dict.fromkeys(out))


def _pm_paradigm(lemma, pos, lang):
    parsed = [p for p in _pymorphy(lang).parse(lemma) if _PM_POS.get(p.tag.POS) == pos and p.normal_form == lemma.lower()]
    if not parsed:
        return []
    out, seen = [], set()
    for f in parsed[0].lexeme:
        if f.word == lemma.lower() or _PM_POS.get(f.tag.POS) != pos:
            continue
        key = (f.word, _pm_tag(pos, set(f.tag.grammemes)))
        if key not in seen:
            seen.add(key)
            out.append(FormEntry(key[1], key[0]))
    return out


# ---------------------------------------------------------------- tiếng Nhật (fugashi + UniDic)
_JA_AUX_TAG = {'タ': 'past', 'ナイ': 'negative', 'マス': 'polite', 'タイ': 'desiderative', 'レル': 'passive', 'セル': 'causative', 'ヌ': 'negative'}
_JA_CFORM_TAG = {'未然形': 'mizen', '連用形': 'renyou', '仮定形': 'katei', '命令形': 'imperative', '意志推量形': 'volitional'}


@lru_cache(maxsize=1)
def _ja_tagger():
    import fugashi

    return fugashi.Tagger()


def _ja_analyze(form):
    tokens = list(_ja_tagger()(form))
    if not tokens or tokens[0].feature.pos1 not in ('動詞', '形容詞'):
        return []
    rest = tokens[1:]
    if any(t.feature.pos1 not in ('助動詞', '助詞') for t in rest):
        return []  # còn từ khác phía sau: là cụm / câu, không phải một dạng chia
    lemma = (tokens[0].feature.lemma or '').split('-')[0]
    if not lemma or lemma == form:
        return []
    pos = 'verb' if tokens[0].feature.pos1 == '動詞' else 'adj'
    parts = []
    for t in rest:
        ctype = (t.feature.cType or '').split('-')[0]
        if t.surface == 'て' or t.surface == 'で':
            parts.append('te')
        elif ctype in _JA_AUX_TAG or (t.feature.cType or '').rsplit('-', 1)[-1] in _JA_AUX_TAG:
            parts.append(_JA_AUX_TAG.get(ctype) or _JA_AUX_TAG[(t.feature.cType or '').rsplit('-', 1)[-1]])
    if not parts:
        cform = (tokens[0].feature.cForm or '').split('-')[0]
        parts = [_JA_CFORM_TAG.get(cform, GENERIC_TAG)]
    return [Analysis(lemma, pos, '.'.join([pos, *dict.fromkeys(parts)]))]


# ---------------------------------------------------------------- tiếng Hàn (kiwipiepy)
@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi

    return Kiwi()


def _ko_analyze(form):
    tokens = _kiwi().tokenize(form)
    if not tokens or tokens[0].tag not in ('VV', 'VA', 'VX'):
        return []
    if any(not t.tag.startswith(('E', 'J', 'X')) for t in tokens[1:]):
        return []
    lemma = tokens[0].form + '다'
    if lemma == form:
        return []
    pos = 'adj' if tokens[0].tag == 'VA' else 'verb'
    past = any(t.tag == 'EP' and t.form in ('었', '았', '였', 'ㅆ') for t in tokens[1:])
    return [Analysis(lemma, pos, f'{pos}.past' if past else f'{pos}.{GENERIC_TAG}')]


# ---------------------------------------------------------------- các ngôn ngữ khác (simplemma)
def _generic_analyze(form, lang):
    import simplemma

    try:
        lemma = simplemma.lemmatize(form, lang=lang)
    except ValueError:  # ngôn ngữ không được hỗ trợ
        return []
    return [Analysis(lemma, None, GENERIC_TAG)] if lemma and lemma.lower() != form.lower() else []


# ---------------------------------------------------------------- API
def analyze(form, lang):
    """Các cách hiểu `form` như một dạng biến cách của từ khác; rỗng nếu không phải / ngôn ngữ chưa hỗ trợ. Không bao giờ ném lỗi."""
    code, form = base_lang(lang), (form or '').strip()
    if not form or code in NO_INFLECTION:
        return []
    try:
        if code == 'en':
            return _en_analyze(form)
        if code in ('ru', 'uk'):
            return _pm_analyze(form, code)
        if code == 'ja':
            return _ja_analyze(form)
        if code == 'ko':
            return _ko_analyze(form)
        return _generic_analyze(form, code)
    except ImportError:
        logger.warning('Thiếu thư viện biến cách cho %s', code)
    except Exception:
        logger.exception('analyze(%r, %s) lỗi', form, code)
    return []


def paradigm(lemma, pos, lang):
    """Các dạng chia của (lemma, pos); rỗng nếu ngôn ngữ / loại từ chưa hỗ trợ sinh dạng."""
    code, pos = base_lang(lang), POS_ALIASES.get(pos or '', None)
    if not lemma or not pos or code in NO_INFLECTION:
        return []
    try:
        if code == 'en':
            return _en_paradigm(lemma, pos)
        if code in ('ru', 'uk'):
            return _pm_paradigm(lemma, pos, code)
    except ImportError:
        logger.warning('Thiếu thư viện biến cách cho %s', code)
    except Exception:
        logger.exception('paradigm(%r, %s, %s) lỗi', lemma, pos, code)
    return []


def supports_paradigm(lang):
    return base_lang(lang) in ('en', 'ru', 'uk')
