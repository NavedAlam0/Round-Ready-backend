import json

import anthropic
from loguru import logger

from app.config import settings

SYSTEM_PROMPT = """You are an emotion analysis expert. You analyze interview candidate responses to detect their emotional state.

Given the last few messages from an interview conversation, assess the candidate's emotional state.

IMPORTANT — SPEECH-TO-TEXT CONTEXT:
These messages come from a real-time speech-to-text system (Deepgram). The transcription is often imperfect:
- Names, acronyms, and technical terms are frequently garbled (e.g., "BCA" → "beast", "MySQL" → "MySerian", proper nouns misspelled)
- Sentence fragments and odd phrasing may be transcription errors, NOT signs of confusion or nervousness
- Filler sounds ("um", "uh") are normal in spoken conversation and do NOT indicate nervousness by themselves
- Do NOT interpret incoherent text, misspellings, or jumbled words as "disorganized thought" — it is almost always an STT artifact

Look for these signals (but account for STT artifacts above):
- NERVOUS: repeatedly saying "I don't know" or "I'm not sure", excessive apologizing, giving up on answers, long silences followed by very short responses, explicitly expressing anxiety
- FRUSTRATED: irritation, impatience, defensive tone, pushback on questions
- CONFIDENT: detailed answers, clear structure, assertive statements, elaborating without prompting
- CALM: steady responses, measured tone, thoughtful pauses

You MUST respond with valid JSON only:
{
  "emotion": "nervous|confident|frustrated|calm",
  "intensity": "low|medium|high",
  "signals": ["specific signal 1", "specific signal 2"],
  "should_intervene": false,
  "suggestion": "What the interviewer should do, or empty string if no intervention needed"
}

Rules:
- should_intervene should ONLY be true if the candidate shows CLEAR, UNMISTAKABLE signs of nervousness or distress across MULTIPLE consecutive answers — not just odd phrasing or STT artifacts
- Garbled text, misspelled words, and incomplete sentences are STT errors — do NOT count them as nervousness signals
- Everyone has minor hesitations — only flag genuine distress patterns
- Be VERY conservative: false positives cause the interviewer to repeatedly encourage a candidate who is doing fine, which is worse than missing a genuine case
- When in doubt, return should_intervene: false
- Respond ONLY with JSON, no other text."""

_client = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.claude_api_key)
    return _client


async def analyze_emotion(recent_messages: list[dict]) -> dict:
    """Analyze the candidate's emotional state from recent messages.

    Args:
        recent_messages: Last 4-6 messages from the conversation (user + assistant)

    Returns:
        Emotion analysis dict with emotion, intensity, should_intervene, suggestion
    """
    # Format messages for the prompt
    formatted = ""
    for msg in recent_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle structured content
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        label = "Interviewer" if role == "assistant" else "Candidate"
        formatted += f"{label}: {content}\n\n"

    client = _get_client()

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": formatted}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        result = json.loads(text)
        logger.debug(f"Feeling Agent: emotion={result['emotion']}, intensity={result['intensity']}, intervene={result['should_intervene']}")
        return result
    except Exception as e:
        logger.error(f"Feeling Agent error: {e}")
        return {
            "emotion": "calm",
            "intensity": "low",
            "signals": [],
            "should_intervene": False,
            "suggestion": "",
        }
