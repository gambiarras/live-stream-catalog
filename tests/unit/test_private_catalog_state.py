from tests import fixture_config  # noqa: F401

# isort: split

import hashlib
import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from live_stream_catalog.config import AppConfig
from live_stream_catalog.io.catalog_state_store import (
    CatalogSnapshot,
    load_private_state,
    private_state_path,
    save_private_state,
)
from live_stream_catalog.models import Channel
from live_stream_catalog.services.build import run_build
from live_stream_catalog.services.catalog_pipeline import (
    finish_catalog_run,
    prepare_catalog_run,
)
from live_stream_catalog.services.catalog_state import carry_previous_resolution
from live_stream_catalog.services.discovery import discovery_scope
from live_stream_catalog.services.expiry import needs_refresh
from live_stream_catalog.services.refresh import run_refresh
from live_stream_catalog.services.resolver import resolve_channel
from live_stream_catalog.sources import addon_catalog, directory_catalog
from live_stream_catalog.sources.loader import _deduplicate
from live_stream_catalog.sources.script_discovered_catalog import row_to_channel

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def channel(**values):
    base = {
        "id": "sample",
        "name": "Sample",
        "source_url": "https://game.example.test/sample",
        "logo": "",
        "group": "general",
        "source_type": "game_platform",
    }
    base.update(values)
    return Channel(**base)


