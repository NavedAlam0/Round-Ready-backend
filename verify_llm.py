"""Check the backend's LLM configuration, then make one real call per tier.

Run after any .env change:

    cd backend && venv/bin/python verify_llm.py

Exits non-zero if anything is misconfigured, so it's usable in CI.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.services.llm_client import active_provider_info, chat, chat_json  # noqa: E402


async def main() -> int:
    info = active_provider_info()

    print("=" * 62)
    print("BACKEND LLM CONFIG")
    print("=" * 62)

    if info.get("error"):
        print(f"  INVALID: {info['error']}")
        return 1

    print(f"  LLM_PROVIDER : {info['provider']}")
    print(f"  wire format  : {info['api']}")
    print(f"  base_url     : {info['base_url']}")
    print(f"  API key set  : {'yes' if info['has_key'] else 'NO'}")
    print("  models:")
    for tier, model in info["models"].items():
        print(f"    {tier:8s} -> {model}")
    if settings.llm_fallback_provider:
        print(f"  fallback     : {settings.llm_fallback_provider}")

    if not info["has_key"]:
        print(f"\n  FAIL: set {info['provider'].upper()}_API_KEY in backend/.env")
        return 1

    print()
    print("=" * 62)
    print("LIVE CALLS")
    print("=" * 62)

    failures = 0

    for tier in ("light", "default", "coding"):
        try:
            reply = await chat(
                "Reply with exactly the word OK and nothing else.",
                "ping",
                max_tokens=16,
                tier=tier,
            )
            ok = "OK" in reply.upper()
            print(f"  {'PASS' if ok else 'WARN'}  chat(tier={tier:8s}) -> {reply[:40]!r}")
            if not ok:
                failures += 1
        except Exception as e:
            print(f"  FAIL  chat(tier={tier:8s}) -> {type(e).__name__}: {str(e)[:90]}")
            failures += 1

    try:
        data = await chat_json(
            "Return JSON only, no prose.",
            'Return exactly {"ok": true}',
            max_tokens=40,
            tier="light",
        )
        ok = data.get("ok") is True
        print(f"  {'PASS' if ok else 'WARN'}  chat_json()            -> {data}")
        if not ok:
            failures += 1
    except Exception as e:
        print(f"  FAIL  chat_json()            -> {type(e).__name__}: {str(e)[:90]}")
        failures += 1

    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All backend LLM checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
