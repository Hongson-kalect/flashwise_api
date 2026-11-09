"""
Tạo sẵn từ bằng AI theo file danh sách (seed): đẩy job vào hàng đợi redis_word cho worker, có giới hạn tốc độ.

File: mỗi dòng 1 từ (cột đầu nếu có tab), dòng trống / bắt đầu bằng # bị bỏ. Chạy lại cùng file an toàn:
từ đã xong (COMPLETED / REJECTED / BANNED) hoặc đang tạo thì bỏ qua, từ FAILED / treo quá WORD_PROCESSING_STALE_SECONDS
thì tạo lại trên cùng bản ghi.

Ví dụ: python manage.py seed_words fixtures/wordlists/en.txt --lang en --base-languages en,vi,ja,zh,ko,es,fr --rate 150
"""
import json
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai.generation import GenerationError, start_word_generation
from ai.models import AIWord
from ai.validators import is_lang
from utils.ai.rate_limit import cooldown_remaining
from utils.redis.client import get_queue_client
from utils.redis.word_init import WordCacheManager
from utils.utils.normalize import is_lookup_word, normalize_lookup

QUEUE = 'redis_word'
DONE = ('COMPLETED', 'REJECTED', 'BANNED')


def read_words(path):
    """Các từ trong file theo thứ tự, đã chuẩn hoá và bỏ trùng; trả (danh sách, số dòng không hợp lệ)."""
    seen, words, invalid = set(), [], 0
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        word = normalize_lookup(line.split('\t', 1)[0])
        if not is_lookup_word(word):
            invalid += 1
            continue
        if word not in seen:
            seen.add(word)
            words.append(word)
    return words, invalid


def read_references(path):
    """File JSONL gợi ý (data/seed/<lang>/words_ref.jsonl của app): mỗi dòng {"w", "entries": [{pos, glosses, tags}]} -> {từ: entries}."""
    refs = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        word = normalize_lookup(str(row.get('w') or ''))
        if word and row.get('entries'):
            refs.setdefault(word, row['entries'])
    return refs


def needs_generation(word_instance):
    """None = chưa có, tạo mới; True = FAILED / treo, tạo lại; False = đã xong hoặc đang tạo."""
    if word_instance is None:
        return True
    if word_instance.status in DONE:
        return False
    if word_instance.status == 'FAILED':
        return True
    updated_at = word_instance.updated_at or word_instance.created_at
    return updated_at is None or (timezone.now() - updated_at).total_seconds() > settings.WORD_PROCESSING_STALE_SECONDS


def queue_length():
    return get_queue_client().llen(QUEUE)


class Command(BaseCommand):
    help = 'Tạo sẵn từ bằng AI theo file danh sách (mỗi dòng 1 từ), đẩy vào hàng đợi worker có giới hạn tốc độ.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='File danh sách từ (UTF-8, mỗi dòng 1 từ)')
        parser.add_argument('--lang', required=True, help='Ngôn ngữ của các từ, vd en')
        parser.add_argument('--base-languages', default=','.join(settings.BASE_LANGUAGE),
                            help='Ngôn ngữ dịch sẵn, cách nhau dấu phẩy (mặc định BASE_LANGUAGE)')
        parser.add_argument('--rate', type=float, default=150, help='Số từ tối đa đẩy vào mỗi phút (mặc định 150)')
        parser.add_argument('--max-queue', type=int, default=60, help='Hàng đợi dài hơn mức này thì chờ worker làm bớt')
        parser.add_argument('--offset', type=int, default=0, help='Bỏ qua N từ đầu file')
        parser.add_argument('--limit', type=int, default=0, help='Chỉ xử lý N từ (0 = hết file)')
        parser.add_argument('--ref', help='File JSONL gợi ý từ nguồn ngoài (words_ref.jsonl): đưa vào prompt draft / refine làm tham chiếu chưa kiểm chứng')
        parser.add_argument('--user', help='username đứng tên các từ (mặc định superuser đầu tiên)')
        parser.add_argument('--with-images', action='store_true', help='Lấy ảnh Pixabay (mặc định tắt vì ~100 request/phút)')
        parser.add_argument('--dry-run', action='store_true', help='Chỉ đếm, không đẩy job')

    def handle(self, *args, **opts):
        lang = opts['lang']
        base = [code.strip() for code in opts['base_languages'].split(',') if code.strip()]
        if not is_lang(lang) or not base or not all(is_lang(code) for code in base):
            raise CommandError('--lang / --base-languages không hợp lệ')
        if opts['rate'] <= 0:
            raise CommandError('--rate phải > 0')
        if not Path(opts['path']).is_file():
            raise CommandError(f"Không thấy file {opts['path']}")

        User = get_user_model()
        user = User.objects.filter(username=opts['user']).first() if opts['user'] else User.objects.filter(is_superuser=True).order_by('id').first()
        if user is None:
            raise CommandError('Không tìm thấy user đứng tên (tạo superuser hoặc truyền --user)')

        if opts['ref'] and not Path(opts['ref']).is_file():
            raise CommandError(f"Không thấy file {opts['ref']}")
        refs = read_references(opts['ref']) if opts['ref'] else {}
        words, invalid = read_words(opts['path'])
        words = words[opts['offset']:]
        if opts['limit']:
            words = words[:opts['limit']]
        existing = {w.value: w for w in AIWord.objects.filter(language_code=lang, value__in=words)}

        cache_manager = WordCacheManager()
        interval = 60 / opts['rate']
        stats = {'queued': 0, 'retried': 0, 'skipped': 0, 'joined': 0, 'error': 0, 'invalid': invalid}
        if opts['ref']:
            stats['with_ref'] = sum(1 for w in words if w in refs)
        extra = {'base_languages': base, 'no_image': not opts['with_images']}

        for index, word in enumerate(words, 1):
            instance = existing.get(word)
            if not needs_generation(instance):
                stats['skipped'] += 1
                continue
            if opts['dry_run']:
                stats['retried' if instance else 'queued'] += 1
                continue

            self._wait_for_capacity(opts['max_queue'])
            if instance is not None:
                cache_manager.cache_word_clear_specific(lang, word)  # cache PROCESSING / ERROR cũ của lần tạo hỏng
            try:
                state, _ = start_word_generation(cache_manager, word, lang, base[0], lambda: user, existing=instance,
                                                 extra={**extra, 'reference': refs[word]} if word in refs else extra)
            except GenerationError:
                stats['error'] += 1
                continue
            if state == 'started':
                stats['retried' if instance else 'queued'] += 1
                time.sleep(interval)
            else:
                stats['joined'] += 1  # đang có người tra đúng lúc này, job đã có
            if index % 50 == 0:
                self.stdout.write(f'{index}/{len(words)} {stats}')

        self.stdout.write(self.style.SUCCESS(f"{'[dry-run] ' if opts['dry_run'] else ''}{len(words)} từ: {stats}"))

    def _wait_for_capacity(self, max_queue):
        """Chờ khi worker đang tụt lại (hàng đợi dài) hoặc Claude đang báo hết hạn mức."""
        while True:
            pause = cooldown_remaining('anthropic')
            if pause <= 0 and queue_length() < max_queue:
                return
            time.sleep(max(pause, 2))
