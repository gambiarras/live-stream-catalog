import unittest
from unittest.mock import patch

from live_stream_catalog.sources.registry import get_resource_registry
from live_stream_catalog.sources.youtube_live_discovery import (
    YouTubeLiveDiscoveryConfig,
    discover_live_channels,
    load_config_resource,
    _youtube_dl_options,
    load_youtube_live_discovery_channels,
)


class FakeYoutubeDL:
    payload = {"entries": []}
    urls = []
    options_seen = []

    def __init__(self, options):
        self.__class__.options_seen.append(options)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def extract_info(self, url, download=False):
        self.__class__.urls.append(url)
        return self.__class__.payload


class FailingYoutubeDL(FakeYoutubeDL):
    def extract_info(self, url, download=False):
        raise RuntimeError("503 Service Unavailable")


class YoutubeLiveDiscoveryTest(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.payload = {"entries": []}
        FakeYoutubeDL.urls = []
        FakeYoutubeDL.options_seen = []

    def test_discovers_multiple_live_streams_from_streams_url(self):
        FakeYoutubeDL.payload = {
            "entries": [
                {
                    "id": "live_video_1",
                    "title": "Main court",
                    "live_status": "is_live",
                    "thumbnails": [{"url": "https://img.example.test/live.jpg"}],
                },
                {
                    "id": "past_video",
                    "title": "Past stream",
                    "live_status": "was_live",
                },
                {
                    "id": "live_video_2",
                    "title": "Second court",
                    "is_live": True,
                },
            ]
        }
        config = YouTubeLiveDiscoveryConfig(
            id="sample.tv",
            name="Sample TV",
            streams_url="https://www.youtube.com/@sample/streams",
            fallback_url="https://www.youtube.com/@sample/live",
            logo="https://img.example.test/channel.jpg",
            group="sports",
            tvg_id="SampleTV.br",
        )

        channels = discover_live_channels(config, youtube_dl_cls=FakeYoutubeDL)

        self.assertEqual(FakeYoutubeDL.urls, ["https://www.youtube.com/@sample/streams"])
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0].id, "sample.tv.live_video_1")
        self.assertEqual(channels[0].name, "Sample TV - Main court")
        self.assertEqual(channels[0].source_url, "https://www.youtube.com/watch?v=live_video_1")
        self.assertEqual(channels[0].logo, "https://img.example.test/live.jpg")
        self.assertEqual(channels[0].source_type, "youtube")
        self.assertEqual(channels[0].tvg_id, "SampleTV.br")
        self.assertEqual(channels[0].status, "pending")

        self.assertEqual(channels[1].id, "sample.tv.live_video_2")
        self.assertEqual(channels[1].name, "Sample TV - Second court")
        self.assertEqual(channels[1].logo, "https://img.example.test/channel.jpg")

    def test_falls_back_to_live_url_when_no_live_streams_are_discovered(self):
        config = YouTubeLiveDiscoveryConfig(
            id="sample.tv",
            name="Sample TV",
            streams_url="https://www.youtube.com/@sample/streams",
            fallback_url="https://www.youtube.com/@sample/live",
            logo="https://img.example.test/channel.jpg",
            group="news",
            tvg_id="SampleTV.br",
            max_results=12,
        )

        channels = discover_live_channels(config, youtube_dl_cls=FakeYoutubeDL)

        self.assertEqual(FakeYoutubeDL.urls, ["https://www.youtube.com/@sample/streams"])
        self.assertEqual(FakeYoutubeDL.options_seen[0]["playlistend"], 12)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].id, "sample.tv")
        self.assertEqual(channels[0].name, "Sample TV")
        self.assertEqual(channels[0].source_url, "https://www.youtube.com/@sample/live")
        self.assertEqual(channels[0].logo, "https://img.example.test/channel.jpg")
        self.assertEqual(channels[0].group, "news")
        self.assertEqual(channels[0].source_type, "youtube")
        self.assertEqual(channels[0].tvg_id, "SampleTV.br")

    def test_loads_channels_from_supplied_configs(self):
        FakeYoutubeDL.payload = {
            "entries": [
                {
                    "id": "live_video_1",
                    "title": "Live now",
                    "live_status": "is_live",
                }
            ]
        }
        configs = [
            YouTubeLiveDiscoveryConfig(
                id="sample.tv",
                name="Sample TV",
                streams_url="https://www.youtube.com/c/sample/streams",
                fallback_url="https://www.youtube.com/c/sample/live",
                logo="",
                group="general",
            )
        ]

        channels = load_youtube_live_discovery_channels(
            default_resolution="720p",
            youtube_dl_cls=FakeYoutubeDL,
            configs=configs,
        )

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].resolution, "720p")
        self.assertEqual(channels[0].source_url, "https://www.youtube.com/watch?v=live_video_1")

    def test_transient_discovery_failure_can_abort_without_becoming_removed(self):
        configs = [
            YouTubeLiveDiscoveryConfig(
                id="sample.tv",
                name="Sample TV",
                streams_url="https://www.youtube.com/@sample/streams",
                fallback_url="https://www.youtube.com/@sample/live",
                logo="",
                group="general",
            )
        ]

        with self.assertLogs(
            "live_stream_catalog.sources.youtube_live_discovery",
            level="ERROR",
        ), self.assertRaisesRegex(RuntimeError, "503"):
            load_youtube_live_discovery_channels(
                youtube_dl_cls=FailingYoutubeDL,
                configs=configs,
                continue_on_error=False,
            )

    def test_transient_discovery_failure_remains_retryable_by_default(self):
        config = YouTubeLiveDiscoveryConfig(
            id="sample.tv",
            name="Sample TV",
            streams_url="https://www.youtube.com/@sample/streams",
            fallback_url="https://www.youtube.com/@sample/live",
            logo="",
            group="general",
        )

        with self.assertLogs(
            "live_stream_catalog.sources.youtube_live_discovery",
            level="ERROR",
        ):
            channels = load_youtube_live_discovery_channels(
                youtube_dl_cls=FailingYoutubeDL,
                configs=[config],
            )

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].status, "pending")
        self.assertFalse(channels[0].removed)
        self.assertTrue(channels[0].requires_dynamic_resolution)

    def test_config_resource_replaces_legacy_youtube_registry(self):
        resource_names = [resource_name for resource_name, _source_type in get_resource_registry()]
        configs = load_config_resource()

        self.assertNotIn("youtube_channels.json", resource_names)
        self.assertGreaterEqual(len(configs), 1)
        self.assertTrue(all(config.streams_url for config in configs))

    def test_youtube_dl_options_accept_cookie_file_from_environment(self):
        with patch.dict("os.environ", {"YOUTUBE_COOKIES_FILE": "/tmp/youtube-cookies.txt"}):
            options = _youtube_dl_options(8)

        self.assertEqual(options["playlistend"], 8)
        self.assertEqual(options["cookiefile"], "/tmp/youtube-cookies.txt")
        self.assertEqual(options["extractor_retries"], 3)
        self.assertEqual(options["retries"], 3)


if __name__ == "__main__":
    unittest.main()
