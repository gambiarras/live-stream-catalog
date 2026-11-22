"""A detached public DTO; private resolver state never participates in its serialization."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class PublicChannelDTO:
    fields: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PublicChannelDTO":
        return cls(MappingProxyType(deepcopy(payload)))

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(dict(self.fields))
