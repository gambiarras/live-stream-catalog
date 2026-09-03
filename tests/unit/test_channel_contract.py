import unittest

from live_stream_catalog.models import Channel
from live_stream_catalog.sources.channel_contract import as_bool, is_explicit_adult


class ChannelContractTest(unittest.TestCase):
    def test_parses_legacy_channel_with_backward_compatible_defaults(self):
        channel = Channel.from_dict(
            {
                "id": "legacy.channel",
                "name": "Legacy",
                "url": "https://example.test/source",
                "group": "general",
                "source_type": "youtube",
            }
        )

        self.assertEqual(channel.provider_id, "youtube")
        self.assertEqual(channel.logical_channel_id, "legacy.channel")
        self.assertTrue(channel.publishable_static)
        self.assertFalse(channel.removed)

    def test_parses_extended_transport_and_sanitizes_secret_values(self):
        channel = Channel.from_dict(
            {
                "id": "extended.channel",
                "name": "Extended",
                "source_url": "https://example.test/source",
                "stream_url": "https://example.test/live.mpd",
                "group": "TV Aberta",
                "source_type": "json_catalog",
                "provider_id": "json_catalog_1",
                "logical_channel_id": "globo.rj",
                "variant_id": "globo.rj.4k",
                "variant_label": "4K HEVC",
                "resolution": "2160p",
                "codec": "hevc",
                "bitrate": 12000000,
                "protocol": "dash",
                "headers": {
                    "Referer": "https://example.test/",
                    "Authorization": "Bearer secret",
                    "Cookie": "session=secret",
                },
                "secret_refs": {"request_headers": "LIVE_CHANNEL_HEADERS"},
                "drm": {
                    "type": "widevine",
                    "license_url": "https://license.example.test/widevine",
                    "token": "private-license-token",
                    "license_headers": {
                        "Origin": "https://example.test",
                        "Authorization": "Bearer license-secret",
                    },
                },
                "publishable_static": False,
            }
        )

        payload = channel.to_dict()

        self.assertEqual(channel.variant_label, "4K HEVC")
        self.assertEqual(payload["request_headers"], {"Referer": "https://example.test/"})
        self.assertEqual(
            payload["drm"]["license_headers"],
            {"Origin": "https://example.test"},
        )
        self.assertNotIn("bearer secret", str(payload).casefold())
        self.assertNotIn("session=secret", str(payload).casefold())
        self.assertNotIn("private-license-token", str(payload))

    def test_omits_sensitive_temporary_stream_url_from_public_payload(self):
        channel = Channel.from_dict(
            {
                "id": "temporary.channel",
                "name": "Temporary",
                "source_url": "config://provider/channel",
                "stream_url": "https://media.example.test/live.m3u8?token=private-value",
                "group": "general",
                "source_type": "stremio_addon",
                "publishable_static": False,
            }
        )

        self.assertIsNone(channel.to_dict()["stream_url"])

    def test_omits_sensitive_stream_url_with_empty_token(self):
        channel = Channel.from_dict(
            {
                "id": "temporary.channel",
                "name": "Temporary",
                "source_url": "config://provider/channel",
                "stream_url": "https://media.example.test/live.m3u8?token=",
                "group": "general",
                "source_type": "stremio_addon",
                "publishable_static": False,
            }
        )

        self.assertIsNone(channel.to_dict()["stream_url"])

    def test_omits_cloud_signed_stream_url_from_public_payload(self):
        channel = Channel.from_dict(
            {
                "id": "temporary.channel",
                "name": "Temporary",
                "source_url": "config://provider/channel",
                "stream_url": "https://media.example.test/live.m3u8?X-Amz-Signature=private",
                "group": "general",
                "source_type": "json_catalog",
                "publishable_static": False,
            }
        )

        self.assertIsNone(channel.to_dict()["stream_url"])

    def test_omits_dynamic_ssai_stream_url_without_exposing_macros(self):
        channel = Channel.from_dict(
            {
                "id": "dynamic.channel",
                "name": "Dynamic",
                "source_url": "config://provider/channel",
                "stream_url": "https://media.example.test/live.m3u8?w=[WIDTH]&ip=[IP]",
                "group": "general",
                "source_type": "json_catalog",
                "delivery_mode": "ssai",
                "requires_dynamic_resolution": True,
                "publishable_static": False,
            }
        )

        self.assertIsNone(channel.to_dict()["stream_url"])

    def test_redacts_detailed_resolution_errors_from_public_payload(self):
        channel = Channel.from_dict(
            {
                "id": "failed.channel",
                "name": "Failed",
                "source_url": "https://provider.example/channel",
                "group": "general",
                "source_type": "youtube",
                "status": "error",
                "error": "403 for https://provider.example/private?token=value",
            }
        )

        self.assertEqual(channel.to_dict()["error"], "resolution_error")

    def test_adult_filter_requires_explicit_metadata(self):
        self.assertFalse(is_explicit_adult({"name": "Adult Swim", "genres": ["Animation"]}))
        self.assertTrue(is_explicit_adult({"name": "Movie Channel", "content_rating": "+18"}))
        self.assertTrue(is_explicit_adult({"name": "Movie Channel", "category": "Conteúdo +18"}))
        self.assertTrue(is_explicit_adult({"name": "Movie Channel", "is_nsfw": True}))

    def test_parses_boolean_strings_without_treating_false_as_true(self):
        self.assertFalse(as_bool("false"))
        self.assertTrue(as_bool("true"))


if __name__ == "__main__":
    unittest.main()
