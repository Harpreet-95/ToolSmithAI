"""
AI Intent Composer — Dynamic Tool Composer v1

Turns a natural-language intent into a proposal dict for admin review.
Two analysis stages:
  1. Deterministic rules  — always runs; zero external dependencies.
  2. AI enrichment        — only when ENABLE_AI_PLANNER=true and openai is installed.
     AI can only refine metadata (goal, name, inputs). It cannot introduce new
     primitives or steps that are not already in ALLOWED_PRIMITIVES.

The result is a proposal ONLY — nothing is saved, nothing is executed.
"""

import json
import logging
import re

from core.primitives.executor import ALLOWED_PRIMITIVES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword tables — primitives / workflow routing
# ---------------------------------------------------------------------------

_PRIMITIVE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("http_request",      ["fetch", " http", "https://", "api ", "request", "webhook", "endpoint", "url ", "external"]),
    ("transform_json",    ["transform", "extract", "parse", "convert", "map ", "rename", "reshape", "filter"]),
    ("send_email",        ["email ", "mail ", "send email", "send mail"]),
    ("send_notification", ["notif", "alert", "ping", "push notif"]),
    ("format_output",     ["format", "template", "render", "compose message", "generate message"]),
]

_DYNAMIC_SIGNALS: frozenset = frozenset([
    "api", "http", "https", "url", "endpoint", "webhook", "fetch",
    "external", "transform", "parse", "extract", "convert", "format",
    "template", "render",
])

_WORKFLOW_SIGNALS: frozenset = frozenset([
    "report", "dataset", "analysis", "analyze", "analyse",
    "summarize", "summarise", "dataset report",
    # Expanded vocabulary (Fix 2)
    "summary", "overview", "digest", "insights", "intelligence",
    "breakdown", "kpi", "kpis",
])

# Anomaly/monitor detection signals — combined with alert signals to identify
# multi-step "detect then notify" workflows.
_ANOMALY_SIGNALS: frozenset = frozenset([
    "anomaly", "anomalies", "monitor", "drift", "outlier", "outliers",
    "irregular", "abnormal", "detect", "detection", "spike", "unusual",
])

_ALERT_SIGNALS: frozenset = frozenset([
    "alert", "notify", "notif", "notification", "ping",
])

# Step-level signals for multi-intent accumulation (Fixes 1, 2)
_REPORT_STEP_SIGNALS: frozenset = frozenset([
    "report", "summary", "overview", "digest", "insights", "intelligence",
    "breakdown", "kpi", "kpis", "summarize", "summarise",
])
_ANALYZE_STEP_SIGNALS: frozenset = frozenset([
    "analyze", "analyse", "analysis",
])
_EMAIL_STEP_SIGNALS: frozenset = frozenset(["email", "mail"])
_NOTIFY_STEP_SIGNALS: frozenset = frozenset([
    "notify", "notification", "notif", "alert", "ping",
])
_REMINDER_SIGNALS: frozenset = frozenset(["remind", "reminder"])

# Validated step types — kept in sync with ALLOWED_MULTI_STEP_TYPES in workflow_service (Fix 5)
_ALLOWED_WORKFLOW_STEP_TYPES: frozenset = frozenset({
    "generate_dataset_report",
    "email_dataset_report",
    "send_notification",
    "analyze_dataset",
})

_PRIMITIVE_PURPOSE: dict[str, str] = {
    "http_request":      "Fetch data from an external HTTP endpoint",
    "transform_json":    "Extract or rename fields from structured data",
    "send_email":        "Send an email to a recipient",
    "send_notification": "Send an in-app notification",
    "format_output":     "Render a text template with input values",
}

_WORKFLOW_PURPOSE: dict[str, str] = {
    "generate_dataset_report": "Generate a comprehensive dataset analysis report",
    "email_dataset_report":    "Generate a report and email it to a recipient",
    "send_notification":       "Send an in-app notification to the user",
    "analyze_dataset":         "Perform a quick dataset quality check",
}

_PRIMITIVE_RISK: dict[str, str] = {
    "http_request":      "high",
    "send_email":        "medium",
    "send_notification": "low",
    "transform_json":    "low",
    "format_output":     "low",
}

