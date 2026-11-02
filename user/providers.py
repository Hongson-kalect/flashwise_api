"""
Xác minh đăng nhập bằng tài khoản bên thứ 3. App tự làm bước đăng nhập với Google / Apple (SDK trên máy) rồi gửi
id token lên; server chỉ kiểm tra chữ ký + aud + hạn, không giữ access token của họ.
Key nằm trong env (settings.GOOGLE_CLIENT_IDS, APPLE_*): thiếu thì ProviderError('provider_not_configured').
"""
import time
from dataclasses import dataclass

import jwt
import requests
from django.conf import settings

GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
APPLE_ISSUER = 'https://appleid.apple.com'
APPLE_KEYS_URL = 'https://appleid.apple.com/auth/keys'
APPLE_TOKEN_URL = 'https://appleid.apple.com/auth/token'
APPLE_REVOKE_URL = 'https://appleid.apple.com/auth/revoke'
SOCIAL_PROVIDERS = ('google', 'apple')

_apple_keys = None


class ProviderError(Exception):
    """code: provider_not_configured (503) | provider_unsupported | token_invalid (400 / 401)."""

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code


@dataclass
class Identity:
    provider: str
    sub: str  # id của user phía nhà cung cấp, không đổi
    email: str = ''
    email_verified: bool = False
    name: str = ''


def is_configured(provider):
    return bool({'google': settings.GOOGLE_CLIENT_IDS, 'apple': settings.APPLE_CLIENT_IDS}.get(provider))


def apple_can_revoke():
    return bool(settings.APPLE_TEAM_ID and settings.APPLE_KEY_ID and settings.APPLE_PRIVATE_KEY and settings.APPLE_CLIENT_IDS)


def _google_claims(token):
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    # audience=None: tự kiểm tra aud bên dưới vì có nhiều client id (web / iOS / Android)
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience=None)


def _apple_claims(token):
    global _apple_keys
    if _apple_keys is None:
        _apple_keys = jwt.PyJWKClient(APPLE_KEYS_URL)  # tự cache khoá công khai của Apple
    key = _apple_keys.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=['RS256'], audience=settings.APPLE_CLIENT_IDS, issuer=APPLE_ISSUER)


def _truthy(value):
    return value is True or str(value).lower() == 'true'


def verify_identity(provider, token):
    """Kiểm tra id token, trả Identity. Lỗi: ProviderError."""
    if provider not in SOCIAL_PROVIDERS:
        raise ProviderError('provider_unsupported')
    if not is_configured(provider):
        raise ProviderError('provider_not_configured')
    if not token or not isinstance(token, str):
        raise ProviderError('token_invalid', 'Missing id_token')
    try:
        if provider == 'google':
            claims = _google_claims(token)
            if claims.get('aud') not in settings.GOOGLE_CLIENT_IDS or claims.get('iss') not in GOOGLE_ISSUERS:
                raise ProviderError('token_invalid', 'Wrong audience')
        else:
            claims = _apple_claims(token)
    except ProviderError:
        raise
    except Exception as e:  # chữ ký sai, hết hạn, không tải được khoá...
        raise ProviderError('token_invalid', str(e)[:200])
    if not claims.get('sub'):
        raise ProviderError('token_invalid', 'Missing sub')
    return Identity(
        provider=provider, sub=str(claims['sub']), email=(claims.get('email') or '').strip().lower(),
        email_verified=_truthy(claims.get('email_verified')), name=claims.get('name') or '',
    )


def _apple_client_secret():
    """client_secret của Apple = JWT ES256 ký bằng key .p8, sống tối đa 6 tháng (dùng 5 phút là đủ)."""
    now = int(time.time())
    return jwt.encode(
        {'iss': settings.APPLE_TEAM_ID, 'iat': now, 'exp': now + 300, 'aud': APPLE_ISSUER, 'sub': settings.APPLE_CLIENT_IDS[0]},
        settings.APPLE_PRIVATE_KEY, algorithm='ES256', headers={'kid': settings.APPLE_KEY_ID},
    )


def apple_exchange_code(code):
    """Đổi authorization code (lần đăng nhập Apple) lấy refresh token để sau này thu hồi. Không đủ key / lỗi: None."""
    if not code or not apple_can_revoke():
        return None
    try:
        res = requests.post(APPLE_TOKEN_URL, data={
            'client_id': settings.APPLE_CLIENT_IDS[0], 'client_secret': _apple_client_secret(),
            'code': code, 'grant_type': 'authorization_code',
        }, timeout=10)
        return res.json().get('refresh_token') if res.ok else None
    except Exception:
        return None


def apple_revoke(refresh_token):
    """Thu hồi quyền của app với tài khoản Apple (bắt buộc khi xoá tài khoản). Trả True nếu Apple nhận."""
    if not refresh_token or not apple_can_revoke():
        return False
    try:
        res = requests.post(APPLE_REVOKE_URL, data={
            'client_id': settings.APPLE_CLIENT_IDS[0], 'client_secret': _apple_client_secret(),
            'token': refresh_token, 'token_type_hint': 'refresh_token',
        }, timeout=10)
        return res.ok
    except Exception:
        return False
