"""The admin Profile page's photo viewer shows the WHOLE uploaded photo (not the round crop) with a "View Full Image" link, for every admin
account: owner, Manager and Staff. The behaviour itself (uncropped shape, link, Esc/X/click-outside, phone width) is checked in a real browser;
these tests keep the page from losing the viewer for any role."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile

User = get_user_model()


class ProfilePhotoViewerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.accounts = {}
        for name, role, superuser in (("pv_owner", UserProfile.Role.OWNER, True), ("pv_manager", UserProfile.Role.MANAGER, False),
                                      ("pv_staff", UserProfile.Role.STAFF, False)):
            user = User.objects.create_user(name, f"{name}@example.test", "Viewer-Pass-2026!", is_staff=True, is_superuser=superuser)
            UserProfile.objects.create(user=user, role=role, profile_picture_url="/media/profile_pictures/example.png")
            cls.accounts[role] = user

    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("arabela_admin:page", kwargs={"page": "profile"}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def assert_full_photo_viewer(self, html):
        # the shared frame that hugs the photo, and the viewer that uses it
        self.assertIn(".admin-photo-frame", html)
        self.assertIn('class="admin-photo-frame', html)
        # the whole photo at its own shape, never the 220px round crop the viewer used to show
        self.assertIn("max-height: 60vh; max-width: 100%; width: auto; height: auto;", html)
        self.assertNotIn('style="width: 220px; height: 220px;"', html)
        # the real file in a new tab
        self.assertIn("View Full Image", html)
        self.assertIn(':href="avatarUrl"', html)
        self.assertIn('target="_blank"', html)
        # a photo that cannot be loaded says so, and Esc closes the viewer
        self.assertIn("This photo could not be loaded.", html)
        self.assertIn('@keydown.escape.window="isViewModal = false"', html)

    def test_owner_sees_the_full_photo_viewer(self):
        self.assert_full_photo_viewer(self.page(self.accounts[UserProfile.Role.OWNER]))

    def test_manager_sees_the_full_photo_viewer(self):
        self.assert_full_photo_viewer(self.page(self.accounts[UserProfile.Role.MANAGER]))

    def test_staff_sees_the_full_photo_viewer(self):
        self.assert_full_photo_viewer(self.page(self.accounts[UserProfile.Role.STAFF]))

    def test_the_round_picture_on_the_page_is_still_the_cropped_one(self):
        html = self.page(self.accounts[UserProfile.Role.STAFF])
        self.assertIn('class="admin-avatar-clickable h-full w-full object-cover"', html)
        self.assertIn('@click="avatarViewFailed = false; isViewModal = true"', html)
