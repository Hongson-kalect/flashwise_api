"""
Tài khoản thật: đăng nhập Google / Apple (id token từ SDK trên máy, user/providers.py), email + mật khẩu,
quên / đổi mật khẩu, liên kết / gỡ liên kết cách đăng nhập.

Gắn tài khoản khách: gọi các API đăng nhập / đăng ký kèm access token của khách (header Authorization).
- Danh tính (Google / Apple / email) chưa gắn với ai: gắn vào chính tài khoản khách đó, nâng tier lên normal_user,
  mọi dữ liệu đã học giữ nguyên (`linked: true`).
- Danh tính đã thuộc tài khoản khác: đăng nhập sang tài khoản đó (`switched_from_guest: true`); dữ liệu khách vẫn nằm
  trong SQLite trên máy, app đẩy lại qua sync/push để gộp theo luật gộp.
Mọi API trả token như device-login: {accessToken, refreshToken, user_info, is_new, linked, switched_from_guest}.
"""
import hashlib
import hmac
import secrets
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from uuid6 import uuid7

from user.models import AccountProvider, UserProfile
from user.models.UserSession import UserSession
from user.providers import SOCIAL_PROVIDERS, ProviderError, apple_exchange_code, apple_revoke, verify_identity
from user.views.auth import _device_for, _issue_tokens, _token_response
from utils.utils.permissions import AuthThrottle

User = get_user_model()

RESET_CODE_ATTEMPTS = 5
RESET_RESEND_SECONDS = 60


