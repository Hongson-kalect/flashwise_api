"""
Keo giữa DB và lớp kiểm chéo (ai.seedflow.verify): lấy các nghĩa của từ đã lưu, dựng yêu cầu Haiku, ghi kết quả về AISense.
Dùng cho stage `verify` của seed_flow và lệnh `verify_translations` (chạy bù cho từ đã có).
"""
import logging

from ai.models import AISense, AIWord
from ai.seedflow import llm, verify

logger = logging.getLogger(__name__)


def word_senses(word_id):
    """Nghĩa đang dùng của từ, theo thứ tự cố định (đúng thứ tự đưa cho model và dùng lại khi ghi kết quả)."""
    return list(AISense.objects.filter(word_id=word_id, is_deleted=False, is_official=True).order_by('created_at', 'id'))


def _needs_check(senses, langs, language_code, redo):
    for s in senses:
        human = verify.human_verified(s)
        wanted = [l for l in verify.sense_langs({'contents': s.contents or {}}, langs, language_code) if not verify.is_human(human, l)]
        if not wanted:
            continue
        if redo or not all((s.verify_state or {}).get(l) for l in wanted):
            return True
    return False


def build_requests(word_ids, language_code, langs, redo=False, model=None):
    """
    ({custom_id = id từ: yêu cầu}, {custom_id: (từ, nghĩa, ngôn ngữ)}). Từ không còn nghĩa nào cần kiểm (đã có verify_state cho mọi ngôn ngữ,
    hoặc người đã verify) bị bỏ qua, trừ khi redo=True.
    """
    requests, meta = [], {}
    for wid in word_ids:
        word = AIWord.objects.filter(id=wid).first()
        senses = word_senses(wid) if word else []
        if not senses or not _needs_check(senses, langs, language_code, redo):
            continue
        dicts = [{'pos': s.pos, 'level': s.level, 'contents': s.contents or {}} for s in senses]
        requests.append(verify.build_request(str(wid), word.value, language_code, dicts, langs, model=model,
                                             hints=verify.layer1_hints(word.value, language_code, dicts, langs)))
        meta[str(wid)] = (word.value, senses, langs)
    return requests, meta


def apply_results(meta, results, language_code, model=None):
    """Ghi kết quả của một lô. Trả tổng {'ok','fixed','flagged','skipped','issues','failed'}; lệnh lỗi thì không đổi gì ở từ đó."""
    totals = {'ok': 0, 'fixed': 0, 'flagged': 0, 'skipped': 0, 'issues': 0, 'failed': 0}
    for custom_id, (word, senses, langs) in meta.items():
        data = llm.parse_json(results.get(custom_id))
        if data is None:
            totals['failed'] += 1
            logger.warning('verify: lệnh của từ %s lỗi hoặc không đọc được, bỏ qua', word)
            continue
        for key, n in verify.apply_to_senses(word, language_code, senses, langs, data, model=model).items():
            totals[key] += n
    return totals
