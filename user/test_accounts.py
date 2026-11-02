import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from ai.models import AISense, AIWord
from core.models import Collection, CollectionItem
from tracking.models import UserSenseProgress
from user.models import AccountProvider, Device, UserProfile
from user.models.UserSession import UserSession

User = get_user_model()
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
GOOGLE = ["web-client.apps.googleusercontent.com"]
PASSWORD = "Flashwise!2026"


def google_claims(sub, email="", verified=True, aud=GOOGLE[0]):
    return {"sub": sub, "email": email, "email_verified": verified, "aud": aud, "iss": "https://accounts.google.com", "name": "An"}


@override_settings(CACHES=LOCMEM_CACHE, GOOGLE_CLIENT_IDS=GOOGLE, APPLE_CLIENT_IDS=[], EMAIL_ENABLED=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.claims = mock.patch("user.providers._google_claims").start()
        self.addCleanup(mock.patch.stopall)

    def guest(self, device_id="phone-1"):
        res = self.client.post("/api/auth/device-login/", {"device_id": device_id, "os": "android"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['accessToken']}")
        return Device.objects.get(device_id=device_id).user

    def anonymous(self):
        self.client.credentials()

    def google(self, sub="g-1", device_id="phone-1", **claims):
        self.claims.return_value = google_claims(sub, **claims)
        return self.client.post("/api/auth/social-login/", {"provider": "google", "id_token": "x", "device_id": device_id}, format="json")

    def register(self, email="an@example.com", device_id="phone-1", password=PASSWORD):
        return self.client.post("/api/auth/email/register/", {"email": email, "password": password, "device_id": device_id}, format="json")

    def email_login(self, email="an@example.com", password=PASSWORD, device_id="phone-2"):
        return self.client.post("/api/auth/email/login/", {"email": email, "password": password, "device_id": device_id}, format="json")

    def use(self, res):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['accessToken']}")


class SocialLoginTests(AccountTestBase):
    def test_not_configured_and_bad_token(self):
        with override_settings(GOOGLE_CLIENT_IDS=[]):
            res = self.google()
            self.assertEqual((res.status_code, res.data["errorCode"]), (503, "provider_not_configured"))
        res = self.google(aud="someone-else")
        self.assertEqual((res.status_code, res.data["errorCode"]), (401, "token_invalid"))
        self.claims.side_effect = ValueError("Token expired")
        self.assertEqual(self.google().status_code, 401)
        res = self.client.post("/api/auth/social-login/", {"provider": "github", "id_token": "x", "device_id": "d"}, format="json")
        self.assertEqual(res.data["errorCode"], "provider_unsupported")

    def test_guest_links_google_and_keeps_data(self):
        guest = self.guest()
        res = self.google(email="an@gmail.com")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data["linked"], res.data["is_new"], res.data["switched_from_guest"]), (True, False, False))
        self.assertEqual(res.data["user_info"]["user"], guest.pk)  # vẫn tài khoản cũ, dữ liệu học giữ nguyên
        self.assertEqual(UserProfile.objects.get(user=guest).tier, "normal_user")
        self.assertEqual(User.objects.get(pk=guest.pk).email, "an@gmail.com")

        # Thiết bị giờ thuộc tài khoản thật: device-login không cấp token khách nữa
        self.anonymous()
        res = self.client.post("/api/auth/device-login/", {"device_id": "phone-1"}, format="json")
        self.assertEqual(res.data["errorCode"], "login_required")

        # Máy khác đăng nhập cùng tài khoản Google
        res = self.google(device_id="tablet")
        self.assertEqual((res.status_code, res.data["user_info"]["user"], res.data["is_new"]), (200, guest.pk, False))

    def test_other_guest_switches_to_existing_account(self):
        owner = self.guest("phone-1")
        self.google()
        other = self.guest("phone-2")
        res = self.google(device_id="phone-2")
        self.assertEqual((res.data["user_info"]["user"], res.data["switched_from_guest"]), (owner.pk, True))
        self.assertFalse(UserSession.objects.filter(user=other, is_active=True).exists())  # phiên khách trên máy này bị đóng

    def test_real_account_cannot_take_others_identity(self):
        self.guest("phone-1")
        self.google(sub="g-1")
        self.guest("phone-2")
        self.use(self.google(sub="g-2", device_id="phone-2"))
        res = self.google(sub="g-1", device_id="phone-2")
        self.assertEqual((res.status_code, res.data["errorCode"]), (409, "provider_in_use"))

    def test_new_account_and_verified_email_conflict(self):
        res = self.google(sub="g-9", email="new@gmail.com")
        self.assertEqual((res.status_code, res.data["is_new"]), (200, True))
        self.assertEqual(res.data["user_info"]["tier"], "normal_user")

        self.anonymous()
        self.register(email="taken@example.com", device_id="d1")
        self.anonymous()
        res = self.google(sub="g-10", email="taken@example.com")
        self.assertEqual((res.status_code, res.data["errorCode"]), (409, "email_in_use"))


