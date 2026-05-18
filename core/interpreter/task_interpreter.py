import json
import logging
import re
import uuid
import datetime

from core.registry.tool_registry import validate_step

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataset report intent detection (checked before generic keyword matching)
# ---------------------------------------------------------------------------

_DATASET_EXACT_PHRASES = (
    "dataset report",
    "uploaded dataset",
)

_DATASET_VERBS = ("report", "analyze", "analyse", "summarize", "summarise")
_EMAIL_VERBS = ("email", "send", "mail")


def _is_dataset_report_intent(lowered: str) -> bool:
    if any(phrase in lowered for phrase in _DATASET_EXACT_PHRASES):
        return True
    return "dataset" in lowered and any(v in lowered for v in _DATASET_VERBS)


def _is_email_dataset_intent(lowered: str) -> bool:
    return _is_dataset_report_intent(lowered) and any(v in lowered for v in _EMAIL_VERBS)


def _build_schedule_from_input(lowered: str) -> tuple:
    """Return (schedule_dict | None, frequency | None, day_of_week | None)."""
    frequency = detect_frequency(lowered)
    day_of_week = detect_weekday(lowered)
    if day_of_week and frequency is None:
        frequency = "weekly"
    if not frequency:
        return None, None, None
    unit_map = {"daily": "day", "weekly": "week", "monthly": "month"}
    schedule = {
        "frequency": frequency,
        "interval": 1,
        "unit": unit_map.get(frequency, frequency),
        "start_at": None,
        "timezone": "UTC",
    }
    return schedule, frequency, day_of_week


def _build_email_dataset_report_plan(intent: str) -> dict:
    plan_id = str(uuid.uuid4())
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    schedule, frequency, day_of_week = _build_schedule_from_input(intent.lower())
    return {
        "plan_id": plan_id,
        "version": "1.0",
        "created_at": created_at,
        "status": "pending",
        "intent": intent,
        "task_type": "email_dataset_report",
        "schedule": schedule,
        "steps": [],
        "metadata": {
            "source": "rule_based_interpreter",
            "tags": ["email_dataset_report"],
            "confidence": 0.95,
            "matched_keywords": ["dataset", "email"],
            "unsupported_reason": None,
            "entities": {
                "frequency": frequency,
                "day_of_week": day_of_week,
                "recipient_hint": None,
                "report_hint": None,
                "action_hint": None,
            },
            "warnings": [] if frequency else ["No schedule detected; this will run once."],
        },
    }


def _build_dataset_report_plan(intent: str) -> dict:
    plan_id = str(uuid.uuid4())
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    schedule, frequency, day_of_week = _build_schedule_from_input(intent.lower())
    return {
        "plan_id": plan_id,
        "version": "1.0",
        "created_at": created_at,
        "status": "pending",
        "intent": intent,
        "task_type": "generate_dataset_report",
        "schedule": schedule,
        "steps": [],
        "metadata": {
            "source": "rule_based_interpreter",
            "tags": ["generate_dataset_report"],
            "confidence": 0.95,
            "matched_keywords": ["dataset"],
            "unsupported_reason": None,
            "entities": {
                "frequency": frequency,
                "day_of_week": day_of_week,
                "recipient_hint": None,
                "report_hint": None,
                "action_hint": None,
            },
            "warnings": [] if frequency else ["No schedule detected; this will run once."],
        },
    }


# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------

_STRONG: dict[str, tuple[str, ...]] = {
    "send_email":      ("email", "mail"),
    "generate_report": ("report", "kpi", "metrics"),
    "set_reminder":    ("remind", "reminder", "notify", "notification"),
}
_WEAK: dict[str, tuple[str, ...]] = {
    "send_email":      ("send", "message"),
    "generate_report": ("summary", "digest"),
    "set_reminder":    (),
}
_PRIORITY = ["generate_report", "send_email", "set_reminder"]

_REPORT_TOPICS = (
    "sales", "revenue", "finance", "inventory", "performance",
    "kpi", "metrics", "traffic", "analytics", "budget", "marketing",
)
_ACTION_VERBS = (
    "email", "send", "generate", "create", "remind", "notify",
    "schedule", "run", "fetch", "build",
)
_RECIPIENT_NAMES = ("team", "manager", "boss", "everyone", "all")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ---------------------------------------------------------------------------
# Task type detection
# ---------------------------------------------------------------------------

