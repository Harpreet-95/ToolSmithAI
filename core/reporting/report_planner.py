"""
Executive Report Query Foundation milestone, Parts 7/8 — bounded, evidence-
grounded executive report planning.

Flow (Part 8, non-negotiable):
    report intent
    -> bounded metadata evidence (data.business_knowledge_service.
       get_source_metric_evidence — approved is_metric/is_dimension/is_date
       columns only, capped tables/columns)
    -> AI proposes KPIQuerySpecs grounded in that evidence
    -> (core.reporting.report_orchestrator independently plans/validates/
        executes each one — this module never executes anything itself)

This module NEVER lets the AI invent a table/column name that becomes SQL:
every KPIQuerySpec produced here carries only BUSINESS-LANGUAGE terms
(entity_name/measure terms exactly like an ordinary SQL Q&A question would
use), which core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan
re-resolves against real evidence independently, from scratch, with its own
adjudication/validation — identical to how an ordinary typed question is
never trusted to name a physical identifier directly. A cheap lexical
grounding check (_kpi_is_grounded) additionally rejects an AI proposal that
doesn't even lexically relate to anything in the evidence it was given,
before a planning call is even attempted — defense in depth, not the primary
safety mechanism (that is attempt_structured_kpi_plan's own validation).

Nothing here is database-specific: every default derives from whatever
get_source_metric_evidence discovers for THIS source, so the same code
proposes different KPIs for an e-commerce schema, a logistics schema, etc.
(Part 13).
"""
from __future__ import annotations

import json
import logging
import re

from core.reporting.kpi_spec import ComparisonRequest, KPIMeasure, KPIQuerySpec

logger = logging.getLogger(__name__)

_MAX_KPIS = 6

_PLANNER_SYSTEM_PROMPT = """You propose a SMALL set of executive-report KPIs for a business database.

You will be given a bounded list of candidate tables, each with: its business
name/domain/entity, a deterministic authority_score (0-0.5, already computed
from real schema evidence — relationship centrality, row-count, root-table
structure, naming-authority exclusions for temp/backup/archive tables — a
HIGHER score means more structurally central, NOT more business-relevant by
itself), whether its dictionary is human-approved (informational only, never
a requirement), and its APPROVED-OR-NOT metric (numeric), dimension
(categorical), and date columns actually available.

DOMAIN-CENTRALITY JUDGMENT (this is where YOUR reasoning matters — the
authority_score alone cannot tell this apart):
- Some candidate domains represent this business's OWN core operations (what
  the organization actually does for its customers/members/students/clients
  — enrollment, orders, shipments, claims, cases, whatever this schema's
  real subject matter is).
- Other candidate domains are GENERIC technical/platform/identity/system
  infrastructure that exists in almost any application regardless of
  business type (user authentication tables, CMS/portal scaffolding, generic
  "Users"/"Roles"/system-log tables unrelated to the business's own
  activity) — these should be STRONGLY DEPRIORITIZED for executive KPIs even
  when their authority_score is high, unless the evidence gives no better
  alternative at all.
- Prefer DIVERSITY across genuinely different business concepts (e.g.
  primary population/entity, operational activity/event, a financial
  measure, a status/health signal, a time trend) over multiple KPIs drawn
  from the same one table or the same narrow subsystem — do not propose 5
  KPIs that are all really the same underlying count with different titles.

STRICT rules:
- Propose AT MOST 6 KPIs.
- Every entity_name and every measure term you use MUST be one of the exact
  business_name/business_label/column_name values given to you, or an
  obvious business synonym of one (e.g. "revenue" for a column labeled
  "Total Revenue"). NEVER invent a metric, table, or column that isn't
  represented in the evidence.
- If the evidence contains no metric columns at all, propose COUNT-style KPIs
  only (how many of each entity).
- Only propose a trend KPI (calendar_grain) when a date column exists on the
  SAME table as the measure you are aggregating.
- Only propose a "comparison" when it is a genuinely time-based measure
  (has a date column) - comparison_type must be one of
  "month_over_month", "quarter_over_quarter", "year_over_year".
- aggregation must be one of: COUNT, DISTINCT_COUNT, SUM, AVG, MIN, MAX.
- visualization_hint must be one of: kpi_card, line_chart, bar_chart, table.
- Vary the KPIs: not every KPI needs a comparison or a trend; propose a mix
  of simple counts, aggregates, and (only where evidence supports it) one
  trend and one comparison.
- For every KPI, set "domain" to the exact domain value of the table it
  measures, so proposals can be checked for real diversity — never omit it.
- TERMINOLOGY: never call a measure "Revenue"/"Profit" unless a column's own
  business_label literally already says so — a payment/fee/amount column is
  "Payments"/"Fees Paid"/"Amount", never inflated to "Revenue" or "Profit"
  (there is a real difference: revenue/profit imply accounting concepts this
  evidence may not actually support). Likewise never call a signup/
  registration a "Customer", or a count a rate/percentage it doesn't compute.
  This applies to the "reason" text too, not just the title.
- Do not propose a KPI whose entity is a generic user-authentication/login/
  session table (the kind of table that logs WHO accessed the system,
  not what the business itself does) unless the evidence contains no
  better business-operational alternative at all.

Return ONLY a JSON object: {"kpis": [ { "title": str, "business_question": str,
"entity_name": str, "domain": str, "measures": [{"term": str, "aggregation": str}],
"calendar_grain": str | null, "dimension_terms": [str, ...],
"comparison_type": str | null, "visualization_hint": str,
"importance": "high"|"medium"|"low", "reason": str } ] }. No markdown, no
explanation outside the JSON.
"""