def _error(code, detail, http=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({'detail': detail, 'errorCode': code, **extra}, status=http)


def _provider_error(e):
    http = {
        'provider_not_configured': status.HTTP_503_SERVICE_UNAVAILABLE,
        'token_invalid': status.HTTP_401_UNAUTHORIZED,
    }.get(e.code, status.HTTP_400_BAD_REQUEST)
    return _error(e.code, str(e), http)


def _current(request):
    return request.user if request.user and request.user.is_authenticated else None


def _is_guest(user):
    profile = UserProfile.objects.filter(user=user).first()
    return profile is None or profile.tier == 'guest'


def _upgrade(user, full_name=''):
    """Khách đã có danh tính thật: lên normal_user, điền tên nếu chưa có."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    changed = []
    if profile.tier == 'guest':
        profile.tier = 'normal_user'
        changed.append('tier')
    if full_name and not profile.full_name:
        profile.full_name = full_name[:255]
        changed.append('full_name')
    if changed:
        profile.save(update_fields=changed + ['updated_at'])


def _new_user(email=''):
    user = User(username=f'user_{uuid7().hex}', email=email)
    user.set_unusable_password()
    user.save()  # signal tạo UserProfile (tier guest), _upgrade nâng lên
    return user


def _email_taken(email, exclude_user=None):
    if not email:
        return False
    rows = AccountProvider.objects.filter(provider='email', provider_user_id=email)
    if exclude_user is not None:
        rows = rows.exclude(user=exclude_user)
    return rows.exists()


def _login(request, user, is_new=False, linked=False):
    """Cấp token cho user trên thiết bị gửi lên. Đang là khách khác thì đóng phiên khách trên thiết bị này."""
    if not user.is_active:
        return _error('account_disabled', 'Account disabled', status.HTTP_403_FORBIDDEN)
    device, error = _device_for(user, request.data)
    if error:
        return error
    current = _current(request)
    switched = current is not None and current.pk != user.pk
    if switched:
        UserSession.objects.filter(user=current, device__device_id=device.device_id, is_active=True).update(is_active=False)
    response = _token_response(_issue_tokens(request, user, device), user)
    response.data.update({'is_new': is_new, 'linked': linked, 'switched_from_guest': switched})
    return response


def _normalize_email(raw):
    email = (raw or '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email if len(email) <= 254 else None


def _check_password(password, user):
    if not isinstance(password, str) or not password:
        return ['Password is required']
    try:
        validate_password(password, user)
    except ValidationError as e:
        return list(e.messages)
    return None


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def social_login(request):
    """
    Body: provider (google | apple), id_token, device_id + os / app_version / device_name / push_token (như device-login),
    full_name (Apple chỉ cho tên ở lần đầu), authorization_code (Apple: để thu hồi khi xoá tài khoản).
    Lỗi: 503 provider_not_configured (chưa có key trong env), 401 token_invalid, 409 provider_in_use
    (đang đăng nhập tài khoản thật mà danh tính thuộc người khác), 409 email_in_use (email đã đăng ký bằng mật khẩu:
    đăng nhập email rồi liên kết).
    """
    provider = request.data.get('provider')
    try:
        identity = verify_identity(provider, request.data.get('id_token'))
    except ProviderError as e:
        return _provider_error(e)

    current = _current(request)
    full_name = (request.data.get('full_name') or identity.name or '').strip()
    secret = apple_exchange_code(request.data.get('authorization_code')) if provider == 'apple' else None
    row = AccountProvider.objects.select_related('user').filter(provider=provider, provider_user_id=identity.sub).first()

    if row is not None:
        if current is not None and row.user_id != current.pk and not _is_guest(current):
            return _error('provider_in_use', 'This account is linked to another user', status.HTTP_409_CONFLICT)
        if secret:
            AccountProvider.objects.filter(pk=row.pk).update(access_token=secret)
        return _login(request, row.user)

    email = identity.email if identity.email_verified else ''
    if current is None and _email_taken(email):
        return _error('email_in_use', 'Email registered with password: log in with email, then link', status.HTTP_409_CONFLICT)

    try:
        with transaction.atomic():
            user = current or _new_user()
            if email and not user.email and not User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
                User.objects.filter(pk=user.pk).update(email=email)
                user.email = email
            if AccountProvider.objects.filter(user=user, provider=provider).exists():
                # Mỗi user chỉ 1 tài khoản của mỗi nhà cung cấp
                return _error('provider_already_linked', f'Another {provider} account is already linked', status.HTTP_409_CONFLICT)
            AccountProvider.objects.create(user=user, provider=provider, provider_user_id=identity.sub, access_token=secret, created_by=user)
            _upgrade(user, full_name)
    except IntegrityError:
        return _error('provider_in_use', 'This account is linked to another user', status.HTTP_409_CONFLICT)
    return _login(request, user, is_new=current is None, linked=current is not None)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def email_register(request):
    """
    Body: email, password, full_name?, device_id + thông tin thiết bị.
    Kèm token khách: gắn email vào tài khoản khách. Kèm token tài khoản thật (đăng nhập Google / Apple): thêm cách
    đăng nhập bằng mật khẩu. Lỗi: 400 email_invalid / password_invalid, 409 email_in_use / email_already_set.
    """
    email = _normalize_email(request.data.get('email'))
    if email is None:
        return _error('email_invalid', 'Invalid email')
    current = _current(request)
    if current is not None and AccountProvider.objects.filter(user=current, provider='email').exists():
        return _error('email_already_set', 'This account already has an email login', status.HTTP_409_CONFLICT)
    if _email_taken(email):
        return _error('email_in_use', 'Email already registered', status.HTTP_409_CONFLICT)
    errors = _check_password(request.data.get('password'), current or User(username=email, email=email))
    if errors:
        return _error('password_invalid', 'Invalid password', errors=errors)

    try:
        with transaction.atomic():
            user = current or _new_user()
            user.email = email
            user.set_password(request.data['password'])
            user.save(update_fields=['email', 'password'])
            AccountProvider.objects.create(user=user, provider='email', provider_user_id=email, created_by=user)
            _upgrade(user, (request.data.get('full_name') or '').strip())
    except IntegrityError:
        return _error('email_in_use', 'Email already registered', status.HTTP_409_CONFLICT)
    return _login(request, user, is_new=current is None, linked=current is not None)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def email_login(request):
    """Body: email, password, device_id + thông tin thiết bị. Sai email hoặc mật khẩu đều trả 401 invalid_credentials."""
    email = _normalize_email(request.data.get('email'))
    row = AccountProvider.objects.select_related('user').filter(provider='email', provider_user_id=email).first() if email else None
    password = request.data.get('password')
    if row is None or not isinstance(password, str) or not row.user.check_password(password):
        return _error('invalid_credentials', 'Wrong email or password', status.HTTP_401_UNAUTHORIZED)
    return _login(request, row.user)


def _reset_key(email):
    return f'password_reset:{email}'


def _hash_code(code):
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def _now():
    return int(time.time())


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def password_forgot(request):
    """
    Body: email. Gửi mã 6 số (sống PASSWORD_RESET_CODE_MINUTES phút). Luôn trả 200 dù email không tồn tại.
    Chưa cấu hình mail (EMAIL_HOST) và không DEBUG: 503 email_not_configured.
    """
    if not settings.EMAIL_ENABLED:
        return _error('email_not_configured', 'Email sending is not configured', status.HTTP_503_SERVICE_UNAVAILABLE)
    email = _normalize_email(request.data.get('email'))
    if email is None:
        return _error('email_invalid', 'Invalid email')
    if AccountProvider.objects.filter(provider='email', provider_user_id=email).exists():
        state = cache.get(_reset_key(email))
        if not state or state.get('sent_at', 0) + RESET_RESEND_SECONDS <= _now():
            code = f'{secrets.randbelow(10 ** 6):06d}'
            cache.set(_reset_key(email), {'hash': _hash_code(code), 'attempts': 0, 'sent_at': _now()},
                      settings.PASSWORD_RESET_CODE_MINUTES * 60)
            send_mail(
                'Flashwise: mã đặt lại mật khẩu',
                f'Mã đặt lại mật khẩu của bạn: {code}\nMã hết hạn sau {settings.PASSWORD_RESET_CODE_MINUTES} phút.',
                settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False,
            )
    return Response({'sent': True, 'expires_in': settings.PASSWORD_RESET_CODE_MINUTES * 60})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def password_reset(request):
    """Body: email, code, password. Đúng mã thì đổi mật khẩu và đóng mọi phiên (đăng nhập lại). Sai 5 lần thì mã bị huỷ."""
    email = _normalize_email(request.data.get('email'))
    state = cache.get(_reset_key(email)) if email else None
    code = str(request.data.get('code') or '')
    if not state:
        return _error('code_invalid', 'Invalid or expired code')
    if not hmac.compare_digest(state['hash'], _hash_code(code)):
        state['attempts'] += 1
        if state['attempts'] >= RESET_CODE_ATTEMPTS:
            cache.delete(_reset_key(email))
        else:
            cache.set(_reset_key(email), state, settings.PASSWORD_RESET_CODE_MINUTES * 60)
        return _error('code_invalid', 'Invalid or expired code')

    row = AccountProvider.objects.select_related('user').filter(provider='email', provider_user_id=email).first()
    if row is None:
        return _error('code_invalid', 'Invalid or expired code')
    errors = _check_password(request.data.get('password'), row.user)
    if errors:
        return _error('password_invalid', 'Invalid password', errors=errors)
    row.user.set_password(request.data['password'])
    row.user.save(update_fields=['password'])
    cache.delete(_reset_key(email))
    UserSession.objects.filter(user=row.user, is_active=True).update(is_active=False)
    return Response({'reset': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AuthThrottle])
def password_change(request):
    """Body: old_password, password. Tài khoản chưa có email: dùng email/register để thêm (400 email_required)."""
    user = request.user
    if not AccountProvider.objects.filter(user=user, provider='email').exists():
        return _error('email_required', 'Add an email login first')
    old = request.data.get('old_password')
    if user.has_usable_password() and (not isinstance(old, str) or not user.check_password(old)):
        return _error('invalid_credentials', 'Wrong password', status.HTTP_401_UNAUTHORIZED)
    errors = _check_password(request.data.get('password'), user)
    if errors:
        return _error('password_invalid', 'Invalid password', errors=errors)
    user.set_password(request.data['password'])
    user.save(update_fields=['password'])
    return Response({'changed': True})


def _provider_rows(user):
    return [
        {'provider': row.provider, 'email': row.provider_user_id if row.provider == 'email' else (user.email or None), 'linked_at': row.created_at}
        for row in AccountProvider.objects.filter(user=user).order_by('created_at')
    ]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def providers(request):
    """Các cách đăng nhập đã liên kết. configured = cách đăng nhập nào server đã có key (app ẩn nút còn lại)."""
    from user.providers import is_configured
    return Response({
        'providers': _provider_rows(request.user),
        'configured': {name: is_configured(name) for name in SOCIAL_PROVIDERS} | {'email': True},
    })


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def unlink_provider(request, provider):
    """Gỡ 1 cách đăng nhập; phải còn ít nhất 1 cách khác (400 last_login_method). Gỡ email thì bỏ mật khẩu."""
    rows = list(AccountProvider.objects.filter(user=request.user))
    row = next((r for r in rows if r.provider == provider), None)
    if row is None:
        return _error('not_linked', 'Not linked', status.HTTP_404_NOT_FOUND)
    if len(rows) == 1:
        return _error('last_login_method', 'Cannot remove the only login method')
    if provider == 'apple':
        apple_revoke(row.access_token)
    row.delete()
    if provider == 'email':
        request.user.set_unusable_password()
        request.user.save(update_fields=['password'])
    return Response({'providers': _provider_rows(request.user)})