def detect_task_type(lowered: str) -> tuple[str, list[str], float]:
    matched_strong: set[str] = set()
    matched_weak: set[str] = set()
    keywords: list[str] = []

    for task, strong_kws in _STRONG.items():
        for kw in strong_kws:
            if kw in lowered:
                matched_strong.add(task)
                if kw not in keywords:
                    keywords.append(kw)

    for task, weak_kws in _WEAK.items():
        for kw in weak_kws:
            if kw in lowered:
                matched_weak.add(task)
                if kw not in keywords:
                    keywords.append(kw)

    all_matched = matched_strong | matched_weak
    task_type = next((t for t in _PRIORITY if t in all_matched), "unknown")

    if task_type == "unknown":
        confidence = 0.2
    elif task_type in matched_strong and len(all_matched) == 1:
        confidence = 1.0
    else:
        confidence = 0.6

    return task_type, keywords, confidence


# ---------------------------------------------------------------------------
# Frequency detection
# ---------------------------------------------------------------------------

def detect_frequency(lowered: str) -> str | None:
    if "daily" in lowered:
        return "daily"
    elif "weekly" in lowered:
        return "weekly"
    elif "monthly" in lowered:
        return "monthly"
    return None


def detect_weekday(lowered: str) -> str | None:
    for day in _WEEKDAYS:
        if day in lowered:
            return day
    return None


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

def extract_entities(lowered: str, frequency: str | None, day_of_week: str | None) -> dict:
    tokens = re.split(r"\W+", lowered)
    token_set = set(tokens)

    # recipient_hint — email address takes priority, then "me", then role words
    recipient_hint: str | None = None
    email_match = re.search(r'\S+@\S+\.\S+', lowered)
    if email_match:
        recipient_hint = email_match.group()
    elif "me" in token_set:
        recipient_hint = "self"
    else:
        for name in _RECIPIENT_NAMES:
            if name in token_set:
                recipient_hint = name
                break
        if recipient_hint is None:
            for i, word in enumerate(tokens):
                if word == "to" and i + 1 < len(tokens):
                    candidate = tokens[i + 1]
                    if candidate not in {"a", "the", "my", "your", "our", "it", "this", "that", ""}:
                        recipient_hint = candidate
                        break

    # report_hint — first matching topic word
    report_hint: str | None = None
    for topic in _REPORT_TOPICS:
        if topic in token_set:
            report_hint = topic
            break

    # action_hint — first matching action verb
    action_hint: str | None = None
    for verb in _ACTION_VERBS:
        if verb in token_set:
            action_hint = verb
            break

    return {
        "frequency":      frequency,
        "recipient_hint": recipient_hint,
        "report_hint":    report_hint,
        "action_hint":    action_hint,
        "day_of_week":    day_of_week,
    }


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def build_execution_plan(
    intent: str,
    task_type: str,
    frequency: str | None,
    matched_keywords: list[str],
    confidence: float,
    entities: dict,
) -> dict:
    plan_id = str(uuid.uuid4())
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    schedule = None
    if frequency:
        unit_map = {"daily": "day", "weekly": "week", "monthly": "month"}
        schedule = {
            "frequency": frequency,
            "interval": 1,
            "unit": unit_map.get(frequency, frequency),
            "start_at": None,
            "timezone": "UTC",
        }

    if task_type == "send_email":
        steps = [
            {
                "step_id": "step_1",
                "order": 1,
                "tool": "email_sender",
                "operation": "send_email",
                "params": {
                    "to": None,
                    "subject": "Your Report",
                    "body_template": "default_email_template",
                },
                "depends_on": None,
            }
        ]
    elif task_type == "generate_report":
        steps = [
            {
                "step_id": "step_1",
                "order": 1,
                "tool": "data_fetcher",
                "operation": "fetch_report_data",
                "params": {"source": "sqlite", "table": "tasks", "filters": {}},
                "depends_on": None,
            },
            {
                "step_id": "step_2",
                "order": 2,
                "tool": "email_sender",
                "operation": "send_email",
                "params": {
                    "to": None,
                    "subject": "Your Report",
                    "body_template": "report_template_v1",
                },
                "depends_on": "step_1",
            },
        ]
    elif task_type == "set_reminder":
        steps = [
            {
                "step_id": "step_1",
                "order": 1,
                "tool": "notifier",
                "operation": "send_notification",
                "params": {
                    "channel": "in_app",
                    "message": "Your reminder",
                    "priority": "normal",
                },
                "depends_on": None,
            }
        ]
    else:
        steps = []

    for step in steps:
        validate_step(step)

    tags = ["unknown"] if task_type == "unknown" else [task_type]
    if frequency:
        tags.append(frequency)

    known = task_type != "unknown"

    warnings: list[str] = []
    if not known:
        warnings.append("This task is not supported yet.")
    elif frequency is None:
        warnings.append("No schedule detected; this will run once.")
    if task_type in ("send_email", "generate_report") and entities.get("recipient_hint") is None:
        warnings.append("No recipient detected; defaulting to self.")

    return {
        "plan_id": plan_id,
        "version": "1.0",
        "created_at": created_at,
        "status": "pending",
        "intent": intent,
        "task_type": task_type,
        "schedule": schedule,
        "steps": steps,
        "metadata": {
            "source": "rule_based_interpreter",
            "tags": tags,
            "confidence": confidence,
            "matched_keywords": matched_keywords,
            "unsupported_reason": None if known else "No supported tool intent detected yet.",
            "entities": entities,
            "warnings": warnings,
        },
    }


