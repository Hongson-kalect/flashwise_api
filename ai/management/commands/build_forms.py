"""
Dựng bảng dạng biến cách (word_form) cho các từ đã có, bằng thư viện biến cách (không tốn AI). Chạy lại an toàn.
  manage.py build_forms [--lang en,ru] [--limit N]
Sau khi seed từ nên chạy một lần; từ tạo sau đó được dựng lười khi có người tra (ai/forms.py).
"""
from django.core.management.base import BaseCommand

from ai.forms import build_forms
from ai.models import AIWord
from utils import morph


class Command(BaseCommand):
    help = 'Dựng bảng dạng biến cách cho các từ đã COMPLETED'

    def add_arguments(self, parser):
        parser.add_argument('--lang', default='', help='Danh sách ngôn ngữ, cách nhau dấu phẩy (mặc định: mọi ngôn ngữ có sinh được dạng)')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        langs = [l for l in opts['lang'].split(',') if l] or None
        words = AIWord.objects.filter(status='COMPLETED').exclude(is_deleted=True).exclude(is_active=False).order_by('language_code', 'value')
        if langs:
            words = words.filter(language_code__in=langs)
        done = made = 0
        for word in words.iterator():
            if not morph.supports_paradigm(word.language_code):
                continue
            made += build_forms(word)
            done += 1
            if opts['limit'] and done >= opts['limit']:
                break
        self.stdout.write(self.style.SUCCESS(f'{done} từ, {made} dòng dạng biến cách'))
