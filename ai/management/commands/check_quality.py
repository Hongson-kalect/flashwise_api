"""
Kiểm chất lượng bằng code (lớp 1, ai/seedflow/quality.py) cho các từ đã có trong DB.

  manage.py check_quality [--lang en] [--langs auto|en,vi,zh,es,fr] [--word w] [--limit N] [--json đường_dẫn] [--examples 3]

--langs auto: ngôn ngữ dịch mong đợi của mỗi từ là các ngôn ngữ có ít nhất một nghĩa của từ đó đã dịch (từ chạy với cấu hình ngôn ngữ cũ không bị
tính là thiếu). Chỉ đọc, không ghi DB.
"""
import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from ai.models import AIWord
from ai.models.AISense import AISense
from ai.seedflow import quality


class Command(BaseCommand):
    help = 'Kiểm chất lượng nghĩa đã lưu bằng code (không gọi AI)'

    def add_arguments(self, parser):
        parser.add_argument('--lang', default='en')
        parser.add_argument('--langs', default='auto')
        parser.add_argument('--word')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--json')
        parser.add_argument('--examples', type=int, default=3)

    def handle(self, *args, **o):
        words = AIWord.objects.filter(language_code=o['lang']).order_by('created_at')
        if o['word']:
            words = words.filter(value=o['word'])
        if o['limit']:
            words = words[:o['limit']]
        by_code, by_code_lang, samples = Counter(), Counter(), defaultdict(list)
        flagged_words, total_senses, flagged_senses = set(), 0, 0
        report = []
        for w in words:
            senses = list(AISense.objects.filter(word=w).order_by('created_at', 'id'))
            if not senses:
                continue
            dicts = [{'pos': s.pos, 'level': s.level, 'contents': s.contents or {}} for s in senses]
            if o['langs'] == 'auto':
                present = {l for d in dicts for l in ((d['contents'].get('definition') or {}).keys())}
                expected = sorted(present)
            else:
                expected = [x for x in o['langs'].split(',') if x]
            found = quality.check_word(dicts, w.value, o['lang'], expected)
            total_senses += len(senses)
            hit = set()
            for idx, iss in found:
                by_code[(iss['severity'], iss['code'])] += 1
                by_code_lang[(iss['code'], iss['lang'])] += 1
                hit.add(idx)
                flagged_words.add(w.value)
                if len(samples[iss['code']]) < o['examples']:
                    samples[iss['code']].append(f"{w.value}#{idx} [{iss['lang']}] {iss['detail']}")
                report.append({'word': w.value, 'sense_id': str(senses[idx].id) if idx is not None else None, 'sense_index': idx, **iss})
            flagged_senses += len(hit)
        n_words = words.count() if not o['limit'] else min(o['limit'], words.count())
        self.stdout.write(f'Từ {n_words}, nghĩa {total_senses}; nghĩa có vấn đề {flagged_senses} ({100 * flagged_senses / max(total_senses, 1):.0f}%), từ có vấn đề {len(flagged_words)}')
        for (sev, code), n in sorted(by_code.items(), key=lambda kv: (kv[0][0] != 'error', -kv[1])):
            self.stdout.write(f'  [{sev}] {code}: {n}')
            for s in samples[code]:
                self.stdout.write(f'      - {s}')
        if o['json']:
            with open(o['json'], 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            self.stdout.write(f'Ghi {len(report)} vấn đề vào {o["json"]}')
