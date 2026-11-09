"""Shared fixtures for AI physical-planning tests.

This module is deliberately not named ``test_*`` so pytest does not collect it
as a standalone test file.
"""

from core.semantic.candidate_adjudicator import AdjudicationResult
from tests.test_phase9_query_planning import env, _add_column, _add_fk, _add_table


def _mock_adjudication(table_fqn: str) -> AdjudicationResult:
    """Return the minimal successful adjudication used by planner tests."""

    return AdjudicationResult(
        selected_table_fqn=table_fqn,
        no_safe_selection=False,
        confidence=0.95,
        evidence_references=("test fixture",),
    )


__all__ = ["env", "_add_column", "_add_fk", "_add_table", "_mock_adjudication"]
