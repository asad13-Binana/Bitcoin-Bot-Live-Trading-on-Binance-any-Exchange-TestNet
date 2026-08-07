"""Response schema notes. Endpoints return JSON dicts documented in README.
Structured errors: {"detail": {"error": <code>, "request_id": <id>}}.
Every payload includes "mode" and "banner"."""
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    error: str
    request_id: str