_CALENDAR_GRAINS = frozenset({"year", "month", "quarter", "week", "day"})
_AGGREGATIONS = frozenset({"COUNT", "DISTINCT_COUNT", "SUM", "AVG", "MIN", "MAX"})
_VISUAL_HINTS = frozenset({"kpi_card", "line_chart", "bar_chart", "table"})
_COMPARISON_TYPES = frozenset({"month_over_month", "quarter_over_quarter", "year_over_year"})

# FINAL PRE-37 MILESTONE, Part 22/23 — hard terminology rule, enforced
# DETERMINISTICALLY rather than by prompt compliance alone (an LLM is not
# 100% reliable at following a "never say X" instruction — live-confirmed
# the word "revenue" still appeared in a proposed KPI's own reason text
# even after the system prompt explicitly forbade it). A KPI whose title OR
# reason uses one of these words is rejected outright UNLESS the underlying
# evidence itself already uses that exact word in a business_label/
# business_name (i.e. a human already approved that terminology for this
# specific column, not the model inventing it) — checked by
# _kpi_uses_unsupported_financial_terms against the same evidence_terms
# _kpi_is_grounded already uses.
_UNSUPPORTED_FINANCIAL_TERMS = frozenset({"revenue", "profit", "profitability"})


def _evidence_terms(evidence: list[dict]) -> set[str]:
    terms: set[str] = set()
    for table in evidence:
        for key in ("business_name", "domain", "entity", "table_fqn"):
            v = table.get(key)
            if v:
                terms.add(str(v).lower())
        for bucket in ("metric_columns", "dimension_columns", "date_columns"):
            for col in table.get(bucket) or []:
                terms.add(str(col.get("column_name") or "").lower())
                if col.get("business_label"):
                    terms.add(str(col["business_label"]).lower())
    return terms


def _term_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _kpi_uses_unsupported_financial_terms(spec_dict: dict, evidence_terms: set[str]) -> bool:
    """True when the proposal's title or reason text uses "revenue"/
    "profit"/"profitability" and no evidence business_label/business_name
    anywhere in the candidate pool already uses that exact word — i.e. the
    model inflated plain payment/fee/amount evidence into an accounting
    term the schema never actually claims. See _UNSUPPORTED_FINANCIAL_TERMS
    for why this is enforced here, deterministically, rather than trusted
    to the system prompt alone."""
    text = f"{spec_dict.get('title') or ''} {spec_dict.get('reason') or ''}".lower()
    used = {t for t in _UNSUPPORTED_FINANCIAL_TERMS if t in text}
    if not used:
        return False
    evidence_tokens: set[str] = set()
    for term in evidence_terms:
        evidence_tokens |= _term_tokens(term)
    return not (used & evidence_tokens)


