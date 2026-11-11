from django.core.management.base import BaseCommand

from ai.models.AISense import AISense
from core.models.ImageLibrary import ImageContext
from utils.image_gen.brief import consistent
from utils.image_gen.service import PROVIDER


FALLBACK_PREFIX = 'A very simple scene that shows the meaning of'  # brief dự phòng cũ (đã bỏ): ảnh vẽ từ nó thường gần như trống


class Command(BaseCommand):
    help = ('Liệt kê ảnh AI cần người xem: (1) nghĩa brief đánh dấu `manual` (không vẽ, chờ ảnh thêm tay), '
            '(2) ảnh vẽ ra nhỏ bất thường (gần như trống: khoảng 11-12 KB, ảnh tốt 17-30 KB), '
            '(3) nghĩa có chủ thể (khoá ảnh) không khớp mô tả vẽ, (4) nghĩa còn brief dự phòng cũ. Kích thước chỉ là gợi ý: '
            'ảnh đơn giản hợp lệ cũng có thể nhỏ. Chỉ đọc, không ghi DB.')

    def add_arguments(self, parser):
        parser.add_argument('--max-bytes', type=int, default=13000, help='Ảnh nhỏ hơn mức này (byte) bị coi là nghi trống')
        parser.add_argument('--word', help='Chỉ nghĩa của từ này')
        parser.add_argument('--limit', type=int, default=0, help='Tối đa N dòng mỗi mục (0 = hết)')

    def handle(self, *args, **opts):
        senses = AISense.objects.filter(language_code='en', is_deleted=False).select_related('metadata', 'image_context')
        if opts['word']:
            senses = senses.filter(word_value=opts['word'])
        limit = opts['limit']

        manual, mismatch, old_fallback = [], [], []
        for sense in senses.order_by('word_value', 'created_at'):
            meta = (sense.metadata.image_metadata if sense.metadata_id and sense.metadata else None) or {}
            if meta.get('kind') == 'manual' and not sense.image_preview:
                manual.append(sense)
            elif (meta.get('prompt') or '').startswith(FALLBACK_PREFIX):
                old_fallback.append(sense)
            elif meta.get('subject') and meta.get('prompt') and not consistent(meta['subject'], meta['prompt']):
                mismatch.append(sense)

        small = []
        contexts = ImageContext.objects.filter(provider=PROVIDER).prefetch_related('images')
        for context in contexts:
            for image in context.images.all():
                size = (image.metadata or {}).get('bytes')
                if isinstance(size, int) and size < opts['max_bytes']:
                    small.append((context.description, size, AISense.objects.filter(image_context=context).count()))
        small.sort(key=lambda x: x[1])

        def cut(rows):
            return rows[:limit] if limit else rows

        self.stdout.write(self.style.WARNING(f'== Chờ ảnh tay (brief manual): {len(manual)}'))
        for s in cut(manual):
            self.stdout.write(f'  {s.word_value} ({s.pos}) [{s.id}]')
        self.stdout.write(self.style.WARNING(f'== Ảnh nhỏ < {opts["max_bytes"]} byte (nghi trống): {len(small)}'))
        for key, size, uses in cut(small):
            self.stdout.write(f'  {size:>6} B  "{key}"  dùng cho {uses} nghĩa')
        self.stdout.write(self.style.WARNING(f'== Còn brief dự phòng cũ (nên viết lại brief rồi vẽ lại): {len(old_fallback)}'))
        for s in cut(old_fallback):
            self.stdout.write(f'  {s.word_value} ({s.pos}) "{s.metadata.image_metadata.get("subject")}"')
        self.stdout.write(self.style.WARNING(f'== Chủ thể không khớp mô tả: {len(mismatch)}'))
        for s in cut(mismatch):
            meta = s.metadata.image_metadata
            self.stdout.write(f'  {s.word_value} ({s.pos}): "{meta["subject"]}" <- {meta["prompt"][:80]}')