_WORKFLOW_RISK: dict[str, str] = {
    "generate_dataset_report": "low",
    "email_dataset_report":    "medium",
    "send_notification":       "low",
    "analyze_dataset":         "low",
}

_STOP_WORDS: frozenset = frozenset([
    "a", "an", "the", "for", "my", "our", "this", "that",
    "with", "and", "or", "to", "from", "of", "in", "on",
    "at", "by", "is", "are", "be", "it", "i",
])

# ---------------------------------------------------------------------------
# Report intent classification
# ---------------------------------------------------------------------------

# Vague patterns: generic requests with no qualifying specifics.
# These trigger clarification_required when no specific-intent keyword is present.
_VAGUE_PATTERNS: frozenset[str] = frozenset([
    "generate report",
    "create report",
    "make report",
    "show report",
    "run report",
    "get report",
    "give me report",
    "create dashboard",
    "make dashboard",
    "show dashboard",
    "make analysis",
    "create analysis",
    "do analysis",
    "run analysis",
    "show analysis",
    "give me analysis",
    "do report",
])

# Keywords that map each request to a specific report intent type.
# First type with the highest score wins; "full_intelligence_report" is the fallback.
_REPORT_INTENT_KEYWORDS: dict[str, frozenset[str]] = {
    "executive_summary": frozenset([
        "executive", "exec summary", "high-level", "highlight",
        "brief", "ceo", "c-suite", "board", "leadership", "stakeholder",
        "management overview", "quick summary", "short summary",
    ]),
    "visual_chart_report": frozenset([
        "chart", "charts", "graph", "graphs", "plot", "plots",
        "diagram", "diagrams", "bar chart", "pie chart", "line chart",
        "scatter", "histogram", "visual", "visuals", "visualization",
        "visualize", "dashboard", "visual report",
    ]),
    "anomaly_report": frozenset([
        "anomaly", "anomalies", "outlier", "outliers", "drift",
        "unusual", "irregular", "abnormal", "detection", "detect",
        "strange", "error detection", "quality issues",
    ]),
    "data_quality_report": frozenset([
        "quality", "data quality", "missing", "null values", "completeness",
        "clean", "cleanse", "cleaning", "validation", "validate",
        "integrity", "hygiene", "missing values",
    ]),
    "trend_report": frozenset([
        "trend", "trends", "over time", "temporal", "time series",
        "history", "historical", "growth", "progression", "change over",
        "time-based", "time based", "over months", "over years",
    ]),
    "full_intelligence_report": frozenset([
        "full", "complete", "comprehensive", "all sections", "everything",
        "detailed", "in-depth", "deep dive", "thorough", "intelligence",
        "advanced", "all analysis", "full analysis", "full report",
        "complete report", "comprehensive report",
    ]),
}

# Section type strings exactly as stamped by report_generator.py.
# None means all sections (full intelligence report).
_SECTION_MAP: dict[str, list[str] | None] = {
    "executive_summary":       ["executive_summary", "kpi", "recommendation"],
    "visual_chart_report":     ["executive_summary", "chart", "trend", "text"],
    "anomaly_report":          ["executive_summary", "anomaly", "drift_detection", "text"],
    "data_quality_report":     ["executive_summary", "kpi", "text", "recommendation"],
    "trend_report":            ["executive_summary", "trend", "historical_comparison", "drift_detection", "chart"],
    "full_intelligence_report": None,
}

# Audience signals
_AUDIENCE_KEYWORDS: dict[str, frozenset[str]] = {
    "executive": frozenset([
        "executive", "ceo", "c-suite", "board", "leadership",
        "vp", "director", "management", "senior",
    ]),
    "technical": frozenset([
        "technical", "developer", "engineer", "data scientist",
        "analyst", "scientist", "dev team", "engineering",
    ]),
    "business": frozenset([
        "business", "manager", "product", "sales", "marketing",
        "operations", "finance", "stakeholder", "client",
    ]),
}

# Visualization preference signals
_VIZ_KEYWORDS: frozenset[str] = frozenset([
    "chart", "graph", "visual", "plot", "diagram", "bar", "pie",
    "line chart", "scatter", "histogram", "visualization", "visualize",
])

