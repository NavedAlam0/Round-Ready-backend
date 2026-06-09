import json

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from loguru import logger

from app.services.claude_client import chat

SYSTEM_PROMPT = """You are an expert company research analyst specializing in interview preparation. You have been given content scraped from a company's own website AND external sources (articles, reviews, blog posts).

Your job is to produce a DEEP analysis of this company that helps a candidate prepare for their interview. You are not just summarizing — you are predicting what this company cares about and what they would ask in an interview.

You MUST respond with valid JSON in this exact format:
{
  "name": "Company Name",
  "description": "3-5 sentence description of what the company ACTUALLY does — their core business, who their customers are, what problems they solve. Be specific, not generic.",
  "industry": "e.g., Fintech, E-commerce, Healthcare, SaaS, etc.",
  "tech_stack": ["List of technologies, languages, frameworks, databases, cloud services the company uses — from job postings, engineering blogs, or tech articles"],
  "products": ["Their main products or services — be specific about what each product does"],
  "culture_notes": "What the company values in employees — engineering culture, work style, collaboration, speed vs quality, etc.",
  "engineering_challenges": ["List 3-5 real engineering challenges this company likely faces based on their products and scale — e.g., 'Processing millions of real-time payment transactions with low latency', 'Building recommendation algorithms for personalized content'"],
  "predicted_interview_topics": ["List 5-8 topics this company would likely ask about in interviews — based on their tech stack, products, and engineering challenges. e.g., 'System design for high-throughput payment processing', 'Database optimization and indexing strategies', 'API design and versioning'"],
  "predicted_coding_topics": ["List 3-5 coding problem themes this company would likely test — tied to their actual engineering work. e.g., 'Graph traversal for social network features', 'Sliding window for real-time analytics', 'Hash map optimization for caching layer'"],
  "interview_style": "What the interview process is likely like — number of rounds, types of interviews (coding, system design, behavioral), difficulty level. Based on external reviews and reports."
}

Rules:
- Be SPECIFIC to this company — do not give generic answers that could apply to any tech company
- For tech_stack: combine info from their website, job postings, and external articles. If nothing is found, make educated guesses based on their industry and scale
- For engineering_challenges: think about what REAL problems their engineers solve daily based on the products they build
- For predicted_interview_topics: think like an interviewer at this company — what would YOU test candidates on?
- For predicted_coding_topics: connect coding challenges to their actual business — not random LeetCode topics
- For interview_style: use any Glassdoor/review data if available, otherwise estimate based on company size and industry
- If certain information is genuinely not available, make reasonable inferences based on the company's industry, size, and products — clearly mark these as inferences
- Respond ONLY with the JSON, no other text."""


async def research_company(url: str) -> dict:
    """Deep-research a company using their website + external search results."""

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    pages_text = []

    # --- Phase 1: Scrape the company's own website ---
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Fetch homepage
        try:
            res = await client.get(url, headers=headers)
            res.raise_for_status()
            pages_text.append(("Homepage", _extract_text(res.text)))
        except Exception as e:
            logger.error(f"Failed to fetch homepage {url}: {e}")
            raise ValueError(f"Could not fetch website: {str(e)}")

        # Try to fetch additional pages from the company's site
        base_url = url.rstrip("/")
        extra_paths = [
            "/about", "/about-us", "/company",
            "/careers", "/jobs",
            "/blog", "/engineering", "/tech-blog",
            "/products", "/services", "/solutions",
        ]
        for path in extra_paths:
            try:
                res = await client.get(f"{base_url}{path}", headers=headers)
                if res.is_success:
                    text = _extract_text(res.text)
                    if len(text) > 100:
                        pages_text.append((f"Page: {path}", text[:3000]))
            except Exception:
                continue

    # Extract company name from homepage for search queries
    homepage_text = pages_text[0][1] if pages_text else ""
    company_name = _guess_company_name(url, homepage_text)

    # --- Phase 2: Search the internet for external info ---
    search_results_text = await _search_external_info(company_name, headers)
    if search_results_text:
        pages_text.append(("External Research", search_results_text))

    # --- Phase 3: Combine and send to Claude ---
    combined = ""
    for page_name, text in pages_text:
        combined += f"\n--- {page_name} ---\n{text}\n"

    # Trim to reasonable length for Claude
    if len(combined) > 25000:
        combined = combined[:25000]

    if not combined.strip():
        raise ValueError("Could not extract any text from the website")

    logger.info(f"Researching company from {url} ({len(combined)} chars total, {len(pages_text)} sources)")

    response = await chat(SYSTEM_PROMPT, f"Website URL: {url}\nCompany Name (guessed): {company_name}\n\nAll scraped content:\n{combined}", max_tokens=4096)

    # Parse JSON from response
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    result = json.loads(text)
    logger.info(f"Company research complete: {result.get('name')} — {len(result.get('predicted_interview_topics', []))} interview topics predicted")
    return result


async def _search_external_info(company_name: str, headers: dict) -> str:
    """Search DuckDuckGo for external info about the company."""
    if not company_name:
        return ""

    search_queries = [
        f"{company_name} what does the company do products",
        f"{company_name} interview experience questions glassdoor",
        f"{company_name} engineering tech stack blog",
    ]

    all_results_text = []

    try:
        ddgs = DDGS()
        for query in search_queries:
            try:
                results = ddgs.text(query, max_results=3)
                for r in results:
                    title = r.get("title", "")
                    body = r.get("body", "")
                    link = r.get("href", "")
                    if body:
                        all_results_text.append(f"[{title}] ({link})\n{body}")
            except Exception as e:
                logger.warning(f"DuckDuckGo search failed for '{query}': {e}")
                continue
    except Exception as e:
        logger.warning(f"DuckDuckGo initialization failed: {e}")
        return ""

    # Also try to scrape the top 2 most promising external pages
    promising_urls = []
    try:
        ddgs = DDGS()
        results = ddgs.text(f"{company_name} engineering blog tech stack", max_results=3)
        for r in results:
            href = r.get("href", "")
            # Skip the company's own site and common non-useful sites
            if href and company_name.lower().replace(" ", "") not in href.lower().replace(" ", ""):
                promising_urls.append(href)
    except Exception:
        pass

    if promising_urls:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            for ext_url in promising_urls[:2]:
                try:
                    res = await client.get(ext_url, headers=headers)
                    if res.is_success:
                        text = _extract_text(res.text)
                        if len(text) > 100:
                            all_results_text.append(f"[Scraped: {ext_url}]\n{text[:2000]}")
                except Exception:
                    continue

    combined = "\n\n".join(all_results_text)
    # Trim external results
    if len(combined) > 8000:
        combined = combined[:8000]

    return combined


def _guess_company_name(url: str, homepage_text: str) -> str:
    """Try to guess the company name from the URL or homepage text."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.hostname or ""

    # Remove common prefixes/suffixes
    name = domain.replace("www.", "").split(".")[0]

    # Capitalize
    return name.capitalize() if name else ""


def _extract_text(html: str) -> str:
    """Extract readable text from HTML, removing scripts/styles/nav."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)

    # Remove excessive blank lines
    lines = [line for line in text.split("\n") if line.strip()]
    return "\n".join(lines)
