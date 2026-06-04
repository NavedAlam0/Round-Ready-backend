import json

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are a company research analyst. Given the text content scraped from a company's website, analyze and summarize the company.

You MUST respond with valid JSON in this exact format:
{
  "name": "Company Name",
  "description": "2-3 sentence description of what the company does",
  "industry": "e.g., Fintech, E-commerce, Healthcare, SaaS, etc.",
  "tech_stack": ["Known technologies they use"],
  "products": ["Main products or services"],
  "culture_notes": "Brief notes about company culture, values, or work style if mentioned"
}

Rules:
- Only include information that is clearly stated or strongly implied on the website
- If tech stack is not mentioned, return an empty list
- If culture info is not available, return "Not available from website"
- Keep the description concise and factual
- Respond ONLY with the JSON, no other text."""


async def research_company(url: str) -> dict:
    """Fetch a company's website and use Claude to analyze it."""

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    pages_text = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Fetch homepage
        try:
            res = await client.get(url, headers=headers)
            res.raise_for_status()
            pages_text.append(("Homepage", _extract_text(res.text)))
        except Exception as e:
            logger.error(f"Failed to fetch homepage {url}: {e}")
            raise ValueError(f"Could not fetch website: {str(e)}")

        # Try to fetch /about page
        base_url = url.rstrip("/")
        for about_path in ["/about", "/about-us", "/company"]:
            try:
                res = await client.get(f"{base_url}{about_path}", headers=headers)
                if res.is_success:
                    text = _extract_text(res.text)
                    if len(text) > 100:  # Only add if meaningful content
                        pages_text.append(("About Page", text))
                        break
            except Exception:
                continue

    # Combine scraped text
    combined = ""
    for page_name, text in pages_text:
        combined += f"\n--- {page_name} ---\n{text}\n"

    # Trim to reasonable length for Claude
    if len(combined) > 15000:
        combined = combined[:15000]

    if not combined.strip():
        raise ValueError("Could not extract any text from the website")

    logger.info(f"Researching company from {url} ({len(combined)} chars scraped)")

    # Send to Claude for analysis
    response = await chat(SYSTEM_PROMPT, f"Website URL: {url}\n\nScraped content:\n{combined}")

    # Parse JSON from response
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    result = json.loads(text)
    logger.info(f"Company research complete: {result.get('name')}")
    return result


def _extract_text(html: str) -> str:
    """Extract readable text from HTML, removing scripts/styles/nav."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)

    # Remove excessive blank lines
    lines = [line for line in text.split("\n") if line.strip()]
    return "\n".join(lines)