# ---------------------------------------------------------------------------
# AI Workflow Planner
# ---------------------------------------------------------------------------

_ALLOWED_TASK_TYPES = frozenset({
    "generate_dataset_report",
    "email_dataset_report",
    "send_email",
    "generate_report",
    "set_reminder",
    "unknown",
})

_ALLOWED_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})

_ALLOWED_WEEKDAYS = frozenset({
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
})

_AI_SYSTEM_PROMPT = """\
You are a workflow plan classifier for ToolSmithAI. Analyze the user's request and return ONLY a \
JSON object — no markdown, no explanation, no code fences.

Choose task_type from this exact list only:
- "generate_dataset_report"  : user wants to view/generate a report from their uploaded CSV dataset
- "email_dataset_report"     : user wants to email a report generated from their CSV dataset
- "send_email"               : user wants to send a general email (not dataset-specific)
- "generate_report"          : user wants to generate a generic report (not dataset-specific)
- "set_reminder"             : user wants a reminder or notification
- "unknown"                  : none of the above

Return exactly this JSON structure:
{
  "task_type": "<one value from the list above>",
  "intent": "<user request verbatim>",
  "schedule": null,
  "entities": {
    "frequency": null,
    "day_of_week": null,
    "recipient_hint": null,
    "report_hint": null,
    "action_hint": null
  }
}

If a recurring schedule is requested, set schedule to:
{"frequency": "daily"|"weekly"|"monthly", "day_of_week": null|"<weekday lowercase>"}
and mirror frequency and day_of_week inside entities.
"""