# FINAL EXECUTIVE-REPORT BLOCKER FIX milestone, Part 7/8/9 — deterministic
# infrastructure/session-telemetry backstop. Live-confirmed against real
# the reference database: dbo.EDU_UserLogon and dbo.EDU_UserLegacyLogin (entity=User, zero
# metric columns, zero dimension columns, a single Logon/date column — a
# bare login-event stub, not a described business entity) surfaced as
# executive KPI candidates ("Count of Users Logged In") purely because their
# authority_score (root table, real row count, no temp/backup naming) looks
# structurally identical to a genuine business table's to the deterministic
# scorer — exactly the gap _PLANNER_SYSTEM_PROMPT's own "STRONGLY
# DEPRIORITIZED... even when their authority_score is high" instruction
# warns about, but prompt compliance alone is not reliably followed (Part
# 10). This is a generic evidence COMBINATION, never a table-name/title
# blacklist: a table is excluded from the KPI candidate pool only when BOTH
#   (a) it is structurally thin — no metric columns AND at most one
#       dimension column, the shape of a bare event/log stub rather than a
#       described business entity (a genuine business record almost always
#       carries several descriptive dimension columns), and
#   (b) its own name/entity/date-column vocabulary lexically matches a
#       generic auth/session/system-telemetry token set
# are true together (Part 11, overfilter protection): a thin table with no
# auth-shaped name (e.g. a narrow join/bridge table) is untouched, and a
# richly-described table that happens to mention "login" in one column
# (e.g. a security product's own core "Login Events" business table, which
# would carry real outcome/method/geo dimension columns) is also untouched
# — "user"/"login" alone is never sufficient, matching Part 11's explicit
# SaaS/cybersecurity/HR counter-examples (see tests).
_INFRASTRUCTURE_SEMANTIC_TOKENS = frozenset({
    "login", "logon", "logoff", "logout", "session", "auth", "authentication",
    "authenticate", "credential", "credentials", "password", "token", "audit",
    "tracking", "cache", "pageview", "impression", "clickstream",
})
_INFRA_MAX_DIMENSION_COLUMNS = 1


def _table_lexical_tokens(table: dict) -> set[str]:
    """CamelCase/snake_case-aware tokens for TABLE's own name, entity, and
    every metric/dimension/date column's name+business_label — reuses
    core.dictionary.rule_classifier._tokenize (via its data.query_planning_
    service re-export, the same tokenizer core.orchestrator.ai_physical_
    planning already relies on throughout) rather than report_planner's own
    plain-regex _term_tokens, so "EDU_UserLogon"/"LogonDate" correctly
    tokenize to {"logon", ...} instead of one opaque lowercased blob."""
    from data.query_planning_service import _tokenize

    text_bits = [table.get("table_fqn") or "", table.get("business_name") or "", table.get("entity") or ""]
    for bucket in ("metric_columns", "dimension_columns", "date_columns"):
        for col in table.get(bucket) or []:
            text_bits.append(col.get("column_name") or "")
            text_bits.append(col.get("business_label") or "")
    tokens: set[str] = set()
    for text in text_bits:
        tokens |= set(_tokenize(text))
    return tokens


def _is_low_relevance_infrastructure_table(table: dict) -> bool:
    """True when TABLE looks like generic platform/auth/session/system
    telemetry rather than this source's own central business activity — see
    the module-level comment above for the full rationale and the two
    conjunctive conditions this checks."""
    if table.get("metric_columns"):
        return False
    if len(table.get("dimension_columns") or []) > _INFRA_MAX_DIMENSION_COLUMNS:
        return False
    return bool(_table_lexical_tokens(table) & _INFRASTRUCTURE_SEMANTIC_TOKENS)


def _kpi_is_grounded(spec_dict: dict, evidence_terms: set[str]) -> bool:
    """Cheap lexical grounding pre-filter — rejects a KPI proposal whose
    entity_name and every measure term share no token at all with anything
    in the evidence it was given. Defense in depth only: the real safety
    gate is attempt_structured_kpi_plan's own real-evidence resolution,
    which independently re-derives every table/column and safely refuses
    an unresolvable term regardless of what this check lets through."""
    evidence_tokens: set[str] = set()
    for term in evidence_terms:
        evidence_tokens |= _term_tokens(term)

    entity_tokens = _term_tokens(spec_dict.get("entity_name") or "")
    if entity_tokens and not (entity_tokens & evidence_tokens):
        return False

    for measure in spec_dict.get("measures") or []:
        m_tokens = _term_tokens(measure.get("term") or "")
        if m_tokens and not (m_tokens & evidence_tokens):
            return False

    return True


# FINAL ENTITY/TABLE ADJUDICATION CORRECTNESS FIX milestone, Part 1-4 —
# live-confirmed root cause of the "Count of Alumni Referral Contacts" =
# 41,000 defect. Reproduced directly against a real production database: candidate_adjudicator
# was NEVER the problem — dnnuser.Users isn't even in the candidate pool
# gather_candidate_evidence returns for entity_name="alumni referral
# contact" (dbo.EDU_ReferralContacts wins that pool with score=1.0 vs
# the next candidate's 0.87, and adjudicate_candidates selected it 5/5
# times in repeated live runs). The actual defect is entirely upstream, in
# THIS module's own KPI-proposal AI call: it wrote a specific, correct-
# sounding title ("Alumni Referral Contacts") while independently setting
# entity_name (and its measure term) to "User" — the GENERIC entity-
# classification taxonomy label the evidence attaches to dozens of
# unrelated person-shaped tables (dnnuser.Users, aspnet_Users,
# EDU_UserLogon, EDU_ReferralUsers, ...), rather than that specific table's
# own business_name/table identity. attempt_structured_kpi_plan then
# correctly (and unavoidably) resolves entity_name="User" to whatever this
# source's single strongest, most-authoritative literal "User" table is
# (dnnuser.Users) — a CORRECT adjudication for a WRONG input, not an
# adjudication bug.
_KPI_TITLE_BOILERPLATE_TOKENS = frozenset({
    "count", "of", "total", "sum", "average", "avg", "number", "amount",
    "the", "a", "an", "and", "by", "for", "in", "distinct",
})


