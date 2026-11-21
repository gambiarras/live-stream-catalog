from tests import fixture_config  # noqa: F401

import unittest
from unittest.mock import Mock, patch

from live_stream_catalog.models import Channel
from live_stream_catalog.services.resolver import resolve_channel, resolve_channels


class FakeMultivariant:
    def __init__(self, uri):
        self.uri = uri


class FakeStream:
    def __init__(self, url, multivariant_uri=None):
        self.url = url
        self.multivariant = FakeMultivariant(multivariant_uri) if multivariant_uri else None


class ResolverTest(unittest.TestCase):
    def test_resolves_pre_resolved_direct_channels_without_streamlink_session(self):
        channel = Channel(
            id="ready.tv",
            name="Ready",
            source_url="https://media.example.test/live.m3u8",
            logo="",
            group="general",
            source_type="script_discovered",
            stream_url="https://media.example.test/live.m3u8?expires=4102444800",
            status="resolved",
        )

        with patch("live_stream_catalog.services.resolver.build_streamlink_session") as build_session:
            result = resolve_channels([channel], max_workers=6)

        build_session.assert_not_called()
        self.assertEqual(result[0].status, "resolved")
        self.assertEqual(result[0].expires_at, "2100-01-01T00:00:00+00:00")
        self.assertIsNotNone(result[0].ttl_seconds)

    def test_resolved_video_platform_channels_are_refreshed_through_streamlink(self):
        channel = Channel(
            id="video_platform.live.video",
            name="VideoPlatform Live",
            source_url="https://video.example.test/watch?v=video",
            logo="",
            group="general",
            source_type="video_platform",
            stream_url="https://old.example.test/live.m3u8",
            status="resolved",
        )
        session = Mock()
        session.streams.return_value = {"best": FakeStream("https://new.example.test/live.m3u8")}

        with patch(
            "live_stream_catalog.services.resolver.build_streamlink_session",
            return_value=session,
        ):
            result = resolve_channel(channel)

        session.streams.assert_called_once_with("https://video.example.test/watch?v=video")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.stream_url, "https://new.example.test/live.m3u8")
        self.assertIsNone(result.error)

    def test_preserves_existing_stream_url_on_transient_video_platform_rate_limit(self):
        channel = Channel(
            id="video_platform.live.video",
            name="VideoPlatform Live",
            source_url="https://video.example.test/watch?v=video",
            logo="",
            group="general",
            source_type="video_platform",
            stream_url="https://rr1---sn.example.media.example.test/videoplayback/expire/4102444800/live.m3u8",
            status="pending",
        )
        session = Mock()
        session.streams.side_effect = RuntimeError(
            "429 Client Error: Too Many Requests for url: https://www.blocked.example.test/sorry/index"
        )

        with patch(
            "live_stream_catalog.services.resolver.build_streamlink_session",
            return_value=session,
        ), self.assertLogs("live_stream_catalog.services.resolver", level="WARNING"):
            result = resolve_channel(channel)

        self.assertEqual(result.status, "resolved")
        self.assertEqual(
            result.stream_url,
            "https://rr1---sn.example.media.example.test/videoplayback/expire/4102444800/live.m3u8",
        )
        self.assertIn("transient_resolve_error", result.error)
        self.assertEqual(result.expires_at, "2100-01-01T00:00:00+00:00")

    def test_drops_expired_stream_url_on_transient_video_platform_rate_limit(self):
        channel = Channel(
            id="video_platform.live.video",
            name="VideoPlatform Live",
            source_url="https://video.example.test/watch?v=video",
            logo="",
            group="general",
            source_type="video_platform",
            stream_url="https://rr1---sn.example.media.example.test/videoplayback/expire/946684800/live.m3u8",
            status="pending",
        )
        session = Mock()
        session.streams.side_effect = RuntimeError(
            "429 Client Error: Too Many Requests for url: https://www.blocked.example.test/sorry/index"
        )

        with patch(
            "live_stream_catalog.services.resolver.build_streamlink_session",
            return_value=session,
        ), self.assertLogs("live_stream_catalog.services.resolver", level="WARNING"):
            result = resolve_channel(channel)

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.stream_url)
        self.assertIn("429 Client Error", result.error)
        self.assertIsNone(result.expires_at)
        self.assertIsNone(result.ttl_seconds)

    def test_clears_existing_stream_url_on_non_transient_error(self):
        channel = Channel(
            id="video_platform.live.video",
            name="VideoPlatform Live",
            source_url="https://video.example.test/watch?v=video",
            logo="",
            group="general",
            source_type="video_platform",
            stream_url="https://old.example.test/live.m3u8",
            status="pending",
        )
        session = Mock()
        session.streams.side_effect = RuntimeError("403 Client Error: Forbidden")

        with patch(
            "live_stream_catalog.services.resolver.build_streamlink_session",
            return_value=session,
        ), self.assertLogs("live_stream_catalog.services.resolver", level="ERROR"):
            result = resolve_channel(channel)

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.stream_url)
        self.assertEqual(result.error, "403 Client Error: Forbidden")

    def test_extracts_ttl_from_stream_multivariant_uri(self):
        channel = Channel(
            id="live_platform.live",
            name="LivePlatform Live",
            source_url="https://live.example.test/example",
            logo="",
            group="general",
            source_type="live_platform",
        )
        session = Mock()
        session.streams.return_value = {
            "best": FakeStream(
                "https://playlist.delivery.example.test/v1/playlist/final.m3u8",
                "https://playback.delivery.example.test/master.m3u8?token="
                "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzM4NCJ9."
                "eyJleHAiOjQxMDI0NDQ4MDB9.signature",
            )
        }

        with patch(
            "live_stream_catalog.services.resolver.build_streamlink_session",
            return_value=session,
        ):
            result = resolve_channel(channel)

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.stream_url, "https://playlist.delivery.example.test/v1/playlist/final.m3u8")
        self.assertEqual(result.expires_at, "2100-01-01T00:00:00+00:00")
        self.assertIsNotNone(result.ttl_seconds)

    def test_retries_empty_live_platform_streams_before_marking_offline(self):
        channel = Channel(
            id="live_platform.live",
            name="LivePlatform Live",
            source_url="https://live.example.test/example",
            logo="",
            group="general",
            source_type="live_platform",
        )
        session = Mock()
        session.streams.side_effect = [
            {},
            {},
            {"best": FakeStream("https://live_platform.example.test/live.m3u8")},
        ]

        with patch(
            "live_stream_catalog.services.resolver.build_streamlink_session",
            return_value=session,
        ), self.assertLogs("live_stream_catalog.services.resolver", level="WARNING"):
            result = resolve_channel(channel)

        self.assertEqual(session.streams.call_count, 3)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.stream_url, "https://live_platform.example.test/live.m3u8")
        self.assertIsNone(result.error)

    def test_accepts_zero_workers_when_there_is_nothing_to_resolve(self):
        result = resolve_channels([], max_workers=0)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
