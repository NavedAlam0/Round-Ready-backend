"""Backwards-compatibility shim.

The real implementation lives in `llm_client.py`, which is provider-agnostic.
This module only exists so existing imports keep working:

    from app.services.claude_client import chat

New code should import from `app.services.llm_client` instead.
"""

from app.services.llm_client import chat, chat_json

__all__ = ["chat", "chat_json", "get_claude_client"]


def get_claude_client():
    """Deprecated: returns the raw Anthropic client.

    Kept only for callers that haven't migrated. Using this bypasses the
    provider abstraction and breaks when LLM_PROVIDER isn't "anthropic",
    so prefer `chat()` / `chat_json()`.
    """
    from app.services.llm_client import _anthropic_client

    return _anthropic_client()
