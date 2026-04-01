def interpret_task(user_input: str) -> dict:
    lowered = user_input.lower()
    PRIORITY = ["generate_report", "send_email"]
    matched = []
    if "email" in lowered or "mail" in lowered or "message" in lowered:
        matched.append("send_email")
    if "report" in lowered or "summary" in lowered:
        matched.append("generate_report")
    task = next((t for t in PRIORITY if t in matched), "unknown")
    if "daily" in lowered:
        frequency = "daily"
    elif "weekly" in lowered:
        frequency = "weekly"
    elif "monthly" in lowered:
        frequency = "monthly"
    else:
        frequency = None
    return {"original_input": user_input, "task_type": task, "frequency": frequency}
