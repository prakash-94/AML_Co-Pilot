"""Small helper shared by the detectors."""

from datetime import datetime


def parse_timestamp(value: object) -> datetime:
    """Accept either a real datetime or the string form ingestion hands back."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
