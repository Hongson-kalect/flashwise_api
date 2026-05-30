from copy import deepcopy
from hmac import new
import token
from tracemalloc import start
from unittest.mock import patch
from urllib import request
from django.http import JsonResponse
from operator import is_
from django.db.models import Q, F, Count, Avg
from django.db.models.functions import TruncDate 
import datetime
from pyexpat import model
from django.forms import model_to_dict
from django.utils import timezone
import re
from rest_framework import viewsets, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import api_view, permission_classes, authentication_classes,action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth import get_user_model

from user.models import Device, UserProfile
from user.models.UserSession import UserSession
from user.serializers.Profile import ProfileSerializer
from .jwt import generate_tokens_for_user, is_refresh_token_valid

from django.db import models

User = get_user_model()

def device_login(request):
    device_id = request.data.get("device_id")
    access_token = request.data.get("access_token")
    refresh_token = request.data.get("refresh_token")
    date =request.user_date

    if not device_id:
        return Response({"detail": "Missing device_id"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        device = Device.objects.get(device_id=device_id)
        user = device.user
        user_info = UserProfile.objects.get(user=user)

        if not refresh_token:
            if user_info.is_guest:
                update_last_seen(device, user)
                # add_login_history(device, user, date)
                tokens = create_new_token(user, device)
                return token_response(tokens, user_info)
            
            # add_login_history(device, user, date)
            return Response({
                "user_info": ProfileSerializer(user_info).data,
                "detail": "Missing refresh token",
                "errorCode": "token_missing"
            }, status=status.HTTP_401_UNAUTHORIZED)

        token_status = is_refresh_token_valid(refresh_token)

        if token_status == "expired":
            return Response({
                "user_info": ProfileSerializer(user_info).data,
                "detail": "Token expired. Please re-authenticate.",
                "errorCode": "token_expired"
            }, status=status.HTTP_401_UNAUTHORIZED)

        if token_status == "invalid":
            return Response({
                "user_info": ProfileSerializer(user_info).data,
                "detail": "Token invalid. Please re-authenticate.",
                "errorCode": "token_invalid"
            }, status=status.HTTP_401_UNAUTHORIZED)

        token_obj = UserSession.objects.get(token_value=refresh_token, token_type="refresh")

        if not token_obj.is_active or token_obj.is_banned:
            UserSession.objects.filter(user=user, device=device).update(is_banned=True)
            # UserSession.objects.filter(user=user).update(is_banned=True)
            return Response({
                "user_info": ProfileSerializer(user_info).data,
                "detail": "UserSession is banned or reused. Please re-authenticate.",
                "errorCode": "token_banned"
            }, status=status.HTTP_401_UNAUTHORIZED)

        # Valid refresh token → issue new one
        UserSession.objects.filter(user=user, device=device).update(is_active=False)
        tokens = create_new_token(user, device)
        update_last_seen(device, user)
        # add_login_history(device, user, date)
        return token_response(tokens, user_info)

    except Device.DoesNotExist:
        # First time: create guest user and assign device
        guest_user = User.objects.create(username=f"guest_{device_id}")
        tokens = generate_tokens_for_user(guest_user)
        device = Device.objects.create(user=guest_user, device_id=device_id, last_seen=timezone.now())
        UserSession.objects.create(user=guest_user, device=device, token=tokens['refreshToken'], expires_at=timezone.now() + datetime.timedelta(days=7))
        Device.objects.create(user=guest_user, last_seen_at=timezone.now())
        user_info = UserProfile.objects.get(user=guest_user)
        # add_login_history(device, user, date)
        return token_response(tokens, user_info)

    except UserSession.DoesNotExist:
        # Token mất nhưng user là guest thì cấp lại token
        if user_info.is_guest:
            tokens = create_new_token(user, device)
            update_last_seen(device, user)
            # add_login_history(device, user, date)
            return token_response(tokens, user_info)

        return Response({
            "user_info": ProfileSerializer(user_info).data,
            "detail": "Token not found.",
            "errorCode": "token_missing"
        }, status=status.HTTP_401_UNAUTHORIZED)

def create_new_token(user, device):
    tokens = generate_tokens_for_user(user)
    UserSession.objects.create(
        user=user,
        device=device,
        token_type='refresh',
        token_value=tokens['refreshToken'],
        expires_at=timezone.now() + datetime.timedelta(days=7)
    )
    return tokens

def update_last_seen(device, user):
    Device.objects.update_or_create(
        user=user,
        defaults={'last_seen_at': timezone.now()}
    )

def token_response(tokens, user_info):
    return Response({
        **tokens,
        "user_info": ProfileSerializer(user_info).data
    })

# def add_login_history(device, user, date):
#     Login.objects.create(device=device, user=user)