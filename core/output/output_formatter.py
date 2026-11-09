def format_output(data: dict) -> dict:
    status = "error" if data.get("status") == "failed" else "success"
    return {
        "status": status,
        "data": data
    }
