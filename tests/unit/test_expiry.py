from tests import fixture_config  # noqa: F401

import unittest
from datetime import timedelta

from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import (
    extract_expiry_from_stream_url,
    needs_refresh,
    utc_now,
)


class ExpiryTest(unittest.TestCase):
    def test_extracts_video_platform_expiry_from_media_host_path(self):
        expires_at, ttl_seconds = extract_expiry_from_stream_url(
            "https://rr.media.example.test/videoplayback/expire/4102444800/id/example"
        )

        self.assertEqual(expires_at, "2100-01-01T00:00:00+00:00")
        self.assertIsNotNone(ttl_seconds)
        self.assertGreater(ttl_seconds, 0)

    def test_extracts_expiry_from_query_parameters(self):
        expires_at, ttl_seconds = extract_expiry_from_stream_url(
            "https://media.example.test/live.m3u8?expires=4102444800"
        )

        self.assertEqual(expires_at, "2100-01-01T00:00:00+00:00")
        self.assertIsNotNone(ttl_seconds)
        self.assertGreater(ttl_seconds, 0)

    def test_extracts_expiry_from_jwt_token_query_parameter(self):
        expires_at, ttl_seconds = extract_expiry_from_stream_url(
            "https://playback.delivery.example.test/master.m3u8?token="
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzM4NCJ9."
            "eyJleHAiOjQxMDI0NDQ4MDB9.signature"
        )

        self.assertEqual(expires_at, "2100-01-01T00:00:00+00:00")
        self.assertIsNotNone(ttl_seconds)
        self.assertGreater(ttl_seconds, 0)

    def test_refreshes_unresolved_channels(self):
        channel = Channel(
            id="offline.tv",
            name="Offline",
            source_url="https://example.test/live",
            logo="",
            group="general",
            source_type="video_platform",
            status="offline",
        )

        self.assertTrue(needs_refresh(channel, min_ttl_seconds=1800))

    def test_refreshes_resolved_channel_when_source_age_is_too_old(self):
        channel = Channel(
            id="game.example.test",
            name="GamePlatform",
            source_url="https://www.game.example.test/example",
            logo="",
            group="general",
            source_type="game_platform",
            status="resolved",
            resolved_at=(utc_now() - timedelta(seconds=3600)).isoformat(),
        )

        self.assertTrue(
            needs_refresh(
                channel,
                min_ttl_seconds=1800,
                max_age_by_source={"game_platform": 1800},
            )
        )

    def test_keeps_resolved_channel_with_null_ttl_when_source_has_no_max_age(self):
        channel = Channel(
            id="script.tv",
            name="Script",
            source_url="https://media.example.test/live.m3u8",
            logo="",
            group="general",
            source_type="script_discovered",
            status="resolved",
            stream_url="https://media.example.test/live.m3u8",
        )

        self.assertFalse(needs_refresh(channel, min_ttl_seconds=1800))


if __name__ == "__main__":
    unittest.main()