class EmailLoginTests(AccountTestBase):
    def test_register_login_and_errors(self):
        guest = self.guest()
        res = self.register(email=" An@Example.com ")
        self.assertEqual((res.status_code, res.data["linked"], res.data["user_info"]["user"]), (200, True, guest.pk))
        self.assertEqual(AccountProvider.objects.get(user=guest).provider_user_id, "an@example.com")
        self.assertEqual(self.register().data["errorCode"], "email_already_set")

        self.anonymous()
        self.assertEqual(self.register().data["errorCode"], "email_in_use")
        self.assertEqual(self.register(email="b@example.com", password="123").data["errorCode"], "password_invalid")
        self.assertEqual(self.register(email="not-an-email").data["errorCode"], "email_invalid")

        res = self.email_login()
        self.assertEqual((res.status_code, res.data["user_info"]["user"]), (200, guest.pk))
        self.assertEqual(self.email_login(password="wrong").data["errorCode"], "invalid_credentials")
        self.assertEqual(self.email_login(email="nobody@example.com").status_code, 401)

    def test_password_reset_and_change(self):
        self.register()
        self.anonymous()
        self.use(self.email_login())
        self.anonymous()
        self.assertEqual(self.client.post("/api/auth/password/forgot/", {"email": "nobody@example.com"}, format="json").status_code, 200)
        self.assertEqual(len(mail.outbox), 0)  # không lộ email nào có tài khoản
        self.client.post("/api/auth/password/forgot/", {"email": "an@example.com"}, format="json")
        code = mail.outbox[0].body.split(": ")[1][:6]

        reset = lambda c, p="NewPass!2026": self.client.post("/api/auth/password/reset/", {"email": "an@example.com", "code": c, "password": p}, format="json")
        self.assertEqual(reset("000000" if code != "000000" else "111111").data["errorCode"], "code_invalid")
        self.assertEqual(reset(code).data, {"reset": True})
        self.assertFalse(UserSession.objects.filter(is_active=True, user__email="an@example.com").exists())  # đăng nhập lại
        self.assertEqual(self.email_login(password="NewPass!2026").status_code, 200)
        self.assertEqual(reset(code).data["errorCode"], "code_invalid")  # mã chỉ dùng 1 lần

        self.use(self.email_login(password="NewPass!2026"))
        res = self.client.post("/api/auth/password/change/", {"old_password": "bad", "password": PASSWORD}, format="json")
        self.assertEqual(res.status_code, 401)
        res = self.client.post("/api/auth/password/change/", {"old_password": "NewPass!2026", "password": PASSWORD}, format="json")
        self.assertEqual(res.data, {"changed": True})

    def test_too_many_wrong_codes_cancel_reset(self):
        self.register()
        self.anonymous()
        self.client.post("/api/auth/password/forgot/", {"email": "an@example.com"}, format="json")
        code = mail.outbox[0].body.split(": ")[1][:6]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.client.post("/api/auth/password/reset/", {"email": "an@example.com", "code": wrong, "password": "NewPass!2026"}, format="json")
        res = self.client.post("/api/auth/password/reset/", {"email": "an@example.com", "code": code, "password": "NewPass!2026"}, format="json")
        self.assertEqual(res.data["errorCode"], "code_invalid")

    @override_settings(EMAIL_ENABLED=False)
    def test_forgot_without_mail_config(self):
        res = self.client.post("/api/auth/password/forgot/", {"email": "an@example.com"}, format="json")
        self.assertEqual((res.status_code, res.data["errorCode"]), (503, "email_not_configured"))

    def test_providers_and_unlink(self):
        self.guest()
        self.google()
        res = self.client.get("/api/auth/providers/")
        self.assertEqual([p["provider"] for p in res.data["providers"]], ["google"])
        self.assertEqual(res.data["configured"], {"google": True, "apple": False, "email": True})
        self.assertEqual(self.client.delete("/api/auth/providers/google/").data["errorCode"], "last_login_method")
        self.register()
        res = self.client.delete("/api/auth/providers/email/")
        self.assertEqual([p["provider"] for p in res.data["providers"]], ["google"])
        self.assertEqual(self.email_login().status_code, 401)


