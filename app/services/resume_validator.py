from loguru import logger

from app.services.claude_client import get_claude_client

SYSTEM_PROMPT = """You are a resume validation expert. Your job is to determine if the given text is extracted from a real resume/CV or not.

A valid resume typically contains MOST of these elements:
- A person's name
- Contact information (email, phone, LinkedIn, etc.)
- Work experience or employment history
- Education or academic background
- Skills or technical proficiencies
- Projects, certifications, or achievements

Be lenient — accept resumes in any format, language, or style. Freshers/students may not have work experience but will have education and skills.

Documents that are NOT resumes include:
- Random articles, essays, or blog posts
- Invoices, receipts, or financial documents
- Legal documents, contracts, or agreements
- Academic papers or research publications (unless it's clearly a CV)
- Blank or near-empty documents
- Gibberish or unreadable text

Respond with ONLY a JSON object (no markdown, no extra text):
{"is_resume": true} or {"is_resume": false, "reason": "brief explanation"}"""


async def validate_resume_text(text: str) -> dict:
    """Check if extracted PDF text looks like a resume. Returns {"is_resume": bool, "reason": str}."""

    word_count = len(text.split())

    if word_count < 30:
        return {
            "is_resume": False,
            "reason": "The document has too little text. Please upload a resume with your details.",
        }

    # Use Haiku for fast, cheap validation
    try:
        client = get_claude_client()
        # Only send first 3000 chars to keep it fast and cheap
        truncated = text[:3000]
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": truncated}],
        )
        raw = response.content[0].text.strip()

        # Parse JSON response — strip markdown code fences if present
        import json
        import re
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        result = json.loads(cleaned)
        return {
            "is_resume": result.get("is_resume", False),
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        logger.warning(f"Resume validation LLM call failed: {e}")
        # If validation fails, allow the upload (don't block users due to AI errors)
        return {"is_resume": True, "reason": ""}
