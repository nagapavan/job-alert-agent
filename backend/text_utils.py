"""Text-length constants and the truncation idiom they share.

These are code invariants rather than tunables: each value is coupled to a specific code path, and
changing one without its counterpart silently changes behaviour — an embedding sample sliced
differently at index time and at query time compares vectors built from different text. They stay
separate constants even where the numbers coincide, because the reasons do not coincide.
"""

EMBED_SAMPLE_CHARS = 400    # dismissal pattern/job text; index and query must slice identically
PROMPT_CONTEXT_CHARS = 600  # JD or resume excerpt handed to the model
HEADER_PROBE_CHARS = 400    # leading resume region used for contact/name detection
PREVIEW_CHARS = 400         # raw text echoed back to the SPA
SNIPPET_CHARS = 400         # stored Gmail snippet on a review-queue row


def truncate_preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """Cut ``text`` to ``limit`` characters, marking it with an ellipsis when it was longer."""
    text = text or ""
    return text[:limit] + ("..." if len(text) > limit else "")
