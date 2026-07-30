"""Provider-agnostic LLM layer.

All backend LLM traffic goes through `chat()` / `chat_json()`. Nothing else in
the codebase should import a vendor SDK directly.

Switching provider is an .env change, never a code change:

    LLM_PROVIDER=openai        # or anthropic, gemini, groq, openrouter, ...
    OPENAI_API_KEY=sk-...

Adding a provider that speaks the OpenAI wire format (most of them do) means
adding one row to `_PRESETS` — or nothing at all, if you set a base URL:

    LLM_PROVIDER=custom
    CUSTOM_BASE_URL=https://my-endpoint/v1
    CUSTOM_API_KEY=...
    CUSTOM_MODEL=some-model

Per-provider env vars follow a uniform convention, so no new config fields are
needed for a new provider:

    <PROVIDER>_API_KEY      required (except ollama)
    <PROVIDER>_BASE_URL     optional, overrides the preset
    <PROVIDER>_MODEL        the "default" tier  (main reasoning work)
    <PROVIDER>_MODEL_LIGHT  the "light" tier    (cheap/fast classification)
    <PROVIDER>_MODEL_CODING the "coding" tier   (deep analysis; may reason)

Tiers let call sites ask for a capability instead of naming a model:
    chat(..., tier="light")   -> resume validation, emotion checks
    chat(..., tier="default") -> evaluation, planning
    chat(..., tier="coding")  -> code review, with reasoning where available
"""

import json
import os
import re
from dataclasses import dataclass

from loguru import logger

from app.config import settings


@dataclass(frozen=True)
class _Preset:
    """Defaults for a provider. Every field is overridable via env."""

    # "openai" = OpenAI-compatible REST (works for the large majority of
    # vendors). "anthropic" = native Anthropic SDK.
    api: str
    base_url: str | None
    default: str
    light: str
    coding: str
    # Env var holding the key, when it doesn't follow <PROVIDER>_API_KEY.
    key_env: str | None = None
    # Whether the coding tier should request extended reasoning.
    supports_reasoning: bool = False


# Adding a provider = adding a row here.
_PRESETS: dict[str, _Preset] = {
    "openai": _Preset(
        api="openai",
        base_url=None,  # SDK default
        default="gpt-5.5",
        light="gpt-4o-mini",
        coding="gpt-5.5",
        supports_reasoning=True,
    ),
    "anthropic": _Preset(
        api="anthropic",
        base_url=None,
        default="claude-sonnet-5",
        light="claude-haiku-4-5-20251001",
        coding="claude-opus-5",
        key_env="CLAUDE_API_KEY",
        supports_reasoning=True,
    ),
    # Google exposes an OpenAI-compatible endpoint, so Gemini needs no extra SDK.
    "gemini": _Preset(
        api="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default="gemini-2.5-pro",
        light="gemini-2.5-flash",
        coding="gemini-2.5-pro",
        supports_reasoning=True,
    ),
    "groq": _Preset(
        api="openai",
        base_url="https://api.groq.com/openai/v1",
        default="llama-3.3-70b-versatile",
        light="llama-3.1-8b-instant",
        coding="llama-3.3-70b-versatile",
    ),
    "openrouter": _Preset(
        api="openai",
        base_url="https://openrouter.ai/api/v1",
        default="openai/gpt-5.5",
        light="openai/gpt-5.5-mini",
        coding="anthropic/claude-opus-5",
    ),
    "deepseek": _Preset(
        api="openai",
        base_url="https://api.deepseek.com",
        default="deepseek-chat",
        light="deepseek-chat",
        coding="deepseek-reasoner",
    ),
    "mistral": _Preset(
        api="openai",
        base_url="https://api.mistral.ai/v1",
        default="mistral-large-latest",
        light="mistral-small-latest",
        coding="mistral-large-latest",
    ),
    "xai": _Preset(
        api="openai",
        base_url="https://api.x.ai/v1",
        default="grok-4",
        light="grok-4-fast",
        coding="grok-4",
    ),
    "together": _Preset(
        api="openai",
        base_url="https://api.together.xyz/v1",
        default="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        light="meta-llama/Llama-3.1-8B-Instruct-Turbo",
        coding="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    # Local models. No key required.
    "ollama": _Preset(
        api="openai",
        base_url="http://localhost:11434/v1",
        default="llama3.3",
        light="llama3.2",
        coding="qwen2.5-coder",
    ),
}

_TIERS = ("default", "light", "coding")


def _env(provider: str, suffix: str) -> str | None:
    """Read <PROVIDER>_<SUFFIX> from the environment."""
    value = os.getenv(f"{provider.upper()}_{suffix}")
    return value.strip() if value and value.strip() else None


def _preset(provider: str) -> _Preset:
    """Preset for a provider, or a bare OpenAI-compatible shell for unknown ones."""
    if provider in _PRESETS:
        return _PRESETS[provider]
    # Unknown provider: allow it as long as a base URL and model are supplied.
    base_url = _env(provider, "BASE_URL")
    model = _env(provider, "MODEL")
    if not base_url or not model:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. Either use a known provider "
            f"({', '.join(sorted(_PRESETS))}) or set "
            f"{provider.upper()}_BASE_URL and {provider.upper()}_MODEL."
        )
    return _Preset(api="openai", base_url=base_url, default=model, light=model, coding=model)


