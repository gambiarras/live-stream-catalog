import unittest
from unittest.mock import patch

from live_stream_catalog.config import AppConfig
from live_stream_catalog.models import Channel
from live_stream_catalog.services.catalog_state import carry_previous_resolution
from live_stream_catalog.services.refresh import _load_dynamic_channels


class RefreshTest(unittest.TestCase):
    def test_keeps_existing_addon_channels_when_refresh_fails(self):
        config = AppConfig(
            output_path=None,
            meta_output_path=None,
            log_level="INFO",
            max_workers=1,
            default_resolution="best",
            min_ttl_seconds=1800,
        )
        existing = Channel(
            id="addon.channel.fhd",
            name="Addon Channel [FHD]",
            source_url="config://addon_catalog_1/channel",
            stream_url="https://media.example.test/live.m3u8",
            logo="",
            group="TV Aberta",
            source_type="stremio_addon",
            provider_id="addon_catalog_1",
            logical_channel_id="addon_catalog_1.channel",
            variant_id="addon.channel.fhd",
            status="resolved",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_configured_stremio_addons",
            side_effect=RuntimeError("boom"),
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_json_catalogs",
            return_value=[],
        ), patch(
            "live_stream_catalog.services.refresh.load_youtube_live_discovery_channels",
            return_value=[],
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            return_value=[],
        ), self.assertLogs("live_stream_catalog.services.refresh", level="ERROR"):
            channels = _load_dynamic_channels(config, [existing])

        self.assertEqual(channels, [existing])

    def test_loads_youtube_and_script_discovered_dynamic_channels(self):
        config = AppConfig(
            output_path=None,
            meta_output_path=None,
            log_level="INFO",
            max_workers=1,
            default_resolution="best",
            min_ttl_seconds=1800,
        )
        youtube_channel = Channel(
            id="youtube.live",
            name="YouTube Live",
            source_url="https://www.youtube.com/watch?v=abc",
            logo="",
            group="general",
            source_type="youtube",
        )
        script_channel = Channel(
            id="script.live",
            name="Script Live",
            source_url="https://media.example.test/live.m3u8",
            logo="",
            group="general",
            source_type="script_discovered",
            stream_url="https://media.example.test/live.m3u8",
            status="resolved",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_youtube_live_discovery_channels",
            return_value=[youtube_channel],
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            return_value=[script_channel],
        ):
            channels = _load_dynamic_channels(config, [])

        self.assertEqual(channels, [youtube_channel, script_channel])

    def test_keeps_existing_dynamic_channels_when_refresh_fails(self):
        config = AppConfig(
            output_path=None,
            meta_output_path=None,
            log_level="INFO",
            max_workers=1,
            default_resolution="best",
            min_ttl_seconds=1800,
        )
        existing_dynamic = Channel(
            id="youtube.old",
            name="Existing",
            source_url="https://www.youtube.com/watch?v=old",
            logo="",
            group="general",
            source_type="youtube",
        )
        static_channel = Channel(
            id="twitch.old",
            name="Static",
            source_url="https://www.twitch.tv/example",
            logo="",
            group="general",
            source_type="twitch",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_youtube_live_discovery_channels",
            side_effect=RuntimeError("boom"),
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            return_value=[],
        ), self.assertLogs("live_stream_catalog.services.refresh", level="ERROR"):
            channels = _load_dynamic_channels(config, [existing_dynamic, static_channel])

        self.assertEqual(channels, [existing_dynamic])

    def test_keeps_only_failed_dynamic_source_when_other_dynamic_source_loads(self):
        config = AppConfig(
            output_path=None,
            meta_output_path=None,
            log_level="INFO",
            max_workers=1,
            default_resolution="best",
            min_ttl_seconds=1800,
        )
        existing_script = Channel(
            id="script.old",
            name="Existing Script",
            source_url="https://media.example.test/old.m3u8",
            logo="",
            group="general",
            source_type="script_discovered",
            stream_url="https://media.example.test/old.m3u8",
            status="resolved",
        )
        new_youtube = Channel(
            id="youtube.new",
            name="New YouTube",
            source_url="https://www.youtube.com/watch?v=new",
            logo="",
            group="general",
            source_type="youtube",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_youtube_live_discovery_channels",
            return_value=[new_youtube],
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs("live_stream_catalog.services.refresh", level="ERROR"):
            channels = _load_dynamic_channels(config, [existing_script])

        self.assertEqual(channels, [new_youtube, existing_script])

    def test_carries_previous_resolution_for_rediscovered_dynamic_channels(self):
        previous = Channel(
            id="youtube.live.video",
            name="Old title",
            source_url="https://www.youtube.com/watch?v=video",
            logo="",
            group="general",
            source_type="youtube",
            stream_url="https://old.example.test/live.m3u8",
            status="resolved",
            error=None,
            resolved_at="2026-07-16T00:00:00+00:00",
            expires_at="2026-07-16T02:00:00+00:00",
            ttl_seconds=3600,
        )
        rediscovered = Channel(
            id="youtube.live.video",
            name="New title",
            source_url="https://www.youtube.com/watch?v=video",
            logo="",
            group="general",
            source_type="youtube",
        )
        changed_video = Channel(
            id="youtube.live.other",
            name="Other",
            source_url="https://www.youtube.com/watch?v=other",
            logo="",
            group="general",
            source_type="youtube",
        )

        channels = carry_previous_resolution([rediscovered, changed_video], [previous])

        self.assertEqual(channels[0].stream_url, "https://old.example.test/live.m3u8")
        self.assertEqual(channels[0].status, "resolved")
        self.assertEqual(channels[0].resolved_at, "2026-07-16T00:00:00+00:00")
        self.assertEqual(channels[0].expires_at, "2026-07-16T02:00:00+00:00")
        self.assertEqual(channels[0].ttl_seconds, 3600)
        self.assertIsNone(channels[1].stream_url)


if __name__ == "__main__":
    unittest.main()
