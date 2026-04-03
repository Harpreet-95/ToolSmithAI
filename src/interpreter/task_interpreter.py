def detect_task_type(lowered: str) -> str:
    PRIORITY = ["generate_report", "send_email", "set_reminder"]
    matched = []
    if "email" in lowered or "mail" in lowered or "message" in lowered:
        matched.append("send_email")
    if "report" in lowered or "summary" in lowered:
        matched.append("generate_report")
    if "remind" in lowered or "reminder" in lowered:
        matched.append("set_reminder")
    return next((t for t in PRIORITY if t in matched), "unknown")


def detect_frequency(lowered: str) -> str | None:
    if "daily" in lowered:
        return "daily"
    elif "weekly" in lowered:
        return "weekly"
    elif "monthly" in lowered:
        return "monthly"
    return None


def interpret_task(user_input: str) -> dict:
    lowered = user_input.lower()
    task = detect_task_type(lowered)
    frequency = detect_frequency(lowered)
    return {"original_input": user_input, "task_type": task, "frequency": frequency}
