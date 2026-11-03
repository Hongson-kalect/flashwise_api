from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.permissions import BasePermission, SAFE_METHODS
from rest_framework.throttling import SimpleRateThrottle

User = get_user_model()

SYSTEM_USERNAME = "flashwise-system"


def get_system_user():
    """User đứng tên cho nội dung AI tạo ra khi người tra từ chưa đăng nhập. Không đăng nhập được."""
    user = User.objects.filter(username=SYSTEM_USERNAME).first()
    if user is None:
        user = User(username=SYSTEM_USERNAME)
        user.set_unusable_password()
        user.save()
    return user


def get_request_user(request):
    """User đang gọi API, chưa đăng nhập thì trả về user hệ thống."""
    if request.user and request.user.is_authenticated:
        return request.user
    return get_system_user()


def consume_daily_quota(user, scope, amount, limit):
    """
    Cộng amount vào bộ đếm theo ngày của user, False (không cộng) nếu vượt limit.
    Dùng cho việc tốn tiền theo số lượng như tạo hàng loạt từ mới bằng AI.
    """
    key = f"quota:{scope}:{user.pk}:{timezone.now():%Y%m%d}"
    cache.add(key, 0, 172800)
    used = cache.incr(key, amount)
    if used > limit:
        cache.decr(key, amount)
        return False
    return True


class IsAdminOrReadOnly(BasePermission):
    """Ai cũng đọc được, chỉ admin (is_staff) được ghi. Dùng cho dữ liệu hệ thống: từ, nghĩa chính thức, ngôn ngữ."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class IsOwnerOrAdmin(BasePermission):
    """
    Đọc: ai cũng được. Tạo: phải đăng nhập. Sửa / xoá: người tạo (created_by) hoặc admin.
    Dùng cho dữ liệu người dùng tự tạo: collection, sense custom.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        if request.user.is_staff:
            return True
        return getattr(obj, "created_by_id", None) == request.user.pk


class WordGenerateThrottle(SimpleRateThrottle):
    """
    Giới hạn số lần 1 người kích hoạt AI tạo từ mới (tốn tiền), tách riêng với giới hạn request thường.
    Không gắn vào view mà gọi tay đúng chỗ sắp đẩy việc vào hàng đợi AI.
    Rate: REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['word_generate'].
    """
    scope = "word_generate"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class AuthThrottle(SimpleRateThrottle):
    """Đăng nhập / đăng ký / quên mật khẩu: giới hạn theo IP (chặn dò mật khẩu). Rate: DEFAULT_THROTTLE_RATES['auth']."""
    scope = "auth"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class FeedbackThrottle(WordGenerateThrottle):
    """Giới hạn gửi báo cáo / góp ý (chặn spam). Rate: DEFAULT_THROTTLE_RATES['feedback']."""
    scope = "feedback"


class ExportThrottle(WordGenerateThrottle):
    """Tải toàn bộ dữ liệu của mình (nặng). Rate: DEFAULT_THROTTLE_RATES['export']."""
    scope = "export"


def active_ban(user):
    """Thông tin ban còn hiệu lực của user ({ban_reason, ban_expires_at}) hoặc None. Ban có hạn thì hết hạn tự mở."""
    from django.db.models import Q
    from user.models import UserProfile

    if not user or not user.is_authenticated:
        return None
    return (
        UserProfile.objects.filter(user=user, is_active=False)
        .filter(Q(ban_expires_at__isnull=True) | Q(ban_expires_at__gt=timezone.now()))
        .values('ban_reason', 'ban_expires_at').first()
    )


class NotBanned(BasePermission):
    """
    User bị ban (interact/moderation.py) không được làm việc người khác nhìn thấy: phát hành bộ, like, report.
    Đọc thì luôn được; học, sync, tra từ, sửa nháp riêng không gắn quyền này nên vẫn dùng bình thường.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        ban = active_ban(request.user)
        if ban:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied({'detail': 'Account is banned', 'errorCode': 'user_banned', **ban})
        return True


class SenseProposeThrottle(SimpleRateThrottle):
    """Giới hạn số lần 1 người đề xuất nghĩa mới (mỗi lần gọi Claude + Gemini). Rate: DEFAULT_THROTTLE_RATES['sense_propose']."""
    scope = "sense_propose"

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
