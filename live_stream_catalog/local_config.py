"""Private deployment inputs kept outside the distributable package."""

import importlib
import json
import os
from importlib import resources
from pathlib import Path
from typing import Any


def local_config_dir() -> Path:
    """Resolve the component configuration directory supplied by operations."""
    configured = os.environ.get("IPTV_LOCAL_CONFIG_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[1] / ".local"


def runtime_settings() -> dict[str, Any]:
    """Read deployment endpoints without placing their values in public logs."""
    path = local_config_dir() / "runtime.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Invalid local catalog runtime configuration") from exc
    if not isinstance(value, dict):
        raise ValueError("Invalid local catalog runtime configuration")
    return value


def source_settings(role: str) -> dict[str, Any]:
    value = runtime_settings().get(role, {})
    return value if isinstance(value, dict) else {}


def resource_json(name: str) -> Any:
    """Use private reviewed data when present; packaged resources are empty skeletons."""
    path = local_config_dir() / "resources" / name
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    resource = resources.files("live_stream_catalog.resources").joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8")) if resource.is_file() else []


def require_local_configuration() -> None:
    """Fail before generation when reviewed private inputs have not been installed."""
    if runtime_settings().get("enabled") is not True:
        raise RuntimeError("Local catalog configuration is required; preserve the last publication")
    folder = local_config_dir() / "resources"
    if not folder.is_dir() or not any(folder.glob("*.json")):
        raise RuntimeError("Local catalog source resources are missing; preserve the last publication")


def discovery_client_factory():
    """Load the operator-selected library client through its local dotted path."""
    dotted = source_settings("video_platform").get("discovery_factory")
    if not isinstance(dotted, str) or "." not in dotted:
        raise RuntimeError("Local discovery client factory is required")
    module, name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


def source_alias(value: str | None, *, provider: bool = False) -> str | None:
    """Keep prior artifact identities usable while public code uses generic roles."""
    field = "provider_aliases" if provider else "source_type_aliases"
    aliases = runtime_settings().get(field, {})
    return aliases.get(value, value) if isinstance(aliases, dict) else value
