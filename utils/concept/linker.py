"""
Nối từng nghĩa vào Concept (tầng 1). Xem utils/concept/__init__.py và flashwise/docs/concept-design.md.

link_sense(sense) -> dict kết quả; link_word(word_id) -> list kết quả cho mọi nghĩa chưa nối của từ.
Chạy tuần tự qua khoá advisory của Postgres (hai worker không cùng tạo một concept). Khoá giữ trong lúc gọi Gemini, ổn cho vài nghĩa mỗi giây;
nạp hàng loạt nên dùng bước gom cụm toàn cục (xem tài liệu) thay vì gọi lần lượt.
"""
import logging
from contextlib import contextmanager

import numpy as np
from django.conf import settings
from django.db import connection

from ai.models.AISense import AISense
from ai.models.Concept import Concept, ConceptDecision
from utils.concept import text
from utils.concept.embedding import embed, from_bytes, to_bytes
from utils.concept.judge import judge as run_judge, model_name

logger = logging.getLogger(__name__)

LOCK_ID = 731001
MAX_CANDIDATES = 6
HEADWORD_MIN_COS = 0.55  # ứng viên tìm bằng headword trùng gloss vẫn phải có chút giống về nghĩa


@contextmanager
def link_lock():
    with connection.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(%s)', [LOCK_ID])
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(%s)', [LOCK_ID])


def _load_pool():
    rows = list(Concept.objects.filter(merged_into__isnull=True, is_deleted=False).exclude(embedding=None)
                .values('id', 'label', 'description_en', 'status', 'pos_class', 'embedding'))
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    return rows, np.vstack([from_bytes(r['embedding']) for r in rows])


def _candidates(sense, vec, rows, mat):
    if not rows:
        return [], []
    same_word = {str(c) for c in AISense.objects.filter(word_id=sense.word_id, concept__isnull=False).values_list('concept_id', flat=True)}
    sims = mat @ vec
    index = {str(r['id']): i for i, r in enumerate(rows)}
    picked = {}
    for i in np.argsort(-sims)[:MAX_CANDIDATES]:
        cid = str(rows[i]['id'])
        if sims[i] >= settings.CONCEPT_MIN_COS and cid not in same_word:
            picked[cid] = float(sims[i])
    for g in text.gloss_words(sense):  # concept của nghĩa tiếng Anh có headword đúng bằng gloss
        for cid in AISense.objects.filter(language_code='en', word_value__iexact=g, concept__isnull=False, is_official=True).values_list('concept_id', flat=True).distinct():
            cid = str(cid)
            if cid in index and cid not in same_word and cid not in picked and sims[index[cid]] >= HEADWORD_MIN_COS:
                picked[cid] = float(sims[index[cid]])
    top = sorted(picked.items(), key=lambda kv: -kv[1])[:MAX_CANDIDATES]
    out = []
    for cid, cos in top:
        r = rows[index[cid]]
        out.append({'id': cid, 'label': r['label'], 'pos': r['pos_class'] or '', 'description': r['description_en'], 'cos': round(cos, 4)})
    return out, top


def share_image(sense, concept):
    """Nghĩa cùng concept dùng chung ảnh: nghĩa chưa có thì lấy của anh em, nghĩa có thì chia cho anh em chưa có. Trả id các nghĩa vừa đổi ảnh."""
    changed = []
    siblings = AISense.objects.filter(concept=concept, is_official=True).exclude(id=sense.id)
    if not sense.image_context_id or not sense.image_preview:
        sib = siblings.filter(image_context__isnull=False).exclude(image_preview__isnull=True).exclude(image_preview='').first()
        if sib:
            sense.image_preview, sense.image_context_id = sib.image_preview, sib.image_context_id
            sense.save(update_fields=['image_preview', 'image_context'])
            changed.append(str(sense.id))
    if sense.image_context_id and sense.image_preview:
        lacking = list(siblings.filter(image_context__isnull=True) | siblings.filter(image_preview__isnull=True) | siblings.filter(image_preview=''))
        for other in lacking:
            other.image_preview, other.image_context_id = sense.image_preview, sense.image_context_id
            other.save(update_fields=['image_preview', 'image_context'])
            changed.append(str(other.id))
    return changed


