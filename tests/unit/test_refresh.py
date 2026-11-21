from tests import fixture_config  # noqa: F401

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
            source_type="addon_catalog",
            provider_id="addon_catalog_1",
            logical_channel_id="addon_catalog_1.channel",
            variant_id="addon.channel.fhd",
            status="resolved",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_configured_addon_catalogs",
            side_effect=RuntimeError("boom"),
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_json_catalogs",
            return_value=[],
        ), patch(
            "live_stream_catalog.services.refresh.load_video_platform_live_discovery_channels",
            return_value=[],
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            return_value=[],
        ), self.assertLogs("live_stream_catalog.services.refresh", level="ERROR"):
            channels = _load_dynamic_channels(config, [existing])

        self.assertEqual(channels, [existing])

    def test_loads_video_platform_and_script_discovered_dynamic_channels(self):
        config = AppConfig(
            output_path=None,
            meta_output_path=None,
            log_level="INFO",
            max_workers=1,
            default_resolution="best",
            min_ttl_seconds=1800,
        )
        video_platform_channel = Channel(
            id="video_platform.live",
            name="VideoPlatform Live",
            source_url="https://video.example.test/watch?v=abc",
            logo="",
            group="general",
            source_type="video_platform",
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
            "live_stream_catalog.services.refresh.load_video_platform_live_discovery_channels",
            return_value=[video_platform_channel],
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            return_value=[script_channel],
        ):
            channels = _load_dynamic_channels(config, [])

        self.assertEqual(channels, [video_platform_channel, script_channel])

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
            id="video_platform.old",
            name="Existing",
            source_url="https://video.example.test/watch?v=old",
            logo="",
            group="general",
            source_type="video_platform",
        )
        static_channel = Channel(
            id="game_platform.old",
            name="Static",
            source_url="https://www.game.example.test/example",
            logo="",
            group="general",
            source_type="game_platform",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_video_platform_live_discovery_channels",
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
        new_video_platform = Channel(
            id="video_platform.new",
            name="New VideoPlatform",
            source_url="https://video.example.test/watch?v=new",
            logo="",
            group="general",
            source_type="video_platform",
        )

        with patch(
            "live_stream_catalog.services.refresh.load_video_platform_live_discovery_channels",
            return_value=[new_video_platform],
        ), patch(
            "live_stream_catalog.services.refresh.load_configured_rest_catalogs",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs("live_stream_catalog.services.refresh", level="ERROR"):
            channels = _load_dynamic_channels(config, [existing_script])

        self.assertEqual(channels, [new_video_platform, existing_script])

    def test_carries_previous_resolution_for_rediscovered_dynamic_channels(self):
        previous = Channel(
            id="video_platform.live.video",
            name="Old title",
            source_url="https://video.example.test/watch?v=video",
            logo="",
            group="general",
            source_type="video_platform",
            stream_url="https://old.example.test/live.m3u8",
            status="resolved",
            error=None,
            resolved_at="2026-07-16T00:00:00+00:00",
            expires_at="2026-07-16T02:00:00+00:00",
            ttl_seconds=3600,
        )
        rediscovered = Channel(
            id="video_platform.live.video",
            name="New title",
            source_url="https://video.example.test/watch?v=video",
            logo="",
            group="general",
            source_type="video_platform",
        )
        changed_video = Channel(
            id="video_platform.live.other",
            name="Other",
            source_url="https://video.example.test/watch?v=other",
            logo="",
            group="general",
            source_type="video_platform",
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
