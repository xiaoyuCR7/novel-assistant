"""Story-facing chapter-summary projection without internal audit metadata."""

from copy import deepcopy


def story_summary_details(details: dict | None) -> dict:
    """Return summary story fields safe for ledgers, search and model context."""
    projected = deepcopy(details or {})
    projected.pop("content_check", None)
    return projected

