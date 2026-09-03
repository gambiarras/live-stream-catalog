import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from live_stream_catalog.sources.stremio_addon_catalog import (
    StremioAddonCatalogConfig,
    load_config_resource,
    load_stremio_addon_channels,
    resource_url,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.requested_urls = []

    def get(self, url, headers=None, timeout=None):
        self.requested_urls.append(url)
        payloads = {
            "https://addon.example.test/manifest.json": {
                "behaviorHints": {"adult": False},
                "catalogs": [
                    {
                        "type": "tv",
                        "id": "live",
                        "name": "Recomendados",
                    }
                ],
            },
            "https://addon.example.test/catalog/tv/live.json": {
                "metas": [
                    {
                        "id": "adult-swim",
                        "type": "tv",
                        "name": "Adult Swim",
                        "genres": ["Animation"],
                        "poster": "https://img.example.test/adult-swim.png",
                        "tvg_id": "AdultSwim.br",
                    },
                    {
                        "id": "explicit-adult",
                        "type": "tv",
                        "name": "Explicit Adult",
                        "genres": ["+18"],
                    },
                ]
            },
            "https://addon.example.test/stream/tv/adult-swim.json": {
                "streams": [
                    {
                        "name": "Full HD",
                        "url": "https://media.example.test/adult-swim-fhd.m3u8",
                        "behaviorHints": {
                            "proxyHeaders": {
                                "request": {"Referer": "https://player.example.test/"}
                            }
                        },
                    },
                    {
                        "name": "4K HEVC",
                        "url": "https://media.example.test/adult-swim-4k.mpd",
                        "drm": {
                            "type": "widevine",
                            "license_url": "https://license.example.test/widevine",
                        },
                    },
                ]
            },
        }
        if url not in payloads:
            raise AssertionError(f"Unexpected URL: {url}")
        return FakeResponse(payloads[url])


class MountedFakeSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.adapters = {}
        self.closed = False

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def close(self):
        self.closed = True


class PartiallyFailingSession:
    def __init__(self):
        self.requested_urls = []

    def get(self, url, headers=None, timeout=None):
        self.requested_urls.append(url)
        if url.endswith("/manifest.json"):
            return FakeResponse(
                {"catalogs": [{"type": "channel", "id": "live", "name": "Live"}]}
            )
        if url.endswith("/catalog/channel/live.json"):
            return FakeResponse(
                {
                    "metas": [
                        {"id": "working", "type": "channel", "name": "Working"},
                        {"id": "timeout", "type": "channel", "name": "Timeout"},
                    ]
                }
            )
        if url.endswith("/stream/channel/working.json"):
            return FakeResponse(
                {"streams": [{"url": "https://media.example.test/working.m3u8"}]}
            )
        raise requests.Timeout("transient timeout")


class RecoveredSession(PartiallyFailingSession):
    def get(self, url, headers=None, timeout=None):
        if url.endswith("/stream/channel/timeout.json"):
            self.requested_urls.append(url)
            return FakeResponse(
                {"streams": [{"url": "https://media.example.test/timeout.m3u8"}]}
            )
        return super().get(url, headers=headers, timeout=timeout)


class FailingSession:
    def get(self, url, headers=None, timeout=None):
        raise requests.Timeout("source unavailable")


class StremioAddonCatalogTest(unittest.TestCase):
    def test_loads_manifest_url_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "LIVE_ADDON_MANIFEST_URL": "https://addon.example.test/manifest.json",
                "LIVE_ADDON_TOTAL_TIMEOUT": "1",
            },
        ):
            configs = load_config_resource()

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].provider_id, "addon_catalog_1")
        self.assertEqual(configs[0].manifest_url, "https://addon.example.test/manifest.json")
        self.assertEqual(configs[0].max_workers, 16)
        self.assertEqual(configs[0].timeout, 8)
        self.assertFalse(hasattr(configs[0], "total_timeout"))

    def test_sizes_owned_http_pool_to_worker_count(self):
        config = StremioAddonCatalogConfig(
            id="addon_catalog_1",
            provider_id="addon_catalog_1",
            manifest_url="https://addon.example.test/manifest.json",
            max_workers=16,
        )
        session = MountedFakeSession()

        with patch(
            "live_stream_catalog.sources.stremio_addon_catalog.requests.Session",
            return_value=session,
        ):
            load_stremio_addon_channels([config])

        self.assertEqual(session.adapters["https://"]._pool_maxsize, 16)
        self.assertEqual(session.adapters["http://"]._pool_maxsize, 16)
        self.assertTrue(session.closed)

    def test_builds_protocol_resource_url(self):
        self.assertEqual(
            resource_url(
                "https://addon.example.test/manifest.json",
                "catalog",
                "tv",
                "live",
                {"skip": "100"},
            ),
            "https://addon.example.test/catalog/tv/live/skip=100.json",
        )

    def test_keeps_variants_and_does_not_filter_adult_swim_by_name(self):
        config = StremioAddonCatalogConfig(
            id="addon_catalog_1",
            provider_id="addon_catalog_1",
            manifest_url="https://addon.example.test/manifest.json",
        )
        session = FakeSession()

        channels = load_stremio_addon_channels([config], session=session)

        self.assertEqual(len(channels), 2)
        self.assertEqual({channel.name for channel in channels}, {"Adult Swim [FHD]", "Adult Swim [4K] [HEVC]"})
        self.assertEqual({channel.tvg_id for channel in channels}, {"AdultSwim.br"})
        self.assertEqual(len({channel.logical_channel_id for channel in channels}), 1)
        self.assertEqual({channel.protocol for channel in channels}, {"hls", "dash"})
        self.assertTrue(channels[0].publishable_static)
        self.assertFalse(channels[1].publishable_static)
        self.assertNotIn("explicit-adult", " ".join(session.requested_urls))
        self.assertNotIn("addon.example.test", str([channel.to_dict() for channel in channels]))

    def test_keeps_successful_channels_when_another_stream_times_out(self):
        config = StremioAddonCatalogConfig(
            id="addon_catalog_1",
            provider_id="addon_catalog_1",
            manifest_url="https://addon.example.test/manifest.json",
            max_workers=2,
        )

        with self.assertLogs(
            "live_stream_catalog.sources.stremio_addon_catalog",
            level="WARNING",
        ):
            channels = load_stremio_addon_channels(
                [config],
                session=PartiallyFailingSession(),
            )

        self.assertEqual([channel.name for channel in channels], ["Working"])

    def test_reuses_fresh_persistent_cache_without_network_requests(self):
        with TemporaryDirectory() as directory:
            config = StremioAddonCatalogConfig(
                id="addon_catalog_1",
                provider_id="addon_catalog_1",
                manifest_url="https://addon.example.test/manifest.json",
                cache_dir=Path(directory),
            )

            first = load_stremio_addon_channels([config], session=FakeSession())
            with self.assertLogs(
                "live_stream_catalog.sources.stremio_addon_catalog",
                level="INFO",
            ) as captured:
                second = load_stremio_addon_channels([config], session=FailingSession())

            self.assertEqual([item.to_dict() for item in second], [item.to_dict() for item in first])
            self.assertTrue((Path(directory) / "addon-catalog-1.json").is_file())
            self.assertIn("Using fresh addon cache", "\n".join(captured.output))

    def test_uses_last_known_good_cache_after_transient_source_failure(self):
        with TemporaryDirectory() as directory:
            config = StremioAddonCatalogConfig(
                id="addon_catalog_1",
                provider_id="addon_catalog_1",
                manifest_url="https://addon.example.test/manifest.json",
                cache_dir=Path(directory),
                cache_ttl=0,
                cache_lkg=172800,
            )
            expected = load_stremio_addon_channels([config], session=FakeSession())

            with self.assertLogs(
                "live_stream_catalog.sources.stremio_addon_catalog",
                level="WARNING",
            ) as captured:
                actual = load_stremio_addon_channels([config], session=FailingSession())

            self.assertEqual([item.to_dict() for item in actual], [item.to_dict() for item in expected])
            self.assertIn("last-known-good addon cache", "\n".join(captured.output))

    def test_resumes_incomplete_catalog_from_incremental_checkpoint(self):
        with TemporaryDirectory() as directory:
            config = StremioAddonCatalogConfig(
                id="addon_catalog_1",
                provider_id="addon_catalog_1",
                manifest_url="https://addon.example.test/manifest.json",
                cache_dir=Path(directory),
                max_workers=2,
            )
            first_session = PartiallyFailingSession()
            first = load_stremio_addon_channels([config], session=first_session)
            checkpoint = Path(directory) / "addon-catalog-1.checkpoint.json"

            recovered_session = RecoveredSession()
            with self.assertLogs(
                "live_stream_catalog.sources.stremio_addon_catalog",
                level="INFO",
            ) as captured:
                second = load_stremio_addon_channels([config], session=recovered_session)

            self.assertEqual([channel.name for channel in first], ["Working"])
            self.assertEqual({channel.name for channel in second}, {"Working", "Timeout"})
            self.assertNotIn(
                "https://addon.example.test/stream/channel/working.json",
                recovered_session.requested_urls,
            )
            self.assertFalse(checkpoint.exists())
            self.assertIn("Resuming addon catalog", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
