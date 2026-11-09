import sys
import unittest
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import qq_avatar


def png(size):
    with BytesIO() as file:
        Image.new("RGB", size, "#7ae7c2").save(file, format="PNG")
        return file.getvalue()


class QQAvatarTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(qq_avatar, "_cache", OrderedDict()))
        self.connect = self.enterContext(patch.object(qq_avatar, "HTTPSConnection"))
        self.connection = self.connect.return_value
        self.response = self.connection.getresponse.return_value.__enter__.return_value
        self.response.status = 200

    def body(self, data):
        self.response.read1.side_effect = BytesIO(data).read1

    def test_downloads_fixed_https_endpoint_crops_avatar_and_caches_by_id(self):
        self.body(png((120, 100)))
        avatar = qq_avatar.get_avatar("1000000042")
        self.assertEqual((avatar.mode, avatar.size), ("RGB", (144, 144)))
        self.assertIs(qq_avatar.get_avatar("1000000042"), avatar)
        self.connect.assert_called_once_with(qq_avatar.AVATAR_HOST, timeout=qq_avatar.AVATAR_TIMEOUT)
        self.connection.request.assert_called_once_with("GET", "/g?b=qq&nk=1000000042&s=100")
        self.connection.close.assert_called_once()
        self.body(png((40, 60)))
        self.assertIsNot(qq_avatar.get_avatar("1000000043"), avatar)
        self.assertEqual(self.connect.call_count, 2)

    def test_timeout_uses_short_failure_cache_then_retries(self):
        with patch.object(qq_avatar, "monotonic", return_value=10) as clock:
            self.connection.getresponse.side_effect = TimeoutError("private network details")
            self.assertIsNone(qq_avatar.get_avatar("1000000042"))
            self.assertIsNone(qq_avatar.get_avatar("1000000042"))
            self.assertEqual(self.connect.call_count, 1)
            clock.return_value += qq_avatar.FAILURE_TTL + 1
            self.connection.getresponse.side_effect = None
            self.body(png((60, 60)))
            self.assertIsNotNone(qq_avatar.get_avatar("1000000042"))
            self.assertEqual(self.connect.call_count, 2)
            clock.return_value += qq_avatar.AVATAR_TTL + 1
            self.body(png((60, 60)))
            self.assertIsNotNone(qq_avatar.get_avatar("1000000042"))
            self.assertEqual(self.connect.call_count, 3)

    def test_rejects_oversized_encoded_or_decoded_images_and_malformed_data(self):
        for offset, payload in enumerate((
            png((100, 100)) + bytes(qq_avatar.MAX_AVATAR_BYTES),
            png((qq_avatar.MAX_AVATAR_SIDE + 1, 4)),
            b"invalid image",
        )):
            with self.subTest(offset=offset):
                self.body(payload)
                self.assertIsNone(qq_avatar.get_avatar(str(1000000042 + offset)))

    def test_rejects_non_digit_ids_and_never_follows_redirects(self):
        self.assertIsNone(qq_avatar.get_avatar("1000000042&redirect=example.invalid"))
        self.connect.assert_not_called()
        self.response.status = 302
        self.assertIsNone(qq_avatar.get_avatar("1000000042"))
        self.connect.assert_called_once()
        self.response.read1.assert_not_called()
