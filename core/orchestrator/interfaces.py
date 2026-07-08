from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from core.orchestrator.models import (
    EvidencePackage,
    OrchestratorRequest,
    ResolvedIntent,
    ServiceCapability,
    ServiceDescriptor,
)


class IIntentResolver(ABC):
    @abstractmethod
    def resolve(self, query: str) -> ResolvedIntent:
        ...


class IServiceRegistry(ABC):
    @abstractmethod
    def get_all(self) -> List[ServiceDescriptor]:
        ...

    @abstractmethod
    def get_by_capability(
        self, capabilities: List[ServiceCapability]
    ) -> List[ServiceDescriptor]:
        ...

    @abstractmethod
    def get_by_id(self, service_id: str) -> ServiceDescriptor | None:
        ...


class IContextBuilder(ABC):
    @abstractmethod
    def build(
        self,
        request: OrchestratorRequest,
        intent: ResolvedIntent,
        services: List[ServiceDescriptor],
    ) -> EvidencePackage:
        ...


class IEnterpriseOrchestrator(ABC):
    @abstractmethod
    def process(self, request: OrchestratorRequest) -> EvidencePackage:
        ...