def _attach(sense, concept, vec):
    n = concept.member_count or 0
    base = from_bytes(concept.embedding) * n if concept.embedding else np.zeros_like(vec)
    mixed = base + vec
    concept.embedding = to_bytes(mixed / max(float(np.linalg.norm(mixed)), 1e-9))
    concept.member_count = n + 1
    if (sense.language_code or '') == 'en' and concept.status != Concept.ANCHORED:
        concept.status = Concept.ANCHORED  # có nghĩa tiếng Anh thật rồi: chuyển từ tạm sang neo
        concept.label = sense.word_value or concept.label
        concept.description_en = text.en_definition(sense) or concept.description_en
    concept.save()
    sense.concept = concept
    sense.save(update_fields=['concept'])


def _create(sense, vec, definition):
    lang = sense.language_code or ''
    if lang == 'en':
        status, label = Concept.ANCHORED, sense.word_value or ''
    elif text.gloss_words(sense):
        status, label = Concept.PROVISIONAL, text.gloss_words(sense)[0]  # nhãn là gloss đầu: cả chuỗi gloss làm judge tưởng concept rộng hơn ("shoes, footwear, boots" vs shoe)
    else:
        status, label = Concept.LEXICAL_GAP, sense.word_value or ''  # không có từ tiếng Anh dùng làm gloss: khuyết từ vựng, định danh bằng mô tả
    concept = Concept.objects.create(label=label[:255], description_en=definition, embedding=to_bytes(vec), member_count=1,
                                     status=status, pos_class=text.pos_class(sense.pos))
    sense.concept = concept
    sense.save(update_fields=['concept'])
    return concept


def link_sense(sense, judge_fn=None, embed_fn=None, force=False):
    judge_fn = judge_fn or run_judge
    embed_fn = embed_fn or embed
    if sense.concept_id and not force:
        return {'sense_id': str(sense.id), 'outcome': 'already', 'concept_id': str(sense.concept_id)}
    definition = text.en_definition(sense)
    if not definition:
        ConceptDecision.objects.create(sense=sense, outcome='skipped', reason='thiếu định nghĩa tiếng Anh')
        return {'sense_id': str(sense.id), 'outcome': 'skipped', 'concept_id': None}
    vec = embed_fn([text.embed_text(sense)])[0]
    with link_lock():
        rows, mat = _load_pool()
        cands, top = _candidates(sense, vec, rows, mat)
        verdict = judge_fn({'head': text.head(sense), 'pos': sense.pos or '', 'definition': definition}, cands) if cands else {'concept_id': None, 'near': [], 'runs': [], 'agree': True}
        target = Concept.objects.filter(id=verdict['concept_id']).first() if verdict['concept_id'] else None
        if target:
            _attach(sense, target, vec)
            outcome, concept = 'linked', target
        else:
            concept = _create(sense, vec, definition)
            outcome = 'ambiguous_split' if not verdict['agree'] else ('created_gap' if concept.status == Concept.LEXICAL_GAP else 'created')
        best = cands[0]['cos'] if cands else None
        ConceptDecision.objects.create(sense=sense, concept=concept, outcome=outcome, cos=best, candidates=cands, runs=verdict['runs'], near=verdict['near'],
                                       model=model_name())
    changed = share_image(sense, concept)
    return {'sense_id': str(sense.id), 'outcome': outcome, 'concept_id': str(concept.id), 'image_changed': changed}


def link_word(word_id, **kwargs):
    results = []
    for sense in AISense.objects.filter(word_id=word_id, is_official=True, concept__isnull=True).order_by('created_at', 'id'):
        try:
            results.append(link_sense(sense, **kwargs))
        except Exception as e:  # nối hỏng không được làm hỏng từ đã lưu
            logger.warning('Nối concept lỗi cho nghĩa %s: %s', sense.id, e)
    return results