def _kpi_title_entity_mismatch(spec_dict: dict) -> bool:
    """True when the proposal's own TITLE names a concept its own
    resolution keys (entity_name + every measure term + every dimension
    term) never mention at all — the exact shape of the Alumni Referral
    Contacts defect above. Deliberately NOT a lexical-similarity table
    selector (never touches candidate_adjudicator or picks a table itself):
    a pure internal-consistency check between two fields the SAME AI call
    authored, using the same near-match tokenizer (data.query_planning_
    service._tokens_near_match, singular/plural-aware, e.g. "payments" ~
    "payment") this codebase already trusts elsewhere for this class of
    comparison. A KPI whose title and entity_name genuinely agree (e.g.
    "Count of Users" / entity_name="User") is untouched — this only fires
    when the title's own distinctive words (aggregation/article boilerplate
    stripped) share NOT ONE near-match with entity_name, any measure term,
    or any dimension term."""
    from data.query_planning_service import _tokens_near_match

    title_tokens = {
        t for t in _term_tokens(spec_dict.get("title") or "")
        if t not in _KPI_TITLE_BOILERPLATE_TOKENS
    }
    if not title_tokens:
        return False  # nothing distinctive in the title to check -- defer to other guards

    grounding_tokens: set[str] = set(_term_tokens(spec_dict.get("entity_name") or ""))
    for measure in spec_dict.get("measures") or []:
        grounding_tokens |= _term_tokens((measure or {}).get("term") or "")
    for dim in spec_dict.get("dimension_terms") or []:
        grounding_tokens |= _term_tokens(dim)

    for tt in title_tokens:
        for gt in grounding_tokens:
            if _tokens_near_match(tt, gt):
                return False

    return True


# ---------------------------------------------------------------------------
# NEXT MILESTONE, Part 19 — Executive Report semantic-title audit.
#
# _kpi_title_entity_mismatch (above) already catches a title naming a
# different ENTITY than its own grounding — but says nothing about a
# STATUS/temporal QUALIFIER word in the title ("Active Users") that the
# KPI's own business_question never actually asks for. Live-confirmed class
# of defect: report_planner's single AI call authors title and
# business_question together but is not guaranteed to keep them consistent
# — a title claiming "Active" whose business_question is merely "How many
# users are there?" executes with NO status filter at all (core.orchestrator.
# ai_physical_planning/data.query_planning_service never see the word
# "active" anywhere in the text they actually resolve), silently mislabeling
# a plain total as if it were filtered.
#
# Reuses core.semantic.concept_resolver.extract_query_intent — the exact
# same deterministic status-word detector data.query_planning_service
# itself uses to decide whether a status filter will be applied — rather
# than a second, hand-rolled status-word list. If the TITLE's own text would
# resolve a status_value that the BUSINESS_QUESTION's text does not also
# resolve (same value), the title is asserting a filter nothing downstream
# will ever apply.
# ---------------------------------------------------------------------------

def _kpi_title_status_word_unsupported(spec_dict: dict) -> str | None:
    """Returns the offending status word (e.g. "Active") when the KPI's
    title claims a status/temporal condition its own business_question text
    does not also carry — else None. A title with no status word at all
    (the common case) always returns None, untouched by this check."""
    from core.semantic.concept_resolver import extract_query_intent

    title = str(spec_dict.get("title") or "")
    business_question = str(spec_dict.get("business_question") or "")
    title_status = extract_query_intent(title).get("status_value")
    if not title_status:
        return None
    question_status = extract_query_intent(business_question).get("status_value")
    if question_status and question_status.lower() == title_status.lower():
        return None
    return title_status


