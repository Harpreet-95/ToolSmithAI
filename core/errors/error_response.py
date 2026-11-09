def build_error_response(message: str, details: str | None = None) -> dict:
    # Never expose exception text for server failures, even if an older route
    # still passes str(exc) as the second argument. Validation/4xx responses
    # may continue to include intentionally authored details.
    if message == "Internal server error":
        details = None
    return {
        "status": "error",
        "message": message,
        "details": details,
    }
