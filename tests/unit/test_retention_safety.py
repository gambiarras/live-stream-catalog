from tests import fixture_config  # noqa: F401

# isort: split

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from live_stream_catalog.config import AppConfig
from live_stream_catalog.models import Channel
from live_stream_catalog.services.build import run_build
from live_stream_catalog.services.discovery import (
    DiscoveryCache,
    DiscoveryContext,
    discovery_scope,
)
from live_stream_catalog.services.expiry import needs_refresh
from live_stream_catalog.services.resolver import resolve_channel
from live_stream_catalog.sources import directory_catalog
from live_stream_catalog.video_platform_transport import is_public_video_platform_hls
from tests.unit.test_directory_catalog import Response, Session
from tests.unit.test_private_catalog_state import NOW, channel


class RetentionSafetyTest(unittest.TestCase):
    def test_private_paths_cannot_alias_publication_files_before_any_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, meta = root / "channels.json", root / "channels.meta.json"
            output.write_bytes(b'[{"previous":"public-array"}]')
            meta.write_bytes(b'{"previous":"public-metadata"}')
            config = AppConfig(output, meta, "INFO", 1, "best", 900)
            expected = {output: output.read_bytes(), meta: meta.read_bytes()}
            for variable in ("LIVE_CATALOG_STATE_FILE", "LIVE_DISCOVERY_CACHE_DIR"):
                for target in (output, meta):
                    alias = root / "private-alias"
                    alias.symlink_to(target)
                    try:
                        for path in (target, alias, target.parent / "." / target.name):
                            with (
                                self.subTest(variable=variable, path=path),
                                patch.dict(
                                    "os.environ",
                                    {
                                        "LIVE_CATALOG_STATE_FILE": str(
                                            root / "private" / "state.json"
                                        ),
                                        "LIVE_DISCOVERY_CACHE_DIR": str(
                                            root / "private" / "discovery"
                                        ),
                                        variable: str(path),
                                    },
                                ),
                                patch(
                                    "live_stream_catalog.services.build.load_catalog"
                                ) as discovery,
                                patch(
                                    "live_stream_catalog.services.build.resolve_channels"
                                ) as resolution,
                                patch(
                                    "live_stream_catalog.services.catalog_pipeline.write_json_atomic"
                                ) as public_write,
                                patch(
                                    "live_stream_catalog.services.catalog_pipeline.save_private_state"
                                ) as private_write,
                            ):
                                with self.assertRaisesRegex(
                                    ValueError, "must not collide"
                                ):
                                    run_build(config)
                                discovery.assert_not_called()
                                resolution.assert_not_called()
                                public_write.assert_not_called()
                                private_write.assert_not_called()
                                for artifact, original in expected.items():
                                    self.assertEqual(artifact.read_bytes(), original)
                                self.assertFalse((root / "private").exists())
                    finally:
                        alias.unlink()

    def test_malformed_header_urls_fail_closed_in_public_dto_and_media_gate(self):
        expiry = int((NOW + timedelta(hours=2)).timestamp())
        for header in (
            "https://source.example.test:bad/",
            "https://source.example.test:0/",
            "https://[invalid/",
            "https:///missing",
            "https://source.example.test/with space",
            "https://source.example.test/\\path",
        ):
            for kind in ("video_platform", "custom_platform"):
                with self.subTest(header=header, kind=kind):
                    value = channel(
                        source_type=kind,
                        source_url="https://video.example.test/watch?v=abcdefghijk",
                        status="resolved",
                        stream_url=f"https://media.example.test/live.m3u8?expire={expiry}&signature=media",
                        protocol="hls",
                        request_headers={"Referer": header},
                    )
                    self.assertIsNone(value.to_dict()["stream_url"])
                    self.assertEqual(value.to_dict()["request_headers"], {})
                    if kind == "video_platform":
                        self.assertFalse(is_public_video_platform_hls(value, now=NOW))

    def test_credential_header_aliases_remain_private_in_static_entries(self):
        for name in (
            "api_key",
            "api-key",
            "access_token",
            "token",
            "cookie",
            "Authorization",
        ):
            value = channel(
                status="resolved",
                stream_url="https://media.example.test/live.m3u8",
                request_headers={name: "private-value", "User-Agent": "Sample"},
            )
            public = value.to_dict()
            self.assertEqual(public["request_headers"], {"User-Agent": "Sample"})
            self.assertIsNone(public["stream_url"])
            self.assertNotIn("private-value", json.dumps(public))

    def test_media_gate_allows_only_media_signature_without_private_header_context(
        self,
    ):
        expiry = int((NOW + timedelta(hours=2)).timestamp())
        for static in (True, False):
            for private in ("api_key", "api-key", "access_token", "token", "signature"):
                value = channel(
                    source_type="video_platform",
                    source_url="https://video.example.test/watch?v=abcdefghijk",
                    stream_url=f"https://media.example.test/live.m3u8?expire={expiry}&signature=public-media",
                    status="resolved",
                    protocol="hls",
                    publishable_static=static,
                    request_headers={
                        "Referer": "https://page.example.test/?" + private + "=private"
                    },
                )
                self.assertFalse(is_public_video_platform_hls(value, now=NOW))
                self.assertIsNone(value.to_dict()["stream_url"])
            value.request_headers = {
                "Referer": "https://page.example.test/",
                "User-Agent": "Sample",
            }
            self.assertTrue(is_public_video_platform_hls(value, now=NOW))

    def test_sensitive_url_headers_and_credential_aliases_are_private_before_and_after_import(
        self,
    ):
        for name in ("api_key", "api-key", "access_token", "%61pi_key", "token"):
            private_url = "https://page.example.test/?" + name + "=private-value"
            for static in (True, False):
                with self.subTest(name=name, static=static):
                    value = channel(
                        source_url=private_url,
                        stream_url=private_url,
                        status="resolved",
                        publishable_static=static,
                        request_headers={
                            "Referer": private_url,
                            "User-Agent": "Sample",
                        },
                    )
                    public = value.to_dict()
                    self.assertIsNone(public["source_url"])
                    self.assertIsNone(public["stream_url"])
                    self.assertEqual(
                        public["request_headers"], {"User-Agent": "Sample"}
                    )
                    self.assertNotIn("private-value", json.dumps(public))
                    data = value.to_state_dict() | {
                        "source_url": "https://page.example.test/",
                        "stream_url": "https://media.example.test/live.m3u8",
                    }
                    imported = Channel.from_dict(data)
                    self.assertIsNone(imported.to_dict()["stream_url"])
                    self.assertEqual(imported.request_headers, {"User-Agent": "Sample"})
                    restored = Channel.from_state_dict(data)
                    self.assertEqual(restored.request_headers["Referer"], private_url)
                    self.assertIsNone(restored.to_dict()["stream_url"])

    def test_transient_retention_keeps_declared_expiry_and_does_not_create_permanent_media(
        self,
    ):
        expiry = (NOW + timedelta(minutes=30)).isoformat()
        observed = (NOW - timedelta(minutes=10)).isoformat()
        value = channel(
            source_type="custom_platform",
            status="resolved",
            stream_url="https://media.example.test/live.m3u8",
            expires_at=expiry,
            resolved_at=observed,
        )
        session = Mock()
        session.streams.side_effect = RuntimeError("429 temporary failure")
        with (
            patch(
                "live_stream_catalog.services.resolver.build_streamlink_session",
                return_value=session,
            ),
            patch("live_stream_catalog.services.resolver.utc_now", return_value=NOW),
            self.assertLogs(level="WARNING"),
        ):
            result = resolve_channel(value)
        self.assertEqual(result.expires_at, expiry)
        self.assertEqual(result.ttl_seconds, 1800)
        self.assertEqual(result.resolved_at, observed)
        with patch(
            "live_stream_catalog.services.expiry.utc_now",
            return_value=NOW + timedelta(minutes=31),
        ):
            self.assertTrue(needs_refresh(result, 900))
        with (
            patch(
                "live_stream_catalog.services.resolver.build_streamlink_session",
                return_value=session,
            ),
            patch(
                "live_stream_catalog.services.resolver.utc_now",
                return_value=NOW + timedelta(minutes=31),
            ),
            self.assertLogs(level="WARNING"),
        ):
            expired = resolve_channel(result)
        self.assertIsNone(expired.stream_url)
        self.assertEqual(expired.resolved_at, observed)

    def test_malformed_declared_expiry_forces_refresh_and_prevents_retention(self):
        for invalid in ("malformed", "", "2026-11-22T12:00:00", 123):
            value = channel(
                source_type="script_discovered",
                status="resolved",
                stream_url="https://media.example.test/live.m3u8",
                expires_at=invalid,
                resolved_at=NOW.isoformat(),
            )
            self.assertTrue(needs_refresh(value, 900))
            result = resolve_channel(value)
            self.assertIsNone(result.stream_url)
            self.assertEqual(result.error, "expired_transport")
        value = channel(
            source_type="custom_platform",
            status="resolved",
            stream_url="https://media.example.test/live.m3u8",
            expires_at="malformed",
            resolved_at=NOW.isoformat(),
        )
        session = Mock()
        session.streams.side_effect = RuntimeError("429 temporary failure")
        with (
            patch(
                "live_stream_catalog.services.resolver.build_streamlink_session",
                return_value=session,
            ),
            self.assertLogs(level="WARNING"),
        ):
            result = resolve_channel(value)
        self.assertIsNone(result.stream_url)
        self.assertEqual(result.resolved_at, NOW.isoformat())

    def test_directory_partial_player_failure_retains_only_affected_channels_from_lkg(
        self,
    ):
        class PlayerSession(Session):
            def __init__(self, *, fail=False, other_url="old"):
                super().__init__()
                self.fail = fail
                self.other_url = other_url

            def get(self, url, **kwargs):
                if urlsplit(url).path == "/player/public-tv" and self.fail:
                    return Response("unavailable", 503)
                if urlsplit(url).path == "/player/unavailable-tv":
                    return Response(
                        '<source src="https://media.example.test/'
                        + self.other_url
                        + '.m3u8">'
                    )
                return super().get(url, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = directory_catalog.DirectoryCatalogConfig(
                ("public-tv", "unavailable-tv"),
                reviewed_stream_hosts=("media.example.test",),
                cache_dir=root / "cache",
                cache_ttl=60,
                cache_lkg=120,
            )
            with patch(
                "live_stream_catalog.sources.directory_catalog.time.time",
                return_value=100,
            ):
                first = directory_catalog.load_directory_catalog_channels(
                    config, session=PlayerSession()
                )
            before = directory_catalog._cache_path(config).read_bytes()
            context = DiscoveryContext(DiscoveryCache(root / "discovery"))
            with (
                patch(
                    "live_stream_catalog.sources.directory_catalog.time.time",
                    return_value=161,
                ),
                discovery_scope(context),
                self.assertLogs(level="WARNING"),
            ):
                second = directory_catalog.load_directory_catalog_channels(
                    config, session=PlayerSession(fail=True, other_url="fresh")
                )
            self.assertEqual(second[0].stream_url, first[0].stream_url)
            self.assertEqual(second[0].resolved_at, first[0].resolved_at)
            self.assertEqual(
                second[1].stream_url, "https://media.example.test/fresh.m3u8"
            )
            self.assertEqual(directory_catalog._cache_path(config).read_bytes(), before)
            observed = context.observations["directory_catalog"]
            self.assertEqual(observed["origin"], "retained")
            self.assertFalse(observed["complete"])
            self.assertEqual(observed["reason"], "partial_discovery")
            self.assertEqual(
                observed["observed_at"], datetime.fromtimestamp(100, UTC).isoformat()
            )