# ---------------------------------------------------------------------------
# FINAL CORRECTNESS CLEANUP MILESTONE, Parts 1-3 — post-execution title
# semantic-consistency validator.
#
# _kpi_title_status_word_unsupported (above) only compares TITLE text
# against the KPI's own proposed business_question TEXT — both authored by
# the same AI call, so they usually agree even when NEITHER is actually
# backed by the real, executed plan. Live-confirmed real defect: "Total
# Payments Due" and its business_question ("What is the total amount due
# for payments?") both say "due", so that check passes, yet the ACTUAL
# resolved SQL is a bare SUM(Amount) with no filter — ACHTransaction has no
# due/paid/status column at all. This validator runs AFTER a KPI/chart's
# real physical plan is known (report_orchestrator._execute_single_spec,
# once sql_plan/query_plan are resolved) and checks the title's own material
# modifier words against that REAL evidence — filters actually applied,
# columns actually selected/grouped, and their temporal/lifecycle
# implication — never against another piece of AI-authored text.
#
# Not a database-specific mapping: every word below is common, cross-domain
# business vocabulary, and _MODIFIER_EVIDENCE_TOKENS lists synonyms/roots,
# never a table or column name.
# ---------------------------------------------------------------------------

_MATERIAL_TITLE_MODIFIERS: dict[str, frozenset[str]] = {
    "active":      frozenset({"active", "isactive", "status", "enabled"}),
    "current":     frozenset({"active", "isactive", "current", "status"}),
    "currently":   frozenset({"active", "isactive", "current", "status"}),
    "due":         frozenset({"due", "outstanding", "overdue", "unpaid", "balance", "owed"}),
    "overdue":     frozenset({"due", "outstanding", "overdue", "unpaid", "balance", "owed"}),
    "outstanding": frozenset({"due", "outstanding", "overdue", "unpaid", "balance", "owed"}),
    "paid":        frozenset({"paid", "payment", "settled", "status"}),
    "unpaid":      frozenset({"due", "outstanding", "unpaid", "balance"}),
    "completed":   frozenset({"complete", "completed", "completion", "end", "close", "closed", "status"}),
    "complete":    frozenset({"complete", "completed", "completion", "end", "close", "closed", "status"}),
    "cancelled":   frozenset({"cancel", "cancelled", "canceled", "status"}),
    "canceled":    frozenset({"cancel", "cancelled", "canceled", "status"}),
    "created":     frozenset({"insert", "create", "created", "register", "registered", "add", "added"}),
    "new":         frozenset({"insert", "create", "created", "register", "registered", "add", "added", "new", "signup"}),
    "open":        frozenset({"open", "active", "status"}),
    "closed":      frozenset({"closed", "close", "end", "complete", "status"}),
    "status":      frozenset({"status", "state", "flag"}),
}


def _plan_evidence_tokens(sql_plan: dict) -> set[str]:
    """Every token this plan's ACTUAL where/select/group_by columns (and
    any literal string filter values) carry — the real semantic support a
    title's material modifiers must be checked against, never the title
    or business_question text."""
    from core.dictionary.rule_classifier import _tokenize

    tokens: set[str] = set()
    for row in (sql_plan.get("where") or []):
        tokens |= set(_tokenize(row.get("column_name") or ""))
        value = row.get("value")
        if isinstance(value, str):
            tokens |= set(_tokenize(value))
    for row in (sql_plan.get("select") or []):
        tokens |= set(_tokenize(row.get("column_name") or ""))
    for row in (sql_plan.get("group_by") or []):
        tokens |= set(_tokenize(row.get("column_name") or ""))
    return tokens


def find_unsupported_title_modifiers(title: str, sql_plan: dict) -> list[str]:
    """Returns every material modifier word literally present in `title`
    that has no semantic support in the plan's own real, executed
    filters/columns (Part 2: "reason from semantic support, not merely
    keyword presence" — a modifier is trusted only when a matching
    evidence token from _MATERIAL_TITLE_MODIFIERS' own synonym set is
    found among the plan's real where/select/group_by column tokens).
    Empty list when the title carries no material modifier at all (the
    common case)."""
    from core.dictionary.rule_classifier import _tokenize

    title_tokens = set(_tokenize(title))
    present = [w for w in _MATERIAL_TITLE_MODIFIERS if w in title_tokens]
    if not present:
        return []
    evidence_tokens = _plan_evidence_tokens(sql_plan)
    return [w for w in present if not (_MATERIAL_TITLE_MODIFIERS[w] & evidence_tokens)]


def repair_unsupported_title(title: str, unsupported_words: list[str], fallback: str) -> str:
    """Conservative repair (Part 3): strip the unsupported word(s) from the
    title text, never invent a replacement business concept. Falls back to
    `fallback` (caller-supplied, e.g. "Total {entity_name}"/"{measure} by
    {dimension}") only when stripping leaves nothing meaningful."""
    import re as _re

    corrected = title
    for word in unsupported_words:
        corrected = _re.sub(rf"\b{_re.escape(word)}\b", "", corrected, flags=_re.IGNORECASE)
    corrected = _re.sub(r"\s{2,}", " ", corrected).strip()
    return corrected or fallback


