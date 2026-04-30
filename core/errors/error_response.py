def build_error_response(message: str, details: str | None = None) -> dict:
    return {
        "status": "error",
        "message": message,
        "details": details,
    }
