import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_stream_catalog.config import AppConfig
from live_stream_catalog.models import Channel
from live_stream_catalog.services.build import run_build


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
