from __future__ import annotations

from core.answering import citation_builder, explanation_builder, recommendation_builder, response_builder
from core.answering.models import EnterpriseAnswer


class AnswerPlanner:
    """
    Converts (ExecutionStrategy, EvidencePackage) into a deterministic
    EnterpriseAnswer. No SQL, no execution, no AI/LLM calls anywhere in
    this package — every fact traces back to evidence already collected
    by the Enterprise Orchestrator.
    """

    def build(self, strategy, package) -> EnterpriseAnswer:
        explanation = explanation_builder.build_explanation(strategy, package)
        citations = citation_builder.build_citations(package)
        recommendations = recommendation_builder.build_recommendations(strategy, package)
        return response_builder.build_response(strategy, package, explanation, citations, recommendations)