class AccountDataTests(AccountTestBase):
    def test_banned_user_can_still_log_in(self):
        user = self.guest()
        UserProfile.objects.filter(user=user).update(is_active=False, ban_reason="x")
        self.anonymous()
        self.assertEqual(self.client.post("/api/auth/device-login/", {"device_id": "phone-1"}, format="json").status_code, 200)

    def test_device_push_token_and_logout(self):
        user = self.guest()
        res = self.client.post("/api/me/device/", {"device_id": "phone-1", "push_token": "ExponentPushToken[1]", "app_version": "3"}, format="json")
        self.assertEqual(res.data["push_token"], "ExponentPushToken[1]")
        self.assertEqual(self.client.post("/api/me/device/", {"device_id": "other"}, format="json").status_code, 404)
        self.client.post("/api/auth/logout/", {}, format="json")
        self.assertIsNone(Device.objects.get(user=user).push_token)

    def test_export(self):
        user = self.guest()
        self.client.post("/api/me/device/", {"device_id": "phone-1", "push_token": "secret-token"}, format="json")
        UserSenseProgress.objects.create(user=user, original_sense_id=AISense.objects.create(
            word=AIWord.objects.create(value="cat", language_code="en"), word_value="cat", language_code="en").id)
        res = self.client.get("/api/me/export/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("attachment", res["Content-Disposition"])
        data = json.loads(res.content)
        self.assertEqual(len(data["user_sense_progress"]), 1)
        self.assertNotIn("secret-token", res.content.decode())

    def test_delete_account_keeps_public_content_only(self):
        user = self.guest()
        word = AIWord.objects.create(value="cat", language_code="en")
        public_sense = AISense.objects.create(word=word, word_value="cat", language_code="en", is_official=False, is_frozen=True, created_by=user)
        private_sense = AISense.objects.create(word=word, word_value="cat", language_code="en", is_official=False, is_frozen=False, created_by=user)
        public = Collection.objects.create(name="Public", language_code="en", released=True, version=1, created_by=user)
        CollectionItem.objects.create(collection=public, sense=public_sense, original=public_sense, released=True, version=1, created_by=user)
        private = Collection.objects.create(name="Private", language_code="en", created_by=user)
        CollectionItem.objects.create(collection=private, sense=private_sense, original=private_sense, created_by=user)
        UserSenseProgress.objects.create(user=user, original_sense_id=public_sense.id)

        self.assertEqual(self.client.delete("/api/me/", {}, format="json").data["errorCode"], "confirm_required")
        res = self.client.delete("/api/me/", {"confirm": True}, format="json")
        self.assertEqual((res.status_code, res.data["deleted_collections"], res.data["kept_public_collections"]), (200, 1, 1))
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(UserSenseProgress.objects.exists())
        self.assertFalse(AISense.objects.filter(pk=private_sense.pk).exists())
        self.assertIsNone(Collection.objects.get(pk=public.pk).created_by)  # bộ công khai còn, bỏ tên tác giả
        self.assertTrue(AISense.objects.filter(pk=public_sense.pk).exists())
        self.assertEqual(self.client.get("/api/me/").status_code, 401)  # token cũ hết dùng được
