import unittest
from unittest.mock import Mock, patch

from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import needs_refresh
from live_stream_catalog.services.removal import terminal_removal_reason
from live_stream_catalog.services.resolver import resolve_channel


def make_channel(source_type: str, source_url: str) -> Channel:
    return Channel(
        id="channel",
        name="Channel",
        source_url=source_url,
        logo="",
        group="general",
        source_type=source_type,
    )


class RemovalTest(unittest.TestCase):
    def test_detects_explicitly_removed_account_but_not_individual_video(self):
        error = RuntimeError("This channel does not exist")

        self.assertEqual(
            terminal_removal_reason("youtube", "https://www.youtube.com/@missing/live", error),
            "channel_not_found",
        )
        self.assertIsNone(
            terminal_removal_reason("youtube", "https://www.youtube.com/watch?v=missing", error)
        )

    def test_does_not_treat_offline_or_transport_errors_as_removed(self):
        for error in (
            "no_stream_found",
            "404 Client Error on manifest.m3u8",
            "429 Too Many Requests",
            "timed out",
            "503 Service Unavailable",
        ):
            self.assertIsNone(
                terminal_removal_reason("twitch", "https://www.twitch.tv/example", error)
            )

    @patch("live_stream_catalog.services.resolver.build_streamlink_session")
    def test_marks_explicitly_missing_account_as_removed_and_stops_refresh(self, build_session: Mock):
        session = Mock()
        session.streams.side_effect = RuntimeError("User not found")
        build_session.return_value = session
        channel = make_channel("twitch", "https://www.twitch.tv/missing")

        result = resolve_channel(channel)

        self.assertTrue(result.removed)
        self.assertEqual(result.status, "removed")
        self.assertEqual(result.removal_reason, "user_not_found")
        self.assertFalse(result.publishable_static)
        self.assertFalse(needs_refresh(result, min_ttl_seconds=1800))

    @patch("live_stream_catalog.services.resolver.build_streamlink_session")
    def test_keeps_channel_offline_when_no_live_stream_exists(self, build_session: Mock):
        session = Mock()
        session.streams.return_value = {}
        build_session.return_value = session
        channel = make_channel("twitch", "https://www.twitch.tv/offline")

        result = resolve_channel(channel)

        self.assertFalse(result.removed)
        self.assertEqual(result.status, "offline")
        self.assertTrue(needs_refresh(result, min_ttl_seconds=1800))


if __name__ == "__main__":
    unittest.main()