class PrivateCatalogStateTest(unittest.TestCase):
    def config(self, root):
        return AppConfig(
            root / "channels.json", root / "channels.meta.json", "INFO", 2, "best", 1800
        )

    def test_full_private_state_and_public_dto_have_distinct_serialization_contracts(
        self,
    ):
        item = channel(
            request_headers={
                "Cookie": "private-session",
                "Referer": "https://page.example.test/",
            },
            secret_refs={"headers": "PRIVATE_HEADERS"},
            drm={"type": "declared", "token": "private-license"},
            stream_url="https://media.example.test/live.m3u8?token=private-session",
            status="resolved",
            publishable_static=False,
            requires_dynamic_resolution=True,
            feed_id="regional-a",
            tags=["news", "regional"],
        )
        declared = item.declared_inputs()
        dto = item.to_public_dto()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_private_state(path, CatalogSnapshot([item]))
            restored = load_private_state(path).channels[0]
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(restored.request_headers["Cookie"], "private-session")
            self.assertEqual(restored.drm["token"], "private-license")
            self.assertEqual(restored.declared_inputs(), declared)
            self.assertEqual(restored.stream_url, item.stream_url)
        public = dto.to_dict()
        self.assertNotIn("private-session", json.dumps(public))
        self.assertNotIn("private-license", json.dumps(public))
        self.assertNotIn("resolution_inputs", public)
        self.assertNotIn("_resolution_inputs", public)
        self.assertNotIn("source_instance", public)
        self.assertEqual(public["feed_id"], "regional-a")
        self.assertEqual(public["tags"], ["news", "regional"])
        item.name = "Changed"
        public["tags"].append("changed")
        self.assertEqual(dto.to_dict()["name"], "Sample")
        self.assertEqual(dto.to_dict()["tags"], ["news", "regional"])
        with self.assertRaises(TypeError):
            dto.fields["name"] = "unsafe"

    def test_declared_inputs_survive_derived_capabilities_and_private_reuse(self):
        original = channel(request_headers={"User-Agent": "sample"})
        declared = original.declared_inputs()
        original.stream_url = "https://media.example.test/live.m3u8"
        original.protocol = "hls"
        original.status = "resolved"
        original.resolved_at = NOW.isoformat()
        original.requires_dynamic_resolution = True
        original.publishable_static = False
        restored = Channel.from_state_dict(original.to_state_dict())
        discovered = channel(request_headers={"user-agent": "sample"})
        self.assertEqual(restored.declared_inputs(), declared)
        result = carry_previous_resolution([discovered], [restored])[0]
        self.assertEqual(result.stream_url, original.stream_url)
        self.assertTrue(result.requires_dynamic_resolution)
        self.assertFalse(result.publishable_static)
        self.assertEqual(result.resolved_at, NOW.isoformat())
        changed = channel(request_headers={"User-Agent": "new-context"})
        self.assertIsNone(
            carry_previous_resolution([changed], [restored])[0].stream_url
        )

    def test_build_and_refresh_use_complete_state_even_when_public_transport_was_omitted(
        self,
    ):
        for second_run in (run_build, run_refresh):
            with (
                self.subTest(mode=second_run.__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                config = self.config(root)
                session = Mock()
                session.streams.return_value = {
                    "best": Mock(
                        url="https://media.example.test/live.m3u8", multivariant=None
                    )
                }
                with (
                    patch(
                        "live_stream_catalog.services.build.load_catalog",
                        side_effect=lambda **_: [channel()],
                    ),
                    patch(
                        "live_stream_catalog.services.refresh._load_dynamic_channels",
                        return_value=[],
                    ),
                    patch(
                        "live_stream_catalog.services.resolver.build_streamlink_session",
                        return_value=session,
                    ) as sessions,
                    patch(
                        "live_stream_catalog.services.resolver.utc_now",
                        return_value=NOW,
                    ),
                    patch(
                        "live_stream_catalog.services.expiry.utc_now", return_value=NOW
                    ),
                ):
                    run_build(config)
                    first = json.loads(config.output_path.read_text())[0]
                    self.assertIsNone(first["stream_url"])
                    second_run(config)
                sessions.assert_called_once()
                state = load_private_state(root / ".cache" / "catalog-state.json")
                self.assertEqual(
                    state.channels[0].stream_url, "https://media.example.test/live.m3u8"
                )
                self.assertEqual(state.channels[0].resolved_at, NOW.isoformat())
                metadata = json.loads(config.meta_output_path.read_text())
                self.assertEqual(metadata["contract_version"], 1)
                self.assertEqual(
                    metadata["catalog_sha256"],
                    hashlib.sha256(config.output_path.read_bytes()).hexdigest(),
                )
                self.assertNotIn("resolution_inputs", json.dumps(metadata))
                self.assertEqual(
                    metadata["diagnostics"]["resolution"], {"kept": 1, "attempted": 0}
                )

    def test_corrupt_private_state_falls_back_to_legacy_array_and_repairs_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            previous = channel(
                source_type="directory_catalog",
                source_url="https://media.example.test/live.m3u8",
                stream_url="https://media.example.test/live.m3u8",
                status="resolved",
            )
            config.output_path.write_text(json.dumps([previous.to_dict()]))
            state = root / ".cache" / "catalog-state.json"
            state.parent.mkdir()
            state.write_text('{"private-context"')
            with (
                patch(
                    "live_stream_catalog.services.refresh._load_dynamic_channels",
                    return_value=[],
                ),
                patch(
                    "live_stream_catalog.services.resolver.build_streamlink_session"
                ) as network,
                self.assertLogs(
                    "live_stream_catalog.io.catalog_state_store", level="WARNING"
                ),
            ):
                run_refresh(config)
            network.assert_not_called()
            repaired = load_private_state(state)
            self.assertEqual(repaired.channels[0].id, "sample")
            self.assertEqual(
                json.loads(config.meta_output_path.read_text())["catalog_sha256"],
                hashlib.sha256(config.output_path.read_bytes()).hexdigest(),
            )

    def test_expired_url_without_metadata_is_due_and_cannot_be_rejuvenated(self):
        observed = (NOW - timedelta(hours=2)).isoformat()
        value = channel(
            source_type="script_discovered",
            source_url="https://media.example.test/live.m3u8?expire=1",
            stream_url="https://media.example.test/live.m3u8?expire=1",
            status="resolved",
            resolved_at=observed,
        )
        with (
            patch("live_stream_catalog.services.expiry.utc_now", return_value=NOW),
            patch("live_stream_catalog.services.resolver.utc_now", return_value=NOW),
            patch(
                "live_stream_catalog.services.resolver.build_streamlink_session"
            ) as network,
        ):
            self.assertTrue(needs_refresh(value, 1800))
            resolved = resolve_channel(value)
        network.assert_not_called()
        self.assertIsNone(resolved.stream_url)
        self.assertEqual(resolved.error, "expired_transport")
        self.assertEqual(resolved.resolved_at, observed)

    def test_feed_contexts_and_declared_tags_survive_state_and_source_deduplication(
        self,
    ):
        values = [
            channel(
                feed_id=feed,
                stream_url="https://media.example.test/shared.m3u8",
                status="resolved",
                tags=["news", "regional"],
            )
            for feed in ("north", "south")
        ]
        self.assertEqual(len(_deduplicate(values)), 2)
        snapshots = [Channel.from_state_dict(item.to_state_dict()) for item in values]
        candidates = [channel(feed_id=feed) for feed in ("south", "north")]
        result = carry_previous_resolution(candidates, snapshots)
        self.assertEqual([item.feed_id for item in result], ["south", "north"])
        self.assertTrue(all(item.stream_url for item in result))
        self.assertEqual(snapshots[0].to_dict()["tags"], ["news", "regional"])

    def test_private_adapter_caches_and_checkpoint_keep_complete_request_context(self):
        value = channel(
            request_headers={"Cookie": "private-session"},
            secret_refs={"headers": "PRIVATE_HEADERS"},
            drm={"type": "declared", "token": "private-license"},
            stream_url="https://media.example.test/live.m3u8?token=private-session",
            status="resolved",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            addon = addon_catalog.AddonCatalogConfig(
                "sample",
                "sample",
                "https://addon.example.test/manifest.json",
                cache_dir=root / "addon",
            )
            addon_catalog._write_cached_channels(addon, [value])
            addon_catalog._write_checkpoint_channels(addon, {"sample": [value]})
            restored = [
                addon_catalog._load_cached_channels(addon, "best")[0][0],
                addon_catalog._load_checkpoint_channels(addon, "best")["sample"][0],
            ]
            source = directory_catalog.DirectoryCatalogConfig(
                ("sample",), cache_dir=root / "directory"
            )
            directory_catalog._write_cache(source, [value])
            restored.append(directory_catalog._read_cache(source)[0][0])
            for item in restored:
                self.assertEqual(item.request_headers["Cookie"], "private-session")
                self.assertEqual(item.drm["token"], "private-license")
                self.assertEqual(item.declared_inputs(), value.declared_inputs())
                self.assertNotIn("private-session", json.dumps(item.to_dict()))
            for path in root.rglob("*.json"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_shared_build_refresh_pipeline_preserves_feeds_and_publication_parity(self):
        def inventory():
            return [
                channel(
                    id="sample-" + feed,
                    feed_id=feed,
                    source_type="json_catalog",
                    source_url="https://media.example.test/live.m3u8",
                    stream_url="https://media.example.test/live.m3u8",
                    status="resolved",
                    tags=["news", "regional"],
                )
                for feed in ("north", "south")
            ]

        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            with (
                patch(
                    "live_stream_catalog.services.build.load_catalog",
                    side_effect=lambda **_: inventory(),
                ),
                patch(
                    "live_stream_catalog.services.refresh._load_dynamic_channels",
                    side_effect=lambda *_args, **_kwargs: inventory(),
                ),
                patch(
                    "live_stream_catalog.services.resolver.build_streamlink_session"
                ) as network,
            ):
                run_build(config)
                built = config.output_path.read_bytes()
                counts = json.loads(config.meta_output_path.read_text())[
                    "source_counts"
                ]
                run_refresh(config)
            network.assert_not_called()
            self.assertEqual(config.output_path.read_bytes(), built)
            self.assertEqual(
                json.loads(config.meta_output_path.read_text())["source_counts"], counts
            )
            self.assertEqual(len(json.loads(built)), 2)

    def test_metadata_origins_are_explicit_without_private_inputs_and_empty_source_removes_its_feeds(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            previous = [channel(id="old", source_type="json_catalog")]
            save_private_state(
                private_state_path(config.output_path), CatalogSnapshot(previous)
            )
            run = prepare_catalog_run(config)
            current = channel(
                source_type="directory_catalog",
                source_url="https://media.example.test/live.m3u8",
                stream_url="https://media.example.test/live.m3u8",
                status="resolved",
            )
            with discovery_scope(run.discovery):
                run.discovery.observe(
                    "json_catalog:empty",
                    "json_catalog",
                    [],
                    origin="fresh",
                    complete=True,
                    reason="empty",
                )
                run.discovery.observe(
                    "directory_catalog",
                    "directory_catalog",
                    [current],
                    origin="cache",
                    complete=True,
                    reason="within_ttl",
                    observed_at=NOW.isoformat(),
                )
                metadata = finish_catalog_run(run, [current])
            public = json.loads(config.output_path.read_text())
            self.assertEqual([row["id"] for row in public], ["sample"])
            sources = metadata.diagnostics["sources"]
            self.assertEqual(sources["json_catalog:empty"]["channels"], 0)
            self.assertTrue(sources["json_catalog:empty"]["complete"])
            self.assertEqual(
                sources["directory_catalog"]["observed_at"], NOW.isoformat()
            )
            self.assertEqual(
                metadata.source_counts["directory_catalog"]["public_transports"], 1
            )
            self.assertNotIn("example.test", json.dumps(metadata.to_dict()))

    def test_private_state_override_and_corrupt_declared_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "state.json"
            with patch.dict("os.environ", {"LIVE_CATALOG_STATE_FILE": str(path)}):
                self.assertEqual(
                    private_state_path(Path(directory) / "channels.json"), path
                )
            value = channel().to_state_dict()
            for invalid in (
                {"request_headers": ["bad-pair"]},
                {"publishable_static": "false"},
                {"source_type": []},
            ):
                payload = value | {
                    "resolution_inputs": value["resolution_inputs"] | invalid
                }
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({"version": 1, "channels": [payload]}))
                with self.assertLogs(
                    "live_stream_catalog.io.catalog_state_store", level="WARNING"
                ):
                    self.assertIsNone(load_private_state(path))

    def test_public_dto_rejects_private_transports_regardless_of_declared_static_flags(
        self,
    ):
        cases = (
            {"stream_url": "https://media.example.test/live.m3u8?token=private"},
            {"request_headers": {" Cookie ": "private-session"}},
            {"request_headers": {"Authorization": "private-session"}},
            {"drm": {"token": "private-license"}},
            {"secret_refs": {"headers": "PRIVATE_HEADERS"}},
        )
        for static in (True, False):
            for private in cases:
                with self.subTest(static=static, private=private):
                    value = channel(
                        stream_url="https://media.example.test/live.m3u8",
                        status="resolved",
                        publishable_static=static,
                        **{
                            key: value
                            for key, value in private.items()
                            if key != "stream_url"
                        },
                    )
                    if "stream_url" in private:
                        value.stream_url = private["stream_url"]
                    public = value.to_dict()
                    self.assertIsNone(public["stream_url"])
                    self.assertEqual(public["request_headers"], {})
                    self.assertNotIn("private-session", json.dumps(public))
        normal = channel(
            stream_url="https://media.example.test/live.m3u8",
            status="resolved",
            request_headers={
                "Referer": "https://page.example.test/",
                "User-Agent": "Sample",
            },
        )
        self.assertEqual(normal.to_dict()["request_headers"], normal.request_headers)
        self.assertEqual(normal.to_dict()["stream_url"], normal.stream_url)

    def test_static_flag_cannot_bypass_video_media_expiry_gate(self):
        for static in (True, False):
            for media in (
                "https://media.example.test/live.m3u8",
                "https://media.example.test/live.m3u8?expire=1",
            ):
                with self.subTest(static=static, media=media):
                    value = channel(
                        source_type="video_platform",
                        source_url="https://video.example.test/watch?v=abcdefghijk",
                        stream_url=media,
                        status="resolved",
                        publishable_static=static,
                    )
                    self.assertIsNone(value.to_dict()["stream_url"])

    def test_local_feed_labels_are_scoped_but_canonical_ids_preserve_cross_provider_equivalence(
        self,
    ):
        rows = [
            row_to_channel(
                {
                    "id": key,
                    "name": key,
                    "feed": "north",
                    "tags": ["regional"],
                    "stream_url": "https://media.example.test/" + key + ".m3u8",
                },
                "script_discovered",
                provider_id="provider-a",
            )
            for key in ("channel-a", "channel-b")
        ]
        self.assertNotEqual(rows[0].feed_id, rows[1].feed_id)
        self.assertEqual(rows[0].feed_id, "provider-a:channel-a:north")
        shared = [
            row_to_channel(
                {
                    "id": "channel-a",
                    "name": "Shared",
                    "feed": "north",
                    "feed_id": "shared-global-feed",
                    "stream_url": "https://media.example.test/" + provider + ".m3u8",
                },
                "script_discovered",
                provider_id=provider,
            )
            for provider in ("provider-a", "provider-b")
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            run = prepare_catalog_run(config)
            with discovery_scope(run.discovery):
                finish_catalog_run(run, rows + shared)
            public = json.loads(config.output_path.read_text())
            self.assertEqual(
                [item["feed_id"] for item in public].count("shared-global-feed"), 2
            )
            local = [item for item in public if item["feed_id"] != "shared-global-feed"]
            self.assertEqual(len({item["feed_id"] for item in local}), 2)
            self.assertTrue(all(item["tags"] == ["regional"] for item in local))

    def test_declared_expiry_cannot_be_erased_by_direct_resolution_or_transient_failures(
        self,
    ):
        observed = (NOW - timedelta(hours=2)).isoformat()
        for url in (
            "https://media.example.test/live.m3u8",
            "https://media.example.test/live.m3u8?expire=1",
        ):
            value = channel(
                source_type="script_discovered",
                stream_url=url,
                status="resolved",
                resolved_at=observed,
                expires_at=(NOW - timedelta(seconds=1)).isoformat(),
            )
            with patch(
                "live_stream_catalog.services.resolver.utc_now", return_value=NOW
            ):
                resolved = resolve_channel(value)
            self.assertIsNone(resolved.stream_url)
            self.assertEqual(resolved.resolved_at, observed)
            self.assertEqual(resolved.error, "expired_transport")
        value = channel(
            status="resolved",
            stream_url="https://media.example.test/live.m3u8?expire=1",
            expires_at=(NOW + timedelta(hours=2)).isoformat(),
            resolved_at=observed,
        )
        session = Mock()
        session.streams.side_effect = RuntimeError("429 unavailable")
        with (
            patch(
                "live_stream_catalog.services.resolver.build_streamlink_session",
                return_value=session,
            ),
            patch("live_stream_catalog.services.resolver.utc_now", return_value=NOW),
            self.assertLogs(level="WARNING"),
        ):
            resolved = resolve_channel(value)
        self.assertIsNone(resolved.stream_url)
        self.assertEqual(resolved.resolved_at, observed)


if __name__ == "__main__":
    unittest.main()
