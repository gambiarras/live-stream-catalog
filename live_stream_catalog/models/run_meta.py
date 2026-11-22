from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class RunMetadata:
    generated_at: str
    total_channels: int
    resolved_channels: int
    offline_channels: int
    removed_channels: int
    error_channels: int
    min_ttl_seconds: int | None
    max_ttl_seconds: int | None
    contract_version: int = 1
    catalog_sha256: str | None = None
    source_counts: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
