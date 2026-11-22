from live_stream_catalog.models import Channel, RunMetadata
from live_stream_catalog.services.expiry import utc_now


def build_run_metadata(channels: list[Channel], *, catalog_sha256: str | None = None,
                       source_counts: dict | None = None, diagnostics: dict | None = None) -> RunMetadata:
    ttl_values = [channel.ttl_seconds for channel in channels if channel.ttl_seconds is not None]

    return RunMetadata(
        generated_at=utc_now().isoformat(),
        contract_version=1, catalog_sha256=catalog_sha256,
        source_counts=source_counts or {}, diagnostics=diagnostics or {},
        total_channels=len(channels),
        resolved_channels=sum(1 for channel in channels if channel.status == "resolved"),
        offline_channels=sum(1 for channel in channels if channel.status == "offline"),
        removed_channels=sum(1 for channel in channels if channel.status == "removed"),
        error_channels=sum(1 for channel in channels if channel.status == "error"),
        min_ttl_seconds=min(ttl_values) if ttl_values else None,
        max_ttl_seconds=max(ttl_values) if ttl_values else None,
    )