# AI enrichment prompt — unchanged from original
_AI_COMPOSE_SYSTEM_PROMPT = """\
You are a backend tool-composition assistant for a data analytics platform.
Given a user intent, return a compact JSON object with ONLY these keys:
{
  "interpreted_goal": "<one clear sentence describing what the user wants>",
  "suggested_name":   "<snake_case identifier, max 5 words>",
  "primitives":       ["<only from: http_request, transform_json, send_email, send_notification, format_output>"],
  "required_inputs":  ["<parameter names the tool needs at runtime>"],
  "notes":            "<optional short caveat or empty string>"
}
Do not include any other keys. Be concise. Never invent primitive types outside the list.
"""

# GPT reasoning layer prompt — Stage 3, read-only explanation only
_AI_REASONING_SYSTEM_PROMPT = """\
You are a planning assistant explaining a pre-built data analytics proposal to the user.
The proposal was built by a deterministic rule-based system. You CANNOT change the plan.
Return ONLY a JSON object with these keys (all optional except reasoning_summary):
{
  "reasoning_summary":    "<1-2 sentences explaining what this plan does and why it makes sense>",
  "confidence":           <float 0.0-1.0, how well the plan matches the intent>,
  "clarification_question": "<one question to ask if the intent is ambiguous, or null>",
  "suggested_name":       "<snake_case name improvement if obvious, or null>"
}
Rules:
- reasoning_summary must always be present and non-empty.
- confidence must be a float strictly between 0.0 and 1.0.
- Do NOT invent, modify, add, or remove any plan steps.
- Do NOT lower the risk level or bypass approval requirements.
- Do NOT include any other keys.
"""

# ---------------------------------------------------------------------------
# Deterministic helpers — primitives / workflow routing (unchanged)
# ---------------------------------------------------------------------------

def _is_anomaly_alert_intent(lowered: str) -> bool:
    """True when intent combines anomaly/monitor signals with alert/notify signals."""
    has_anomaly = any(sig in lowered for sig in _ANOMALY_SIGNALS)
    has_alert   = any(sig in lowered for sig in _ALERT_SIGNALS)
    return has_anomaly and has_alert


def _classify_intent(lowered: str) -> str:
    if any(sig in lowered for sig in _WORKFLOW_SIGNALS):
        return "workflow"
    if _is_anomaly_alert_intent(lowered):
        return "workflow"
    dynamic_hits = sum(1 for sig in _DYNAMIC_SIGNALS if sig in lowered)
    if dynamic_hits >= 1:
        return "dynamic_tool"
    if any(kw in lowered for kw in ["email ", "mail ", "notif", "alert"]):
        return "dynamic_tool"
    return "workflow"


def _match_primitives(lowered: str) -> list[str]:
    matched = []
    for primitive, keywords in _PRIMITIVE_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            matched.append(primitive)
    return matched if matched else ["format_output"]


def _match_workflow_steps(lowered: str) -> list[str]:
    """Accumulate workflow steps from all detected intent signals.

    Replaces early-return cascade so compound intents produce all relevant steps.
    """
    steps: list[str] = []

    is_reminder = any(sig in lowered for sig in _REMINDER_SIGNALS)

    has_anomaly = any(sig in lowered for sig in _ANOMALY_SIGNALS)
    has_analyze = (not is_reminder) and any(sig in lowered for sig in _ANALYZE_STEP_SIGNALS)
    has_report  = (not is_reminder) and any(sig in lowered for sig in _REPORT_STEP_SIGNALS)
    has_email   = any(sig in lowered for sig in _EMAIL_STEP_SIGNALS)
    has_notify  = any(sig in lowered for sig in _NOTIFY_STEP_SIGNALS)

    # 1. Analysis step
    if has_anomaly or has_analyze:
        steps.append("analyze_dataset")
    # 2. Report generation step
    if has_report:
        steps.append("generate_dataset_report")
    # 3. Email delivery (only when paired with analysis/report context)
    if has_email and (has_report or has_anomaly or has_analyze):
        steps.append("email_dataset_report")
    # 4. Notification delivery
    if has_notify:
        steps.append("send_notification")

    # 5. Fallback
    if not steps:
        if is_reminder:
            return []   # reminder intent with no data signals — bridge falls through to legacy
        if has_email:
            steps.append("email_dataset_report")
        else:
            steps.append("generate_dataset_report")

    return [s for s in list(dict.fromkeys(steps)) if s in _ALLOWED_WORKFLOW_STEP_TYPES]


