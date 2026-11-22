"""Complete private resolver state, deliberately separate from the public channel array."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from live_stream_catalog.io.json_store import write_json_atomic
from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import utc_now

logger = logging.getLogger(__name__)
STATE_VERSION = 1
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_CHANNELS = 20_000


@dataclass(slots=True)
class CatalogSnapshot:
    channels: list[Channel]
    discovery: dict[str, Any] = field(default_factory=dict)


def private_state_path(output: Path) -> Path:
    configured = os.environ.get("LIVE_CATALOG_STATE_FILE")
    return (
        Path(configured)
        if configured
        else output.parent / ".cache" / "catalog-state.json"
    )


def load_private_state(
    path: Path, *, default_resolution: str = "best"
) -> CatalogSnapshot | None:
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.stat().st_size > MAX_STATE_BYTES:
            raise ValueError("Invalid private state file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise ValueError("Unknown state version")
        rows = payload["channels"]
        if not isinstance(rows, list) or len(rows) > MAX_CHANNELS:
            raise ValueError("Invalid state channel inventory")
        channels = [
            Channel.from_state_dict(item, default_resolution=default_resolution)
            for item in rows
        ]
        observations = payload.get("discovery", {})
        if not isinstance(observations, dict):
            raise TypeError("Invalid state discovery inventory")
        return CatalogSnapshot(channels, observations)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        logger.warning(
            "Ignoring invalid private catalog state; using the compatible public snapshot"
        )
        return None


def save_private_state(path: Path, snapshot: CatalogSnapshot) -> None:
    payload = {
        "version": STATE_VERSION,
        "saved_at": utc_now().isoformat(),
        "channels": [channel.to_state_dict() for channel in snapshot.channels],
        "discovery": snapshot.discovery,
    }
    if (
        len(snapshot.channels) > MAX_CHANNELS
        or len(json.dumps(payload).encode()) > MAX_STATE_BYTES
    ):
        raise ValueError("Private catalog state exceeds its configured safety limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_atomic(path, payload)
    path.chmod(0o600)
