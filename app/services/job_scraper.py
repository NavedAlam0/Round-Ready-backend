import httpx
from bs4 import BeautifulSoup


async def scrape_job_posting(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    # Try to extract structured data
    title = ""
    company = ""
    description = ""

    # Try meta tags first
    og_title = soup.find("meta", property="og:title")
    if og_title:
        title = og_title.get("content", "")

    og_desc = soup.find("meta", property="og:description")
    if og_desc:
        description = og_desc.get("content", "")

    # Try common selectors for job postings
    for selector in ["h1", ".job-title", "[data-testid='jobTitle']", ".posting-headline h2"]:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            title = title or el.get_text(strip=True)
            break

    for selector in [".company-name", "[data-testid='companyName']", ".posting-categories .sort-by-team"]:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            company = el.get_text(strip=True)
            break

    # Get full page text as fallback for description
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    raw_text = soup.get_text(separator="\n", strip=True)

    # Trim to reasonable length
    if len(raw_text) > 10000:
        raw_text = raw_text[:10000]

    return {
        "title": title,
        "company": company,
        "description": description or raw_text[:3000],
        "raw_text": raw_text,
    }