def _extract_required_inputs(lowered: str, primitives: list[str]) -> list[str]:
    inputs: list[str] = []
    placeholders = re.findall(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', lowered)
    inputs.extend(placeholders)
    if "http_request" in primitives and not placeholders:
        inputs.append("url")
    if "send_email" in primitives and "to" not in inputs:
        inputs.append("to")
    if "send_notification" in primitives and "message" not in inputs:
        inputs.append("message")
    return list(dict.fromkeys(inputs))


def _extract_workflow_required_inputs(
    step_types: list[str],
    dataset_id: int | None,
) -> list[str]:
    """Return required_inputs for a workflow proposal based on step types (Fix 4)."""
    _DATASET_STEPS = {"generate_dataset_report", "email_dataset_report", "analyze_dataset"}
    required: list[str] = []
    if any(st in _DATASET_STEPS for st in step_types) and dataset_id is None:
        required.append("dataset_id")
    if "email_dataset_report" in step_types:
        required.append("recipient_email")
    if "send_notification" in step_types:
        has_data_step = any(st in _DATASET_STEPS for st in step_types)
        if not has_data_step:
            required.append("notification_message")
    return required


def _compute_risk(proposal_type: str, keys: list[str]) -> str:
    risk_table = _PRIMITIVE_RISK if proposal_type == "dynamic_tool" else _WORKFLOW_RISK
    levels = [risk_table.get(k, "low") for k in keys]
    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    return "low"


def _suggest_name(lowered: str) -> str:
    tokens = re.sub(r'[^a-z0-9\s]', '', lowered).split()
    words = [t for t in tokens if t not in _STOP_WORDS and len(t) > 1][:5]
    return "_".join(words) if words else "custom_tool"


def _build_warnings(proposal_type: str, matched: list[str], risk: str) -> list[str]:
    warnings: list[str] = []
    if "http_request" in matched:
        warnings.append(
            "http_request makes external network calls. "
            "Add the target domain to DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS before approving."
        )
    if risk == "high":
        warnings.append("This proposal carries HIGH risk due to external network access. Review carefully before approving.")
    if proposal_type == "workflow" and "email_dataset_report" in matched:
        warnings.append("Email step requires a recipient address to be supplied at execution time.")
    return warnings


def _execution_preview(steps: list[dict]) -> str:
    parts = [f"{s['order']}. {s.get('purpose', s.get('primitive_type') or s.get('step_type', ''))}" for s in steps]
    return "  →  ".join(parts) if parts else "No steps defined."


# ---------------------------------------------------------------------------
# Report intent analysis — new in this version
# ---------------------------------------------------------------------------

def _is_vague_request(lowered: str) -> bool:
    """True when the request matches a known vague pattern with no qualifying specifics."""
    if not any(pat in lowered for pat in _VAGUE_PATTERNS):
        return False
    # If any specific report-type keyword is present, it is not purely vague
    for intent_type, keywords in _REPORT_INTENT_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return False
    return True


def _classify_report_intent(lowered: str) -> str:
    """Return the best-matching report intent type string.

    Scores each type by keyword hit count; 'full_intelligence_report' is the
    fallback when nothing matches.
    """
    scores: dict[str, int] = {t: 0 for t in _REPORT_INTENT_KEYWORDS}
    for intent_type, keywords in _REPORT_INTENT_KEYWORDS.items():
        scores[intent_type] = sum(1 for kw in keywords if kw in lowered)
    best = max(scores, key=lambda t: scores[t])
    return best if scores[best] > 0 else "full_intelligence_report"


def _detect_audience(lowered: str) -> str | None:
    """Return the primary audience signal or None."""
    for audience, keywords in _AUDIENCE_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return audience
    return None


def _detect_visualization_focus(lowered: str) -> bool:
    """True when the request contains explicit visualization preference signals."""
    return any(kw in lowered for kw in _VIZ_KEYWORDS)


def _get_selected_sections(intent_type: str) -> list[str] | None:
    """Return section type allowlist for the intent, or None for full report."""
    return _SECTION_MAP.get(intent_type)


def _detect_clarification(
    lowered: str,
    dataset_id: int | None,
    is_vague: bool,
    report_intent_type: str | None = None,
) -> tuple[bool, list[str]]:
    """Return (clarification_required, missing_inputs list).

    clarification_required is True when:
      - The dataset is missing for a clearly report-related intent, OR
      - The request is vague (no specific intent keywords, matches a generic pattern).

    A classified report_intent_type is sufficient evidence that this is a report
    intent even when the raw text lacks common signal words like "report".
    """
    missing: list[str] = []

    is_report_intent = (
        report_intent_type is not None
        or any(sig in lowered for sig in _WORKFLOW_SIGNALS)
    )
    if is_report_intent and dataset_id is None:
        missing.append("dataset")

    if is_vague:
        missing.append("report_type")

    return (len(missing) > 0, missing)


# ---------------------------------------------------------------------------
# AI enrichment (optional — never invents unvalidated primitives)
# ---------------------------------------------------------------------------

def _ai_enrich(intent: str, base: dict) -> dict | None:
    from core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS
    if not OPENAI_API_KEY:
        return None
    try:
        import openai as _openai
    except ImportError:
        logger.warning("[composer] openai not installed; skipping AI enrichment")
        return None

    try:
        client = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _AI_COMPOSE_SYSTEM_PROMPT},
                {"role": "user",   "content": intent},
            ],
            max_tokens=280,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning("[composer] AI enrichment failed (%s: %s); rule-based proposal used", type(exc).__name__, exc)
        return None

    enriched = dict(base)

    if isinstance(raw.get("interpreted_goal"), str) and raw["interpreted_goal"].strip():
        enriched["interpreted_goal"] = raw["interpreted_goal"].strip()

    if isinstance(raw.get("suggested_name"), str) and raw["suggested_name"].strip():
        clean = re.sub(r'[^a-z0-9_]', '_', raw["suggested_name"].lower().strip()).strip('_')
        if clean:
            enriched["suggested_name"] = clean

    if isinstance(raw.get("required_inputs"), list):
        valid = [str(i) for i in raw["required_inputs"]
                 if isinstance(i, str) and re.match(r'^[A-Za-z_]\w*$', i)]
        if valid:
            enriched["required_inputs"] = valid

    if isinstance(raw.get("primitives"), list) and base["proposal_type"] == "dynamic_tool":
        valid_prims = [p for p in raw["primitives"] if p in ALLOWED_PRIMITIVES]
        if valid_prims:
            new_steps = [
                {
                    "order":          i,
                    "primitive_type": p,
                    "purpose":        _PRIMITIVE_PURPOSE.get(p, p),
                    "config_preview": {},
                    "validated":      True,
                }
                for i, p in enumerate(valid_prims, 1)
            ]
            enriched["primitives_or_steps"] = new_steps
            new_risk = _compute_risk("dynamic_tool", valid_prims)
            level_order = {"low": 0, "medium": 1, "high": 2}
            if level_order.get(new_risk, 0) >= level_order.get(base["risk_level"], 0):
                enriched["risk_level"] = new_risk
            enriched["execution_preview"] = _execution_preview(new_steps)
            enriched["warnings"] = _build_warnings("dynamic_tool", valid_prims, enriched["risk_level"])

    enriched["source"] = "ai_assisted"
    logger.info("[composer] AI enrichment applied: goal='%s'", enriched.get("interpreted_goal", "")[:60])
    return enriched


