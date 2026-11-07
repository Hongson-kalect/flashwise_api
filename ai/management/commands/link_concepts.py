"""
Nối các nghĩa chưa có concept vào Concept (tầng 1), tiếng Anh trước để làm neo.
  manage.py link_concepts [--lang en,es] [--word apple] [--limit N] [--reset] [--judge-runs 2]
--reset xoá mọi concept và quyết định rồi nối lại từ đầu (dùng khi đo / sửa ngưỡng).
"""
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand

from ai.models.AISense import AISense
from ai.models.Concept import Concept, ConceptDecision
from utils.concept.linker import link_sense


class Command(BaseCommand):
    help = 'Nối nghĩa chưa có concept vào Concept (tầng 1)'

    def add_arguments(self, parser):
        parser.add_argument('--lang', default='', help='Chỉ các ngôn ngữ này, cách nhau dấu phẩy')
        parser.add_argument('--word', default='', help='Chỉ các từ này, cách nhau dấu phẩy')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--reset', action='store_true')
        parser.add_argument('--judge-runs', type=int, default=0)

    def handle(self, *args, **opts):
        if opts['judge_runs']:
            settings.CONCEPT_JUDGE_RUNS = opts['judge_runs']
        if opts['reset']:
            AISense.objects.update(concept=None)
            ConceptDecision.objects.all().delete()
            Concept.objects.all().delete()
            self.stdout.write('Đã xoá concept cũ')
        qs = AISense.objects.filter(is_official=True, concept__isnull=True, is_deleted=False)
        if opts['lang']:
            qs = qs.filter(language_code__in=opts['lang'].split(','))
        if opts['word']:
            qs = qs.filter(word_value__in=opts['word'].split(','))
        senses = sorted(qs, key=lambda s: (s.language_code != 'en', s.created_at))  # tiếng Anh trước
        if opts['limit']:
            senses = senses[:opts['limit']]
        counts = Counter()
        for s in senses:
            try:
                r = link_sense(s)
            except Exception as e:
                counts['error'] += 1
                self.stderr.write(f'{s.word_value} [{s.language_code}]: {e}')
                continue
            counts[r['outcome']] += 1
        self.stdout.write(f'{len(senses)} nghĩa: ' + ', '.join(f'{k} {v}' for k, v in sorted(counts.items())))