def _parse_kpi_dict(raw: dict, idx: int) -> KPIQuerySpec | None:
    title = str(raw.get("title") or "").strip()
    business_question = str(raw.get("business_question") or "").strip()
    entity_name = str(raw.get("entity_name") or "").strip()
    measures_raw = raw.get("measures") or []
    if not title or not business_question or not entity_name or not measures_raw:
        return None

    measures: list[KPIMeasure] = []
    for m in measures_raw:
        term = str((m or {}).get("term") or "").strip()
        aggregation = str((m or {}).get("aggregation") or "").strip().upper()
        if not term or aggregation not in _AGGREGATIONS:
            return None
        measures.append(KPIMeasure(term=term, aggregation=aggregation))  # type: ignore[arg-type]

    calendar_grain = raw.get("calendar_grain")
    calendar_grain = calendar_grain.lower() if isinstance(calendar_grain, str) and calendar_grain.lower() in _CALENDAR_GRAINS else None

    dimension_terms = tuple(
        str(d).strip() for d in (raw.get("dimension_terms") or []) if str(d).strip()
    )[:3]

    comparison = None
    comparison_type = raw.get("comparison_type")
    if isinstance(comparison_type, str) and comparison_type in _COMPARISON_TYPES:
        comparison = ComparisonRequest(comparison_type=comparison_type)  # type: ignore[arg-type]

    visualization_hint = raw.get("visualization_hint")
    if visualization_hint not in _VISUAL_HINTS:
        visualization_hint = "kpi_card"

    importance = raw.get("importance")
    if importance not in ("high", "medium", "low"):
        importance = "medium"

    return KPIQuerySpec(
        id=f"kpi_{idx}", title=title[:120], business_question=business_question[:300],
        entity_name=entity_name, measures=tuple(measures), calendar_grain=calendar_grain,
        dimension_terms=dimension_terms, comparison=comparison,
        visualization_hint=visualization_hint, importance=importance,
        reason=str(raw.get("reason") or "")[:300],
    )


def _ai_propose_kpis(evidence: list[dict]) -> list[dict] | None:
    try:
        from core.config import ENABLE_AI_REPORT_NARRATIVE, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS
    except Exception:
        return None
    if not ENABLE_AI_REPORT_NARRATIVE or not OPENAI_API_KEY:
        return None
    try:
        import openai as _openai
    except ImportError:
        return None

    context = json.dumps({"tables": evidence}, default=str)
    try:
        client = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            max_tokens=1200,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = json.loads((response.choices[0].message.content or "").strip())
        kpis = raw.get("kpis")
        return kpis if isinstance(kpis, list) else None
    except Exception as exc:
        logger.warning("[report_planner] AI proposal failed (%s: %s); using deterministic fallback", type(exc).__name__, exc)
        return None


def _deterministic_fallback_kpis(evidence: list[dict]) -> list[dict]:
    """No AI available/enabled: propose a small, generic set of KPIs
    directly from the evidence's own shape — never database-specific, never
    hardcoded table/column names, since it only ever reads what
    get_source_metric_evidence discovered for THIS source."""
    if not evidence:
        return []

    top = evidence[0]
    entity_name = top.get("business_name") or top.get("domain") or top.get("table_fqn")
    kpis: list[dict] = []

    kpis.append({
        "title": f"Total {entity_name}", "business_question": f"How many {entity_name} are there?",
        "entity_name": entity_name, "measures": [{"term": entity_name, "aggregation": "COUNT"}],
        "visualization_hint": "kpi_card", "importance": "high", "reason": "Baseline volume KPI.",
    })

    metrics = top.get("metric_columns") or []
    dates = top.get("date_columns") or []
    dims = top.get("dimension_columns") or []

    for metric in metrics[:2]:
        term = metric.get("business_label") or metric.get("column_name")
        kpis.append({
            "title": f"Total {term}", "business_question": f"What is the total {term}?",
            "entity_name": entity_name, "measures": [{"term": term, "aggregation": "SUM"}],
            "visualization_hint": "kpi_card", "importance": "high", "reason": "Primary financial/volume metric.",
        })

    if metrics and dates:
        metric_term = metrics[0].get("business_label") or metrics[0].get("column_name")
        kpis.append({
            "title": f"{metric_term} by Month", "business_question": f"What is total {metric_term} by month?",
            "entity_name": entity_name, "measures": [{"term": metric_term, "aggregation": "SUM"}],
            "calendar_grain": "month", "visualization_hint": "line_chart",
            "importance": "medium", "reason": "Trend over time.",
        })

    if metrics and dims:
        metric_term = metrics[0].get("business_label") or metrics[0].get("column_name")
        dim_term = dims[0].get("business_label") or dims[0].get("column_name")
        kpis.append({
            "title": f"{metric_term} by {dim_term}", "business_question": f"What is total {metric_term} by {dim_term}?",
            "entity_name": entity_name, "measures": [{"term": metric_term, "aggregation": "SUM"}],
            "dimension_terms": [dim_term], "visualization_hint": "bar_chart",
            "importance": "medium", "reason": "Categorical breakdown.",
        })

    return kpis[:_MAX_KPIS]


