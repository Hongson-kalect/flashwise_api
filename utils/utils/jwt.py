
import datetime
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes, authentication_classes,action
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import InvalidToken,ExpiredTokenError
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User

User = get_user_model()

def is_access_token_valid(token):
    auth = JWTAuthentication()
    try:
        validated_token = auth.get_validated_token(token)
        user = auth.get_user(validated_token)
        return True
    except ExpiredTokenError:
        return "expired"
    except InvalidToken:
        return "invalid"

def is_refresh_token_valid(token):
    try:
        refresh = RefreshToken(token)
        user_id = refresh["user_id"]
        # nếu tạo object không lỗi, tức là token hợp lệ
        user = User.objects.get(id=user_id)
        return user
    
    except TokenError as e:
        print("error", str(e))
        if "expired" in str(e).lower():
            return "expired"
        return "invalid"
def generate_tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        "accessToken": str(refresh.access_token),
        "refreshToken": str(refresh),
    }

def get_user_from_token_header(request):
    auth = JWTAuthentication()
    header = auth.get_header(request)
    if not header:
        return None

    raw_token = auth.get_raw_token(header)
    if not raw_token:
        return None

    try:
        validated_token = auth.get_validated_token(raw_token)
        return auth.get_user(validated_token)
    except InvalidToken:
        return None

def create_new_token(user, device):
    tokens = generate_tokens_for_user(user)
    return tokens

@api_view(["POST"])
@permission_classes([])  # Bỏ hết kiểm tra xác thực
@authentication_classes([])  # Không yêu cầu token
def refresh_token(request):
    refresh_token = request.data.get('refresh_token')
    is_valid = is_refresh_token_valid(refresh_token)

    if is_valid == "expired" or is_valid == "invalid":
        return Response({
            "detail": "Token invalid. Please re-authenticate.",
            "errorCode": "token_invalid"
        }, status=status.HTTP_401_UNAUTHORIZED)
    
    tokens = generate_tokens_for_user(is_valid)
    return Response(tokens)