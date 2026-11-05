from django.core.management.base import BaseCommand

from ai.scores import refresh_sense_scores


class Command(BaseCommand):
    help = "Tính lại AISense.score cho mọi nghĩa: chạy sau khi đổi công thức / trọng số trong ai/scores.py"

    def handle(self, *args, **options):
        self.stdout.write("Đã cập nhật score cho %d nghĩa" % refresh_sense_scores())