def _kpi_dedup_key(spec: KPIQuerySpec) -> tuple:
    """FINAL PRE-37 MILESTONE, Part 19 — two KPI proposals that represent
    the SAME underlying business measurement (same entity, same measure
    terms+aggregations, same grouping/grain/date scope) must collapse to
    one, regardless of how differently their AI-generated titles read (the
    live-confirmed real duplicate: "Count of Alumni Referral Contacts" vs
    "Count of Users in Alumni Referral Contacts" — different titles, same
    physical/business meaning). Deliberately keyed on MEANING (entity +
    measures + aggregations + dimension/grain scope), never on `title`
    text."""
    measures_key = tuple(sorted(
        (m.term.strip().lower(), m.aggregation) for m in spec.measures
    ))
    dims_key = tuple(sorted(d.strip().lower() for d in spec.dimension_terms))
    return (
        spec.entity_name.strip().lower(), measures_key, spec.calendar_grain,
        dims_key, spec.comparison.comparison_type if spec.comparison else None,
    )


def _dedupe_kpis(specs: list[KPIQuerySpec]) -> tuple[list[KPIQuerySpec], list[str]]:
    """Keeps the first (highest-priority, since callers append in the AI's/
    fallback's own proposed order) spec for each _kpi_dedup_key — prefers
    the stronger business-relevant proposal over a later semantic
    duplicate, never merges or picks by title text."""
    seen: dict[tuple, KPIQuerySpec] = {}
    warnings: list[str] = []
    for spec in specs:
        key = _kpi_dedup_key(spec)
        if key in seen:
            warnings.append(
                f"Proposed KPI '{spec.title}' merged: semantically equivalent to "
                f"'{seen[key].title}' (same entity/measure/scope)."
            )
            continue
        seen[key] = spec
    return list(seen.values()), warnings