def _validate_ai_plan(raw: dict) -> dict:
    """
    Validate an AI-generated plan dict against the strict allowlist.
    Returns a sanitized dict of safe fields, or raises ValueError.
    This runs even if the model misbehaves — no AI output bypasses this.
    """
    task_type = raw.get("task_type")
    if not isinstance(task_type, str) or task_type not in _ALLOWED_TASK_TYPES:
        raise ValueError(f"invalid task_type: {task_type!r}")

    schedule  = raw.get("schedule")
    frequency: str | None   = None
    day_of_week: str | None = None

    if schedule is not None:
        if not isinstance(schedule, dict):
            raise ValueError("schedule must be a dict or null")
        freq_raw = schedule.get("frequency")
        if freq_raw is not None:
            freq_str = str(freq_raw).lower().strip()
            if freq_str not in _ALLOWED_FREQUENCIES:
                raise ValueError(f"invalid frequency: {freq_raw!r}")
            frequency = freq_str
        dow_raw = schedule.get("day_of_week")
        if dow_raw is not None:
            dow_str = str(dow_raw).lower().strip()
            if dow_str not in _ALLOWED_WEEKDAYS:
                raise ValueError(f"invalid day_of_week: {dow_raw!r}")
            day_of_week = dow_str

    entities_raw = raw.get("entities") or {}
    if not isinstance(entities_raw, dict):
        raise ValueError("entities must be a dict or null")

    def _safe_str(val, max_len: int = 100) -> str | None:
        if val is None:
            return None
        s = str(val).strip()[:max_len]
        return s or None

    entities = {
        "frequency":      frequency,
        "day_of_week":    day_of_week,
        "recipient_hint": _safe_str(entities_raw.get("recipient_hint")),
        "report_hint":    _safe_str(entities_raw.get("report_hint")),
        "action_hint":    _safe_str(entities_raw.get("action_hint"), max_len=50),
    }

    return {
        "task_type":   task_type,
        "frequency":   frequency,
        "day_of_week": day_of_week,
        "entities":    entities,
    }


def _ai_interpret_task(user_input: str) -> dict | None:
    """
    Ask the AI planner to classify user_input.
    Returns a validated, sanitized dict of plan fields on success.
    Returns None on ANY failure — caller always falls back to rule-based interpreter.
    The AI can only propose task_type, schedule, and entities.
    Steps are NEVER derived from AI output; they are always built deterministically.
    """
    from core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS

    if not OPENAI_API_KEY:
        logger.debug("[ai_planner] OPENAI_API_KEY not set; skipping")
        return None

    try:
        import openai as _openai
    except ImportError:
        logger.warning("[ai_planner] openai package not installed; falling back to rule-based interpreter")
        return None

    try:
        client   = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _AI_SYSTEM_PROMPT},
                {"role": "user",   "content": user_input},
            ],
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
        )
        content   = response.choices[0].message.content or ""
        raw       = json.loads(content.strip())
        validated = _validate_ai_plan(raw)
        logger.info(
            "[ai_planner] success: task_type=%s frequency=%s",
            validated["task_type"], validated["frequency"],
        )
        return validated
    except Exception as exc:
        logger.warning(
            "[ai_planner] failed (%s: %s); falling back to rule-based interpreter",
            type(exc).__name__, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def interpret_task(user_input: str) -> dict:
    from core.config import ENABLE_AI_PLANNER

    lowered = user_input.lower()

    # Dataset-specific intents are always handled by the rule-based interpreter.
    # These paths are fast, reliable, and should not be delegated to an LLM.
    if _is_email_dataset_intent(lowered):
        logger.info("[interpreter] source=rule_based_interpreter task_type=email_dataset_report")
        return _build_email_dataset_report_plan(user_input)
    if _is_dataset_report_intent(lowered):
        logger.info("[interpreter] source=rule_based_interpreter task_type=generate_dataset_report")
        return _build_dataset_report_plan(user_input)

    # Try AI planner when enabled. On any failure it returns None and we fall through.
    if ENABLE_AI_PLANNER:
        ai_result = _ai_interpret_task(user_input)
        if ai_result is not None:
            plan = build_execution_plan(
                intent=user_input,
                task_type=ai_result["task_type"],
                frequency=ai_result["frequency"],
                matched_keywords=[],
                confidence=0.9,
                entities=ai_result["entities"],
            )
            plan["metadata"]["source"] = "ai_planner"
            return plan

    # Rule-based fallback — always runs when AI is disabled or fails.
    task, keywords, confidence = detect_task_type(lowered)
    frequency = detect_frequency(lowered)
    day_of_week = detect_weekday(lowered)
    if day_of_week and frequency is None:
        frequency = "weekly"
    entities = extract_entities(lowered, frequency, day_of_week)
    plan = build_execution_plan(
        intent=user_input,
        task_type=task,
        frequency=frequency,
        matched_keywords=keywords,
        confidence=confidence,
        entities=entities,
    )
    logger.info(
        "[interpreter] source=rule_based_interpreter task_type=%s confidence=%.2f",
        task, confidence,
    )
    return plan
