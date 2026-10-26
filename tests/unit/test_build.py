import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_stream_catalog.config import AppConfig
from live_stream_catalog.models import Channel
from live_stream_catalog.services.build import run_build
from live_stream_catalog.services.refresh import run_refresh


class BuildTest(unittest.TestCase):
    def _config(self, directory: str) -> AppConfig:
        root = Path(directory)
        return AppConfig(
            output_path=root / "channels.json",
            meta_output_path=root / "channels.meta.json",
            log_level="INFO",
            max_workers=4,
            default_resolution="best",
            min_ttl_seconds=1800,
        )

    def test_build_and_refresh_publish_newly_discovered_direct_transport(self):
        for run in (run_build, run_refresh):
            with self.subTest(mode=run.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    config = self._config(directory)
                    existing = Channel(
                        id="fixture.channel",
                        name="Fixture Channel",
                        source_url="config://fixture/channel",
                        logo="",
                        group="general",
                        source_type="json_catalog",
                        stream_url="https://old.example.test/live.m3u8",
                        status="resolved",
                        expires_at="2000-01-01T00:00:00+00:00",
                        ttl_seconds=0,
                    )
                    config.output_path.write_text(json.dumps([existing.to_dict()]))
                    discovered = Channel(
                        id="fixture.channel",
                        name="Fixture Channel [FHD]",
                        source_url="config://fixture/channel",
                        logo="",
                        group="general",
                        source_type="json_catalog",
                        stream_url="https://new.example.test/live.m3u8",
                        status="resolved",
                        protocol="hls",
                    )
                    with patch(
                        "live_stream_catalog.services.build.load_catalog",
                        return_value=[discovered],
                    ), patch(
                        "live_stream_catalog.services.refresh._load_dynamic_channels",
                        return_value=[discovered],
                    ), patch(
                        "live_stream_catalog.services.resolver.build_streamlink_session",
                        side_effect=AssertionError("Unexpected network resolution"),
                    ):
                        run(config)

                    output = json.loads(config.output_path.read_text())
                    metadata = json.loads(config.meta_output_path.read_text())
                    self.assertEqual(len(output), 1)
                    self.assertEqual(output[0], discovered.to_dict())
                    self.assertEqual(output[0]["stream_url"], discovered.stream_url)
                    self.assertIsNone(output[0]["expires_at"])
                    self.assertEqual(metadata["resolved_channels"], 1)

    def test_reuses_unexpired_existing_stream_urls_during_full_build(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            existing = Channel(
                id="youtube.live.video",
                name="Old title",
                source_url="https://www.youtube.com/watch?v=video",
                logo="",
                group="general",
                source_type="youtube",
                stream_url="https://rr1---sn.example.googlevideo.com/videoplayback/expire/4102444800/live.m3u8",
                status="resolved",
                resolved_at="2026-07-16T00:00:00+00:00",
                expires_at="2100-01-01T00:00:00+00:00",
                ttl_seconds=3600,
            )
            config.output_path.write_text(json.dumps([existing.to_dict()]), encoding="utf-8")
            rediscovered = Channel(
                id="youtube.live.video",
                name="New title",
                source_url="https://www.youtube.com/watch?v=video",
                logo="",
                group="general",
                source_type="youtube",
            )

            with patch("live_stream_catalog.services.build.load_catalog", return_value=[rediscovered]), patch(
                "live_stream_catalog.services.build.resolve_channels",
                return_value=[],
            ) as resolve_channels:
                run_build(config)

            resolve_channels.assert_called_once_with([], max_workers=4)
            output = json.loads(config.output_path.read_text(encoding="utf-8"))
            self.assertEqual(output[0]["id"], "youtube.live.video")
            self.assertEqual(
                output[0]["stream_url"],
                "https://rr1---sn.example.googlevideo.com/videoplayback/expire/4102444800/live.m3u8",
            )
            self.assertEqual(output[0]["status"], "resolved")

    def test_refreshes_expired_existing_stream_urls_during_full_build(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            existing = Channel(
                id="youtube.live.video",
                name="Old title",
                source_url="https://www.youtube.com/watch?v=video",
                logo="",
                group="general",
                source_type="youtube",
                stream_url="https://rr1---sn.example.googlevideo.com/videoplayback/expire/946684800/live.m3u8",
                status="resolved",
                resolved_at="2026-07-16T00:00:00+00:00",
                expires_at="2000-01-01T00:00:00+00:00",
                ttl_seconds=0,
            )
            config.output_path.write_text(json.dumps([existing.to_dict()]), encoding="utf-8")
            rediscovered = Channel(
                id="youtube.live.video",
                name="New title",
                source_url="https://www.youtube.com/watch?v=video",
                logo="",
                group="general",
                source_type="youtube",
            )
            refreshed = Channel(
                id="youtube.live.video",
                name="New title",
                source_url="https://www.youtube.com/watch?v=video",
                logo="",
                group="general",
                source_type="youtube",
                stream_url="https://new.example.test/live.m3u8",
                status="resolved",
            )

            with patch("live_stream_catalog.services.build.load_catalog", return_value=[rediscovered]), patch(
                "live_stream_catalog.services.build.resolve_channels",
                return_value=[refreshed],
            ) as resolve_channels:
                run_build(config)

            self.assertEqual(resolve_channels.call_args.args[0][0].id, "youtube.live.video")
            output = json.loads(config.output_path.read_text(encoding="utf-8"))
            self.assertEqual(output[0]["stream_url"], "https://new.example.test/live.m3u8")

    def test_keeps_last_catalog_file_when_configured_source_loading_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            original = [
                {
                    "id": "last-known-good",
                    "name": "Last Known Good",
                    "source_url": "https://example.test/channel",
                    "logo": "",
                    "group": "general",
                    "source_type": "youtube",
                    "status": "resolved",
                }
            ]
            config.output_path.write_text(json.dumps(original), encoding="utf-8")

            with patch(
                "live_stream_catalog.services.build.load_catalog",
                side_effect=RuntimeError("configured source unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "configured source unavailable"):
                    run_build(config)

            self.assertEqual(json.loads(config.output_path.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
