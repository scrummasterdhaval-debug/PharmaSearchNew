from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    name: str
    region: str
    countries: tuple[str, ...]
    supports_live_search: bool = True
    supports_documents: bool = False
    enabled: bool = True
    rate_limit_per_minute: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "region": self.region,
            "countries": list(self.countries),
            "supports_live_search": self.supports_live_search,
            "supports_documents": self.supports_documents,
            "enabled": self.enabled,
            "rate_limit_per_minute": self.rate_limit_per_minute,
        }


class SourceConnector(Protocol):
    metadata: SourceMetadata

    def search(self, substance: str) -> list[dict[str, Any]]:
        ...

    def fetch_details(self, product: dict[str, Any]) -> dict[str, Any]:
        return product

    def fetch_documents(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def health_check(self) -> dict[str, Any]:
        return {"source": self.metadata.name, "enabled": self.metadata.enabled}


class FunctionSourceConnector:
    def __init__(
        self,
        metadata: SourceMetadata,
        search_function: Callable[[str], list[dict[str, Any]]],
    ) -> None:
        self.metadata = metadata
        self._search_function = search_function

    def search(self, substance: str) -> list[dict[str, Any]]:
        return self._search_function(substance)

    def fetch_details(self, product: dict[str, Any]) -> dict[str, Any]:
        return product

    def fetch_documents(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def health_check(self) -> dict[str, Any]:
        return {"source": self.metadata.name, "enabled": self.metadata.enabled}


class NotImplementedSourceConnector:
    def __init__(self, metadata: SourceMetadata) -> None:
        self.metadata = metadata

    def search(self, substance: str) -> list[dict[str, Any]]:
        return []

    def fetch_details(self, product: dict[str, Any]) -> dict[str, Any]:
        return product

    def fetch_documents(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def health_check(self) -> dict[str, Any]:
        return {
            "source": self.metadata.name,
            "enabled": self.metadata.enabled,
            "status": "not_implemented",
        }
