import json
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ai.models import AISense, AISenseMetadata, AIWord

DEFAULT_PATH = Path(settings.BASE_DIR) / 'fixtures' / 'ai_words.json'
# REJECTED cũng giữ: nạp lại thì tra từ vô nghĩa đó trả 404 ngay, khỏi gọi AI lần nữa
KEEP_STATUSES = ('COMPLETED', 'REJECTED')
USER_FIELDS = ('created_by', 'updated_by')
# Trỏ sang bảng khác không nằm trong file (nghĩa khác, ảnh): bỏ để nạp vào DB trống không lỗi khoá ngoại
SENSE_DROP_FIELDS = ('original', 'previous', 'image_context', 'image_preview')


def _dump(queryset, drop=()):
    rows = json.loads(serializers.serialize('json', queryset))
    for row in rows:
        for field in (*USER_FIELDS, *drop):
            if field in row['fields']:
                row['fields'][field] = None
    return rows


class Command(BaseCommand):
    help = (
        'Lưu / nạp lại các từ AI đã tạo (từ + nghĩa chính thức + metadata) để clear DB xong không phải gọi AI lại. '
        'export: ghi ra file; import: nạp vào DB, từ đã có (cùng value + language_code) thì bỏ qua.'
    )

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['export', 'import'])
        parser.add_argument('--path', default=str(DEFAULT_PATH))

    def handle(self, *args, action, path, **options):
        path = Path(path)
        if action == 'export':
            self.export(path)
        else:
            self.load(path)

    def export(self, path):
        words = AIWord.objects.filter(status__in=KEEP_STATUSES).exclude(is_deleted=True).exclude(is_active=False).order_by('language_code', 'value')
        senses = AISense.objects.filter(word__in=words, is_official=True).exclude(is_deleted=True).order_by('id')
        metadata = AISenseMetadata.objects.filter(id__in=senses.exclude(metadata__isnull=True).values('metadata_id')).order_by('id')

        rows = [*_dump(metadata), *_dump(words), *_dump(senses, SENSE_DROP_FIELDS)]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        self.stdout.write(f'Đã lưu {words.count()} từ, {senses.count()} nghĩa, {metadata.count()} metadata vào {path}')

    def load(self, path):
        if not path.exists():
            raise CommandError(f'Không thấy file {path}')

        objects = list(serializers.deserialize('json', path.read_text(encoding='utf-8')))
        by_model = {AIWord: [], AISense: [], AISenseMetadata: []}
        for obj in objects:
            by_model[type(obj.object)].append(obj)

        existing = set(AIWord.objects.exclude(is_active=False).values_list('value', 'language_code'))
        words = [w for w in by_model[AIWord] if (w.object.value, w.object.language_code) not in existing]
        word_ids = {w.object.id for w in words}
        senses = [s for s in by_model[AISense] if s.object.word_id in word_ids]
        meta_ids = {s.object.metadata_id for s in senses}
        known_meta = set(AISenseMetadata.objects.filter(id__in=meta_ids).values_list('id', flat=True))
        metadata = [m for m in by_model[AISenseMetadata] if m.object.id in meta_ids and m.object.id not in known_meta]

        with transaction.atomic():
            for obj in [*metadata, *words, *senses]:
                obj.save()

        self._clear_cache(words)
        skipped = len(by_model[AIWord]) - len(words)
        self.stdout.write(f'Đã nạp {len(words)} từ, {len(senses)} nghĩa, {len(metadata)} metadata (bỏ qua {skipped} từ đã có)')

    def _clear_cache(self, words):
        """Key cũ (PROCESSING / NOT_FOUND / ERROR) của từ vừa nạp sẽ che dữ liệu DB, bỏ đi. Redis không chạy thì thôi."""
        try:
            from utils.redis.word_init import WordCacheManager
            cache = WordCacheManager()
            for w in words:
                cache.cache_word_clear_specific(w.object.language_code, w.object.value)
        except Exception as e:
            self.stderr.write(f'Không dọn được cache Redis: {e}')
