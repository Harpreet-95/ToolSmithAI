"""
Connect-time wiring tests -- Phase 7, Step 3J, Part 4.

Covers data.datasource_service._run_business_knowledge_mapping_for_source:
  - a complete no-op when BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED is
    off (the default) -- the mapper module is never even imported/called
  - a no-op (never raises) when no READY retrieval build exists yet
  - calls core.sqlservice.business_knowledge_mapper.run_business_knowledge_mapping
    exactly once, with the latest READY build's id, when enabled and a
    build exists
  - never lets a mapper exception escape and fail the connect job
"""
from __future__ import annotations

from dataclasses import dataclass

import data.datasource_service as datasource_service_module


@dataclass
class _FakeBuild:
    id: int


def test_noop_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(
        "core.config.BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED", False,
    )
    calls = []
    monkeypatch.setattr(
        "core.sqlservice.retrieval_store.get_latest_ready_retrieval_build",
        lambda *a, **k: calls.append("should_not_be_called") or _FakeBuild(id=1),
    )
    datasource_service_module._run_business_knowledge_mapping_for_source(1, "alice")
    assert calls == []


def test_noop_when_no_ready_build(monkeypatch):
    monkeypatch.setattr("core.config.BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED", True)
    monkeypatch.setattr(
        "core.sqlservice.retrieval_store.get_latest_ready_retrieval_build", lambda *a, **k: None,
    )
    mapper_calls = []
    monkeypatch.setattr(
        "core.sqlservice.business_knowledge_mapper.run_business_knowledge_mapping",
        lambda *a, **k: mapper_calls.append((a, k)),
    )
    datasource_service_module._run_business_knowledge_mapping_for_source(1, "alice")
    assert mapper_calls == []


def test_calls_mapper_with_ready_build_id_when_enabled(monkeypatch):
    monkeypatch.setattr("core.config.BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED", True)
    monkeypatch.setattr(
        "core.sqlservice.retrieval_store.get_latest_ready_retrieval_build",
        lambda source_id, user_id: _FakeBuild(id=42),
    )

    class _FakeEmbeddingProvider:
        pass

    monkeypatch.setattr(
        "core.sqlservice.embedding_providers.OpenAIEmbeddingProvider", lambda: _FakeEmbeddingProvider(),
    )
    mapper_calls = []
    monkeypatch.setattr(
        "core.sqlservice.business_knowledge_mapper.run_business_knowledge_mapping",
        lambda *a, **k: mapper_calls.append(a),
    )
    datasource_service_module._run_business_knowledge_mapping_for_source(7, "alice")
    assert len(mapper_calls) == 1
    args = mapper_calls[0]
    assert args[0] == 7  # source_id
    assert args[1] == "alice"  # user_id
    assert args[2] == 42  # build.id


def test_mapper_exception_never_escapes(monkeypatch):
    monkeypatch.setattr("core.config.BUSINESS_KNOWLEDGE_MAPPING_ON_CONNECT_ENABLED", True)
    monkeypatch.setattr(
        "core.sqlservice.retrieval_store.get_latest_ready_retrieval_build",
        lambda source_id, user_id: _FakeBuild(id=1),
    )
    monkeypatch.setattr(
        "core.sqlservice.embedding_providers.OpenAIEmbeddingProvider",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Must not raise -- this is a best-effort connect-time step.
    datasource_service_module._run_business_knowledge_mapping_for_source(1, "alice")
