"""Tối ưu tham số FSRS riêng cho user từ review_log. Chạy định kỳ (cron mỗi ngày) hoặc cho 1 user: `fit_fsrs_params --user 5 --force`."""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from tracking.fsrs_params import fit_user
from tracking.models import ReviewLog


class Command(BaseCommand):
    help = 'Tối ưu tham số FSRS cho user có đủ dữ liệu (mặc định mọi user có review_log); user đã tối ưu mà chưa có thêm dữ liệu thì bỏ qua'

    def add_arguments(self, parser):
        parser.add_argument('--user', type=int, help='id user, bỏ trống = mọi user có review_log')
        parser.add_argument('--force', action='store_true', help='tối ưu lại kể cả khi chưa có thêm dữ liệu')

    def handle(self, *args, user=None, force=False, **options):
        ids = [user] if user else list(ReviewLog.objects.filter(is_fsrs=True).values_list('user_id', flat=True).distinct())
        counts = {}
        for pk in ids:
            target = User.objects.filter(pk=pk).first()
            if target is None:
                self.stderr.write(f'Không có user {pk}')
                continue
            result = fit_user(target, force=force)
            counts[result['status']] = counts.get(result['status'], 0) + 1
            self.stdout.write(f"user {pk}: {result}")
        self.stdout.write(f'Xong: {counts}')
