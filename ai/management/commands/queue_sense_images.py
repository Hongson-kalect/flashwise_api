from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from ai.models.AISense import AISense
from utils.image_gen import comfy
from utils.image_gen.service import enqueue_senses, sense_subject
from utils.image_gen.subject import build_prompt, image_key, seed_for


class Command(BaseCommand):
    help = ('Xếp việc vẽ ảnh AI (ComfyUI) cho các nghĩa chưa có ảnh vào hàng đợi IMAGE_GEN_QUEUE; worker vẽ lần lượt. '
            'Nghĩa phải có chủ thể ảnh (metadata.image_metadata.subject, do bước metadata sinh). '
            'Chủ thể đã có ảnh thì gắn luôn, không vẽ lại. --try "chủ thể" vẽ thử 1 ảnh, không ghi DB.')

    def add_arguments(self, parser):
        parser.add_argument('--word', help='Chỉ nghĩa của từ này (đúng giá trị, vd kettle)')
        parser.add_argument('--lang', help='Ngôn ngữ của từ (dùng kèm --word hoặc để lọc)')
        parser.add_argument('--limit', type=int, default=0, help='Tối đa N nghĩa (0 = hết)')
        parser.add_argument('--dry-run', action='store_true', help='Chỉ đếm nghĩa đủ điều kiện, không xếp job')
        parser.add_argument('--try', dest='try_subject', help='Vẽ thử 1 chủ thể bằng ComfyUI, lưu PNG ra --out, không ghi DB')
        parser.add_argument('--out', default='image_gen_try.png', help='File PNG cho --try')

    def handle(self, *args, **opts):
        if opts['try_subject']:
            return self._try(opts['try_subject'], opts['out'])

        senses = AISense.objects.filter(Q(image_preview__isnull=True) | Q(image_preview='')).select_related('metadata')
        if opts['word']:
            senses = senses.filter(word_value=opts['word'])
        if opts['lang']:
            senses = senses.filter(language_code=opts['lang'])
        senses = senses.order_by('created_at')
        if opts['limit']:
            senses = senses[:opts['limit']]

        eligible, no_subject = [], 0
        for sense in senses:
            if sense_subject(sense)[0]:
                eligible.append(sense)
            else:
                no_subject += 1
        self.stdout.write(f'{len(eligible)} nghĩa đủ điều kiện, {no_subject} nghĩa chưa có chủ thể ảnh (từ tạo trước khi có image_subject, hoặc loại "none")')
        if opts['dry_run'] or not eligible:
            return
        stats = enqueue_senses(eligible)
        self.stdout.write(self.style.SUCCESS(
            f"Đã gắn ngay {stats['attached']} (chủ thể đã có ảnh), xếp {stats['queued']} job vẽ mới, "
            f"{stats['joined']} nghĩa chờ chung job, bỏ qua {stats['skipped']}"))

    def _try(self, subject, out):
        key = image_key(subject)
        if not key:
            raise CommandError('Chủ thể rỗng')
        try:
            png = comfy.generate(build_prompt(subject), seed_for(key))
        except (comfy.ComfyUnavailable, comfy.ComfyError) as e:
            raise CommandError(str(e))
        with open(out, 'wb') as f:
            f.write(png)
        self.stdout.write(self.style.SUCCESS(f'Đã lưu {out} ({len(png) // 1024} KB)'))