def plan_executive_report_kpis(source_id: int, user_id: str, *, max_kpis: int = _MAX_KPIS) -> tuple[list[KPIQuerySpec], list[str]]:
    """Returns (kpi_specs, warnings). kpi_specs is bounded to max_kpis and
    contains ONLY specs that passed the lexical grounding pre-filter — every
    one still independently goes through attempt_structured_kpi_plan later
    and may still be rejected there (Part 8's real safety gate)."""
    from data.business_knowledge_service import get_source_metric_evidence

    # FINAL PRE-37 MILESTONE, Part 16/17 — widened from 20: live-confirmed
    # against a real production database that a hard top-20-by-authority-score cutoff,
    # combined with domain diversity capping most slots to the single
    # "Operations" domain most real business tables share, could still
    # exclude a genuinely central table (dbo.EDU_ClassSignups/EDU_Class)
    # purely because OTHER same-domain tables individually scored a little
    # higher — never reaching the AI's own domain-adjudication reasoning at
    # all. A wider pool gives the AI (which reasons about genuine business
    # centrality vs. generic infrastructure, not just the raw authority
    # number) more real candidates to choose diversely from; the AI is
    # still capped to proposing _MAX_KPIS, and every proposal is still
    # independently re-validated from scratch (attempt_structured_kpi_plan).
    evidence = get_source_metric_evidence(source_id, user_id, limit_tables=35)
    if not evidence:
        return [], ["No approved metric/dimension/date columns were found for this source — cannot propose report KPIs."]

    # Part 8/9 — deterministic infrastructure-telemetry backstop, applied
    # BEFORE the AI ever sees these candidates (never a post-hoc title
    # blacklist): a table this source itself has no better business-
    # operational alternative for is never excluded, since removing a
    # candidate from THIS list means the AI proposes from whatever remains
    # (Part 12 — natural KPI replacement, no separate code path needed).
    infra_excluded = [t for t in evidence if _is_low_relevance_infrastructure_table(t)]
    infra_warnings = [
        f"Excluded candidate table {t['table_fqn']} from executive KPI proposals: "
        "structurally thin (no metric columns, at most one dimension column) and "
        "lexically matches auth/session/system-telemetry semantics — low executive "
        "business relevance."
        for t in infra_excluded
    ]
    if infra_excluded:
        excluded_fqns = {t["table_fqn"] for t in infra_excluded}
        evidence = [t for t in evidence if t["table_fqn"] not in excluded_fqns]
    if not evidence:
        return [], infra_warnings + [
            "Every candidate table for this source was excluded as low-relevance "
            "infrastructure — cannot propose report KPIs.",
        ]

    evidence_terms = _evidence_terms(evidence)

    raw_kpis = _ai_propose_kpis(evidence)
    used_fallback = raw_kpis is None
    if raw_kpis is None:
        raw_kpis = _deterministic_fallback_kpis(evidence)

    warnings: list[str] = list(infra_warnings)
    specs: list[KPIQuerySpec] = []
    # Part 19 — parse every proposal first (not capped at max_kpis yet):
    # dedup below may collapse some, and truncating to max_kpis BEFORE
    # dedup could silently drop a genuinely distinct KPI in favor of a
    # duplicate that happened to be parsed first.
    for idx, raw in enumerate(raw_kpis[: max_kpis * 2]):  # allow some to fail grounding/parsing
        if not isinstance(raw, dict):
            continue
        if not _kpi_is_grounded(raw, evidence_terms):
            warnings.append(f"Proposed KPI '{raw.get('title')}' rejected: not grounded in available schema evidence.")
            continue
        if _kpi_uses_unsupported_financial_terms(raw, evidence_terms):
            warnings.append(
                f"Proposed KPI '{raw.get('title')}' rejected: uses a financial term "
                f"(revenue/profit) not supported by any approved business label in the evidence."
            )
            continue
        if _kpi_title_entity_mismatch(raw):
            warnings.append(
                f"Proposed KPI '{raw.get('title')}' rejected: its title names a more specific "
                f"concept than its own entity_name ({raw.get('entity_name')!r}) and measure/dimension "
                "terms actually reference — refusing rather than risk resolving to an unrelated "
                "generic table under a misleading label."
            )
            continue
        # A calendar-grain trend groups into multiple periods (rendered as a
        # chart series); a comparison collapses to ONE scalar value per
        # period (current vs previous) — structurally incompatible, not a
        # business-relevance judgment call the AI can be trusted to get
        # right by prompt wording alone (live-confirmed: the model proposed
        # BOTH calendar_grain="month" and comparison_type="month_over_month"
        # for one "Monthly Payment Trend" KPI, which report_orchestrator can
        # only ever resolve as a silently-dropped chart — see its own
        # cur_card/prev_card single-scalar requirement). Deterministically
        # keep the trend (the richer, chart-producing shape) and drop the
        # comparison here, before _parse_kpi_dict ever builds a
        # ComparisonRequest from it — never a per-title/per-table special
        # case, applies to any proposal shaped this way.
        if raw.get("calendar_grain") and raw.get("comparison_type"):
            warnings.append(
                f"Proposed KPI '{raw.get('title')}' comparison removed: a calendar-grain "
                "trend already renders as a chart series and cannot also collapse to a "
                "single comparison scalar."
            )
            raw = {**raw, "comparison_type": None}
        # NEXT MILESTONE, Part 19 — a title claiming a status/temporal
        # condition ("Active Users") the KPI's own business_question text
        # never asks for is a mislabeled metric, not a bad KPI: corrected
        # conservatively (the unsupported word stripped from the title) the
        # same way the comparison_type fix above mutates `raw` in place,
        # rather than rejected outright — the numeric value this KPI will
        # execute is untouched, only its label.
        unsupported_status = _kpi_title_status_word_unsupported(raw)
        if unsupported_status:
            corrected_title = re.sub(
                rf"\b{re.escape(unsupported_status)}\b", "", str(raw.get("title") or ""), flags=re.IGNORECASE,
            )
            corrected_title = re.sub(r"\s{2,}", " ", corrected_title).strip() or f"Total {raw.get('entity_name') or ''}".strip()
            warnings.append(
                f"Proposed KPI title '{raw.get('title')}' renamed to '{corrected_title}': "
                f"its own business_question ('{raw.get('business_question')}') never applies an "
                f"'{unsupported_status}' filter, so the original title claimed a condition the "
                "query does not actually prove."
            )
            raw = {**raw, "title": corrected_title}
        spec = _parse_kpi_dict(raw, idx)
        if spec is None:
            warnings.append(f"Proposed KPI '{raw.get('title')}' rejected: malformed proposal.")
            continue
        specs.append(spec)

    specs, dedup_warnings = _dedupe_kpis(specs)
    warnings.extend(dedup_warnings)
    specs = specs[:max_kpis]

    if used_fallback:
        warnings.append("AI report planning unavailable — used a deterministic evidence-based KPI proposal instead.")

    return specs, warnings
