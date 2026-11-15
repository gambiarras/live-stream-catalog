import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from live_stream_catalog.models import Channel


def load_channels_file(path: Path, default_resolution: str = "best") -> list[Channel]:
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Channel.from_dict(item, default_resolution=default_resolution) for item in raw]


def write_json_atomic(path: Path, payload: list[dict] | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=path.parent) as tmp:
            temp_path = Path(tmp.name)
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