def _model_for(provider: str, tier: str) -> str:
    """Pick the model for a provider + tier, honouring env overrides."""
    if tier not in _TIERS:
        raise ValueError(f"Unknown tier '{tier}'. Use one of {_TIERS}.")
    pre = _preset(provider)
    suffix = {"default": "MODEL", "light": "MODEL_LIGHT", "coding": "MODEL_CODING"}[tier]
    override = _env(provider, suffix)
    if override:
        return override
    # Fall back through coding -> default so a single <PROVIDER>_MODEL works.
    if tier != "default":
        generic = _env(provider, "MODEL")
        if generic and tier == "coding":
            return generic
    return getattr(pre, tier)


def _api_key(provider: str) -> str:
    pre = _preset(provider)
    if pre.key_env:
        key = os.getenv(pre.key_env, "").strip()
        if key:
            return key
    key = _env(provider, "API_KEY") or ""
    if not key and provider == "anthropic":
        key = (settings.claude_api_key or "").strip()
    return key


# --------------------------------------------------------------------------
# Clients (lazily created, one per provider)
# --------------------------------------------------------------------------

_clients: dict[str, object] = {}


def _openai_client(provider: str):
    if provider not in _clients:
        from openai import AsyncOpenAI

        pre = _preset(provider)
        base_url = _env(provider, "BASE_URL") or pre.base_url
        key = _api_key(provider)
        if not key and provider != "ollama":
            raise RuntimeError(
                f"No API key for LLM provider '{provider}'. "
                f"Set {provider.upper()}_API_KEY in .env."
            )
        _clients[provider] = AsyncOpenAI(
            api_key=key or "not-needed",
            base_url=base_url,
        )
    return _clients[provider]


def _anthropic_client(provider: str = "anthropic"):
    if provider not in _clients:
        import anthropic

        key = _api_key(provider)
        if not key:
            raise RuntimeError(
                "No API key for LLM provider 'anthropic'. "
                "Set CLAUDE_API_KEY (or ANTHROPIC_API_KEY) in .env."
            )
        _clients[provider] = anthropic.AsyncAnthropic(api_key=key)
    return _clients[provider]


# --------------------------------------------------------------------------
# Per-API implementations
# --------------------------------------------------------------------------


async def _chat_openai(
    provider: str, system_prompt: str, user_message: str, max_tokens: int, tier: str
) -> str:
    client = _openai_client(provider)
    model = _model_for(provider, tier)
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }

    # Reasoning models reject `max_tokens` and `temperature`, and need the
    # budget expressed as max_completion_tokens (it also covers hidden
    # reasoning tokens, so give it headroom).
    if _is_reasoning_model(model):
        kwargs["max_completion_tokens"] = max_tokens * 2
        if tier == "coding":
            effort = (settings.llm_reasoning_effort or "medium").strip()
            if effort:
                kwargs["reasoning_effort"] = effort
    else:
        kwargs["max_tokens"] = max_tokens

    response = await client.chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()


async def _chat_anthropic(
    provider: str, system_prompt: str, user_message: str, max_tokens: int, tier: str
) -> str:
    client = _anthropic_client(provider)
    model = _model_for(provider, tier)
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    # Skip thinking blocks; return the visible text.
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return ""


_DISPATCH = {"openai": _chat_openai, "anthropic": _chat_anthropic}


def _is_reasoning_model(model: str) -> bool:
    """True for OpenAI reasoning families, which take different params."""
    m = model.lower()
    return bool(re.match(r"^(o\d|gpt-5)", m))


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


async def chat(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4096,
    tier: str = "default",
) -> str:
    """Send a single-turn prompt to the configured provider and return the text.

    Args:
        system_prompt: System instruction.
        user_message: The user turn.
        max_tokens: Output budget.
        tier: "default", "light", or "coding".
    """
    primary = (settings.llm_provider or "openai").strip().lower()

    fallback = (settings.llm_fallback_provider or "").strip().lower()
    if not fallback and settings.llm_fallback_enabled:
        # Backwards compatible with the old boolean flag: pick the other vendor.
        fallback = "anthropic" if primary == "openai" else "openai"

    try:
        impl = _DISPATCH[_preset(primary).api]
        return await impl(primary, system_prompt, user_message, max_tokens, tier)
    except Exception as exc:
        if not fallback or fallback == primary:
            raise
        logger.warning(
            f"LLM provider '{primary}' failed ({exc}); falling back to '{fallback}'"
        )
        impl = _DISPATCH[_preset(fallback).api]
        return await impl(fallback, system_prompt, user_message, max_tokens, tier)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


async def chat_json(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4096,
    tier: str = "default",
    default: dict | None = None,
) -> dict:
    """Like `chat`, but parse the reply as JSON.

    Providers differ in how much markdown they wrap around JSON, so the fences
    are stripped here rather than at every call site. Returns `default` (or {})
    if the reply isn't parseable.
    """
    raw = await chat(system_prompt, user_message, max_tokens=max_tokens, tier=tier)
    cleaned = _FENCE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        # Last resort: pull the outermost {...} block out of a chatty reply.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        logger.warning(f"LLM did not return valid JSON: {cleaned[:200]!r}")
        return default if default is not None else {}


def active_provider_info() -> dict:
    """Resolved provider/model per tier — used for the startup log."""
    provider = (settings.llm_provider or "openai").strip().lower()
    try:
        pre = _preset(provider)
        return {
            "provider": provider,
            "api": pre.api,
            "base_url": _env(provider, "BASE_URL") or pre.base_url or "(sdk default)",
            "has_key": bool(_api_key(provider)) or provider == "ollama",
            "models": {t: _model_for(provider, t) for t in _TIERS},
        }
    except ValueError as exc:
        return {"provider": provider, "error": str(exc)}