def _validate_reasoning(raw: dict) -> dict:
    """Strip any keys outside the allowed set; enforce types."""
    out: dict = {}
    rs = raw.get("reasoning_summary")
    if isinstance(rs, str) and rs.strip():
        out["reasoning_summary"] = rs.strip()
    conf = raw.get("confidence")
    if isinstance(conf, (int, float)) and 0.0 < float(conf) <= 1.0:
        out["confidence"] = round(float(conf), 3)
    cq = raw.get("clarification_question")
    if isinstance(cq, str) and cq.strip():
        out["clarification_question"] = cq.strip()
    sn = raw.get("suggested_name")
    if isinstance(sn, str) and sn.strip():
        clean = re.sub(r'[^a-z0-9_]', '_', sn.lower().strip()).strip('_')
        if clean:
            out["suggested_name"] = clean
    return out


def _ai_add_reasoning(intent: str, proposal: dict) -> dict:
    """Stage 3: overlay GPT explanation/confidence onto the proposal (read-only)."""
    from core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS
    if not OPENAI_API_KEY:
        return proposal
    try:
        import openai as _openai
    except ImportError:
        return proposal

    context_summary = (
        f"Intent: {intent}\n"
        f"Plan type: {proposal.get('proposal_type')}\n"
        f"Steps: {[s.get('step_type') or s.get('primitive_type') for s in proposal.get('primitives_or_steps', [])]}\n"
        f"Risk: {proposal.get('risk_level')}\n"
        f"Goal: {proposal.get('interpreted_goal')}"
    )
    try:
        client = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _AI_REASONING_SYSTEM_PROMPT},
                {"role": "user",   "content": context_summary},
            ],
            max_tokens=180,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning("[composer] AI reasoning failed (%s: %s); skipping", type(exc).__name__, exc)
        return {**proposal, "ai_enrichment_used": False, "ai_error": str(exc)}

    validated = _validate_reasoning(raw)
    if not validated.get("reasoning_summary"):
        logger.warning("[composer] AI reasoning returned no summary; skipping")
        return {**proposal, "ai_enrichment_used": False}

    enriched = dict(proposal)
    enriched["reasoning_summary"] = validated["reasoning_summary"]
    if "confidence" in validated:
        enriched["confidence"] = validated["confidence"]
    if "clarification_question" in validated:
        enriched["clarification_question"] = validated["clarification_question"]
    if "suggested_name" in validated:
        enriched["suggested_name"] = validated["suggested_name"]
    enriched["ai_enrichment_used"] = True
    logger.info("[composer] AI reasoning applied: confidence=%s", validated.get("confidence"))
    return enriched


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def compose_from_intent(intent: str, dataset_id: int | None = None) -> dict:
    """Analyse a natural-language intent and return a proposal dict.

    Always returns a result. Never writes to the database. Never executes tools.

    Args:
        intent:     User's natural-language request.
        dataset_id: Optional context — signals that a dataset is available.

    Returns:
        Proposal dict conforming to the AI Intent Composer v1 schema.
        New fields added in this version:
          clarification_required  bool   — True when clarification is needed
          missing_inputs          list   — ["dataset", "report_type", ...]
          suggested_report_type   str    — one of 6 report intent types
          audience                str|None — executive | technical | business | None
          visualization_focus     bool   — True when viz signals detected
          selected_sections       list|None — section type allowlist (None = all)
    """
    from core.config import ENABLE_AI_PLANNER, OPENAI_API_KEY, OPENAI_MODEL

    lowered = intent.lower()

    # ── Stage 1: deterministic rule analysis ────────────────────────────────
    proposal_type = _classify_intent(lowered)

    if proposal_type == "workflow":
        step_types = _match_workflow_steps(lowered)
        _has_anomaly = any(sig in lowered for sig in _ANOMALY_SIGNALS)
        _has_notify  = any(sig in lowered for sig in _NOTIFY_STEP_SIGNALS)
        purpose_override: dict[str, str] = {}
        if _has_anomaly:
            purpose_override["analyze_dataset"]   = "Analyze dataset for anomalies and drift"
            purpose_override["send_notification"] = "Send anomaly alert notification"
        elif len(step_types) > 1 and _has_notify:
            purpose_override["send_notification"] = "Send notification with workflow results"
        primitives_or_steps = [
            {
                "order":     i,
                "step_type": st,
                "purpose":   purpose_override.get(st) or _WORKFLOW_PURPOSE.get(st, st.replace("_", " ")),
                "validated": True,
            }
            for i, st in enumerate(step_types, 1)
        ]
        required_inputs = _extract_workflow_required_inputs(step_types, dataset_id)
        risk = _compute_risk("workflow", step_types)
        warnings = _build_warnings("workflow", step_types, risk)
    else:
        matched_primitives = _match_primitives(lowered)
        primitives_or_steps = [
            {
                "order":          i,
                "primitive_type": p,
                "purpose":        _PRIMITIVE_PURPOSE.get(p, p),
                "config_preview": {},
                "validated":      p in ALLOWED_PRIMITIVES,
            }
            for i, p in enumerate(matched_primitives, 1)
        ]
        required_inputs = _extract_required_inputs(lowered, matched_primitives)
        risk = _compute_risk("dynamic_tool", matched_primitives)
        warnings = _build_warnings("dynamic_tool", matched_primitives, risk)

    # ── Stage 1b: report intent analysis (workflow proposals only) ───────────
    if proposal_type == "workflow":
        is_vague             = _is_vague_request(lowered)
        report_intent_type   = _classify_report_intent(lowered)
        audience             = _detect_audience(lowered)
        visualization_focus  = _detect_visualization_focus(lowered)
        selected_sections    = _get_selected_sections(report_intent_type)
        clarification_req, missing_inputs = _detect_clarification(
            lowered, dataset_id, is_vague, report_intent_type=report_intent_type
        )

        # Propagate dataset missing-input into the required_inputs list so
        # existing checks (e.g. frontend disable logic) see it consistently.
        if "dataset" in missing_inputs and "dataset_id" not in required_inputs:
            required_inputs = ["dataset_id"] + [r for r in required_inputs if r != "dataset_id"]

        # When clarification is needed, add a warning so it surfaces in the UI.
        if clarification_req:
            clarification_warning = _build_clarification_warning(missing_inputs)
            if clarification_warning not in warnings:
                warnings = [clarification_warning] + warnings

        # Propagate selected_sections into report steps so execution can scope output (Fix 3)
        if selected_sections is not None:
            _REPORT_AWARE_STEPS = {"generate_dataset_report", "email_dataset_report"}
            primitives_or_steps = [
                {**step, "selected_sections": selected_sections}
                if step.get("step_type") in _REPORT_AWARE_STEPS
                else step
                for step in primitives_or_steps
            ]
    else:
        is_vague             = False
        report_intent_type   = None
        audience             = None
        visualization_focus  = False
        selected_sections    = None
        clarification_req    = False
        missing_inputs       = []

    base_proposal: dict = {
        "interpreted_goal":    intent.strip().rstrip(".").rstrip("?").strip() + ".",
        "suggested_name":      _suggest_name(lowered),
        "proposal_type":       proposal_type,
        "primitives_or_steps": primitives_or_steps,
        "required_inputs":     required_inputs,
        "risk_level":          risk,
        "approval_required":   True,
        "execution_preview":   _execution_preview(primitives_or_steps),
        "warnings":            warnings,
        "source":              "rule_based",
        "dataset_id":          dataset_id,
        # ── Report intent fields ─────────────────────────────────────────────
        "clarification_required": clarification_req,
        "missing_inputs":         missing_inputs,
        "suggested_report_type":  report_intent_type,
        "audience":               audience,
        "visualization_focus":    visualization_focus,
        "selected_sections":      selected_sections,
        # ── AI reasoning fields (Stage 3) ────────────────────────────────────
        "reasoning_summary":      None,
        "confidence":             None,
        "clarification_question": None,
        "ai_enrichment_used":     False,
        # ── AI metadata (visibility layer) ────────────────────────────────────
        "ai_enabled":    ENABLE_AI_PLANNER,
        "ai_model_used": OPENAI_MODEL if (ENABLE_AI_PLANNER and OPENAI_API_KEY) else None,
        "planner_source": "composer",
        "fallback_used":  False,
    }

    # ── Stage 2: AI enrichment (opt-in, safe fallback) ──────────────────────
    if ENABLE_AI_PLANNER:
        enriched = _ai_enrich(intent, base_proposal)
        if enriched is not None:
            base_proposal = enriched

    # ── Stage 3: AI reasoning overlay (opt-in, read-only, safe fallback) ────
    if ENABLE_AI_PLANNER:
        base_proposal = _ai_add_reasoning(intent, base_proposal)

    return base_proposal


def _build_clarification_warning(missing_inputs: list[str]) -> str:
    """Build a human-readable clarification warning from missing_inputs."""
    labels = {
        "dataset":     "No dataset selected",
        "report_type": "Report type is unclear",
    }
    parts = [labels.get(m, m) for m in missing_inputs]
    joined = " · ".join(parts)
    return f"Clarification needed: {joined}. Please refine the intent or select a dataset before executing."
