from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from core.models import Collection, UserCollection
from user.models import Device, UserProfile
from user.models.UserSession import UserSession

User = get_user_model()

# Test không phụ thuộc Redis: throttle dùng cache trong bộ nhớ
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM_CACHE)
class DeviceLoginTests(APITestCase):
    def setUp(self):
        cache.clear()

    def login(self, device_id="device-1"):
        return self.client.post("/api/auth/device-login/", {"device_id": device_id, "os": "android"}, format="json")

    def test_missing_device_id(self):
        res = self.client.post("/api/auth/device-login/", {}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_new_device_creates_guest(self):
        res = self.login()
        self.assertEqual(res.status_code, 200)
        self.assertIn("accessToken", res.data)
        self.assertIn("refreshToken", res.data)
        self.assertEqual(res.data["user_info"]["tier"], "guest")

        device = Device.objects.get(device_id="device-1")
        self.assertTrue(UserProfile.objects.filter(user=device.user).exists())
        self.assertFalse(device.user.has_usable_password())
        self.assertEqual(UserSession.objects.filter(user=device.user, is_active=True).count(), 1)

    def test_same_device_reuses_guest_and_closes_old_session(self):
        first = self.login()
        second = self.login()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(User.objects.filter(username__startswith="guest_").count(), 1)
        self.assertNotEqual(first.data["refreshToken"], second.data["refreshToken"])

        user = Device.objects.get(device_id="device-1").user
        self.assertEqual(UserSession.objects.filter(user=user, is_active=True).count(), 1)

    def test_registered_account_needs_real_login(self):
        self.login()
        user = Device.objects.get(device_id="device-1").user
        UserProfile.objects.filter(user=user).update(tier="normal_user")

        res = self.login()
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["errorCode"], "login_required")

    def test_access_token_authenticates(self):
        token = self.login().data["accessToken"]
        res = self.client.get("/api/user-collections/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(res.status_code, 200)


@override_settings(CACHES=LOCMEM_CACHE)
class RefreshTokenTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.tokens = self.client.post("/api/auth/device-login/", {"device_id": "device-2"}, format="json").data

    def test_refresh_rotates_token(self):
        # App gọi `${baseURL}/refresh-token` với body {refreshToken}
        res = self.client.post("/api/refresh-token", {"refreshToken": self.tokens["refreshToken"]}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertNotEqual(res.data["refreshToken"], self.tokens["refreshToken"])

    def test_reused_refresh_token_is_rejected_and_revokes_device(self):
        new = self.client.post("/api/refresh-token/", {"refreshToken": self.tokens["refreshToken"]}, format="json").data

        reuse = self.client.post("/api/refresh-token/", {"refreshToken": self.tokens["refreshToken"]}, format="json")
        self.assertEqual(reuse.status_code, 401)
        self.assertEqual(reuse.data["errorCode"], "token_revoked")

        # Token mới cũng bị đóng vì nghi lộ token
        after = self.client.post("/api/refresh-token/", {"refreshToken": new["refreshToken"]}, format="json")
        self.assertEqual(after.status_code, 401)

    def test_invalid_refresh_token(self):
        res = self.client.post("/api/refresh-token/", {"refreshToken": "not-a-token"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["errorCode"], "token_invalid")

    def test_logout_closes_session(self):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.tokens['accessToken']}"}
        res = self.client.post("/api/auth/logout/", {"refreshToken": self.tokens["refreshToken"]}, format="json", **auth)
        self.assertEqual(res.status_code, 204)

        res = self.client.post("/api/refresh-token/", {"refreshToken": self.tokens["refreshToken"]}, format="json")
        self.assertEqual(res.status_code, 401)


@override_settings(CACHES=LOCMEM_CACHE)
class PermissionTests(APITestCase):
    def setUp(self):
        cache.clear()

    def guest(self, device_id):
        token = self.client.post("/api/auth/device-login/", {"device_id": device_id}, format="json").data["accessToken"]
        user = Device.objects.get(device_id=device_id).user
        return user, {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_user_collections_require_login(self):
        self.assertEqual(self.client.get("/api/user-collections/").status_code, 401)

    def test_user_collections_only_show_own(self):
        alice, alice_auth = self.guest("alice")
        bob, _ = self.guest("bob")
        collection = Collection.objects.create(name="Bộ từ", language_code="en")
        UserCollection.objects.create(user=bob, collection=collection)

        res = self.client.get("/api/user-collections/", **alice_auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["count"], 0)

    def test_download_collection(self):
        _, auth = self.guest("carol")
        collection = Collection.objects.create(name="IELTS", language_code="en", is_official=True)
        private = Collection.objects.create(name="Của người khác", language_code="en")

        first = self.client.post(f"/api/user-collections/{collection.id}/download/", **auth)
        again = self.client.post(f"/api/user-collections/{collection.id}/download/", **auth)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        # Bộ cá nhân của người khác không tải được
        self.assertEqual(self.client.post(f"/api/user-collections/{private.id}/download/", **auth).status_code, 404)

    def test_other_user_cannot_delete_collection(self):
        owner, _ = self.guest("owner")
        _, other_auth = self.guest("other")
        collection = Collection.objects.create(name="Của owner", language_code="en", created_by=owner)

        res = self.client.delete(f"/api/collections/{collection.id}/", **other_auth)
        self.assertEqual(res.status_code, 404)  # bộ cá nhân của người khác không hiện ra (bộ công khai thì 403, xem core/tests.py)

    def test_language_write_is_admin_only(self):
        _, auth = self.guest("dave")
        res = self.client.post("/api/languages/", {"code": "xx", "name": "Test"}, format="json", **auth)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.client.get("/api/languages/").status_code, 200)

    def test_clear_words_is_admin_only(self):
        _, auth = self.guest("eve")
        self.assertEqual(self.client.delete("/api/ai-words/clear/", **auth).status_code, 403)
        self.assertEqual(self.client.delete("/api/ai-words/clear/").status_code, 401)

    def test_get_word_validates_params(self):
        res = self.client.get("/api/ai-words/get-word/")
        self.assertEqual(res.status_code, 400)
        res = self.client.get("/api/ai-words/get-word/", {"value": "x" * 101, "lang": "en", "user_lang": "vi"})
        self.assertEqual(res.status_code, 400)

    def test_create_sense_requires_login(self):
        res = self.client.post("/api/ai-senses/create-sense/", {}, format="json")
        self.assertEqual(res.status_code, 401)

    @override_settings(DEBUG=False)
    def test_dev_token_hidden_when_not_debug(self):
        # Route chỉ được đăng ký lúc import urls khi DEBUG=True, view tự chặn thêm khi DEBUG=False
        res = self.client.get("/api/auth/dev-token/")
        self.assertIn(res.status_code, (404,))
