from __future__ import annotations

import re
from dataclasses import dataclass, field

from data.query_execution_service import _has_write_keywords, _strip_quoted_identifiers

# Statement types this engine allows unconditionally, keyed by their leading
# keyword after stripping whitespace and uppercasing.
_ALLOWED_PREFIXES: tuple[str, ...] = ("SELECT", "WITH", "DESCRIBE", "DESC", "EXPLAIN")

# SHOW is dialect-specific: MySQL/PostgreSQL support it, SQL Server/Oracle do not.
_SHOW_DIALECTS: frozenset[str] = frozenset({"mysql", "postgresql"})

# Comment syntax could hide a blocked keyword from the write-keyword scan —
# reject outright rather than attempting to safely strip comments.
_COMMENT_RE = re.compile(r"--|/\*")

# Not covered by data.query_execution_service's write-keyword regex, which
# was written for the generate_sql pipeline that never produces these.
_CALL_RE = re.compile(r"\bCALL\b", re.IGNORECASE)
_STORED_PROC_RE = re.compile(r"\b(sp_|xp_)\w+", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    blocking_reasons: list[str] = field(default_factory=list)


def validate(sql: "str | None", dialect: str) -> ValidationResult:
    """Accept only read-only SQL. Never raises — every outcome is a ValidationResult."""
    if not sql or not sql.strip():
        return ValidationResult(is_valid=False, blocking_reasons=["SQL is empty."])

    reasons: list[str] = []

    if _COMMENT_RE.search(sql):
        reasons.append("SQL comments are not permitted.")

    if ";" in sql:
        reasons.append("Multiple statements are not permitted.")

    upper_tokens = sql.strip().upper().split()
    first_word = upper_tokens[0] if upper_tokens else ""
    allowed = list(_ALLOWED_PREFIXES)
    if dialect in _SHOW_DIALECTS:
        allowed.append("SHOW")
    if first_word not in allowed:
        reasons.append(
            f"Statement type '{first_word}' is not permitted. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )

    cleaned = _strip_quoted_identifiers(sql)
    if _has_write_keywords(sql):
        reasons.append("SQL contains a write or DDL keyword outside a quoted identifier.")
    if _CALL_RE.search(cleaned):
        reasons.append("CALL statements are not permitted.")
    if _STORED_PROC_RE.search(cleaned):
        reasons.append("Stored procedure execution is not permitted.")

    return ValidationResult(is_valid=not reasons, blocking_reasons=reasons)
