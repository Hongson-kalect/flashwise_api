"""
Kiểm chéo bản dịch của các từ đã có trong DB bằng Claude Haiku (lớp 2, ai/seedflow/verify.py) và ghi kết quả vào AISense
(verify_state theo ngôn ngữ, ai_verify). Ngôn ngữ nào người đã verify (có trong `verify`) không bị đụng tới.

  manage.py verify_translations [--lang en] [--langs auto|vi,zh,...] [--word w] [--limit N] [--redo] [--status] [--concurrency 4]

Mặc định chỉ kiểm nghĩa chưa có verify_state cho đủ ngôn ngữ; --redo kiểm lại cả nghĩa đã kiểm (trừ ngôn ngữ người đã verify).
--status chỉ thống kê, không gọi AI. Chạy online (lô nhỏ); seed_flow có stage verify dùng batch giảm giá.
"""
import asyncio
from collections import Counter

from django.core.management.base import BaseCommand

from ai.models import AISense, AIWord
from ai.seedflow import llm, verify, verify_db


class Command(BaseCommand):
    help = 'Haiku kiểm chéo bản dịch của Gemini cho các từ đã lưu'

    def add_arguments(self, parser):
        parser.add_argument('--lang', default='en')
        parser.add_argument('--langs', default='auto')
        parser.add_argument('--word')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--redo', action='store_true')
        parser.add_argument('--status', action='store_true')
        parser.add_argument('--concurrency', type=int, default=4)
        parser.add_argument('--chunk', type=int, default=20, help='số từ mỗi lượt gọi rồi ghi')

    def handle(self, *args, **o):
        if o['status']:
            return self.status(o['lang'])
        words = AIWord.objects.filter(language_code=o['lang'], status='COMPLETED').order_by('created_at')
        if o['word']:
            words = words.filter(value=o['word'])
        ids = list(words.values_list('id', flat=True))
        if o['limit']:
            ids = ids[:o['limit']]
        total, tin, tout = Counter(), 0, 0
        for start in range(0, len(ids), o['chunk']):
            group = ids[start:start + o['chunk']]
            requests, meta = [], {}
            for wid in group:
                langs = self.langs_of(wid, o['langs'], o['lang'])
                r, m = verify_db.build_requests([wid], o['lang'], langs, redo=o['redo'])
                requests += r
                meta.update(m)
            if not requests:
                continue
            results = asyncio.run(llm.run_online(requests, concurrency=o['concurrency']))
            tin += sum(r['tokens_in'] for r in results.values())
            tout += sum(r['tokens_out'] for r in results.values())
            total.update(verify_db.apply_results(meta, results, o['lang']))
            total['words'] += len(meta)
        self.stdout.write(f"Từ đã kiểm {total['words']}; cặp (nghĩa, ngôn ngữ): ok {total['ok']}, sửa {total['fixed']}, nghi {total['flagged']}; "
                          f"bỏ qua (người đã verify) {total['skipped']}; lệnh lỗi {total['failed']}; vấn đề ở bản gốc {total['issues']}")
        self.stdout.write(f'Token {tin} vào / {tout} ra, ~${llm.cost(tin, tout):.4f} (giá online)')

    @staticmethod
    def langs_of(word_id, spec, language_code):
        if spec != 'auto':
            return [x for x in spec.split(',') if x]
        found = set()
        for s in verify_db.word_senses(word_id):
            c = s.contents or {}
            found |= set((c.get('definition') or {}).keys()) | set((c.get('translations') or {}).keys())
        return sorted(found - {language_code})

    def status(self, lang):
        qs = AISense.objects.filter(language_code=lang, is_deleted=False, is_official=True)
        rows = list(qs.only('verify', 'ai_verify', 'verify_state'))
        human = sum(1 for s in rows if s.verify)
        ai = sum(1 for s in rows if s.ai_verify)
        both = sum(1 for s in rows if s.verify and s.ai_verify)
        checked = sum(1 for s in rows if s.verify_state)
        self.stdout.write(f"Nghĩa {len(rows)}: có ngôn ngữ do người verify {human}, có ngôn ngữ ai_verify {ai}, cả hai {both}, đã qua Haiku {checked}")
        per_lang = Counter(l for s in rows for l in (s.ai_verify or []))
        self.stdout.write('ai_verify theo ngôn ngữ: ' + ', '.join(f'{k} {n}' for k, n in sorted(per_lang.items())))
        states = Counter()
        for s in qs.exclude(verify_state={}).only('verify_state'):
            for k, v in (s.verify_state or {}).items():
                if not k.startswith('_') and isinstance(v, dict):
                    states[v.get('state')] += 1
        self.stdout.write('Cặp (nghĩa, ngôn ngữ): ' + ', '.join(f'{k} {n}' for k, n in sorted(states.items())))
