import re
import logging
import httpx

logger = logging.getLogger(__name__)

def parse_ddg_html(html_text: str) -> list[str]:
    """Parse search snippet text fields from DuckDuckGo results using standard regex."""
    # Find snippet links
    pattern = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
    raw_snippets = pattern.findall(html_text)
    
    clean_snippets = []
    for snippet in raw_snippets[:4]:
        # Remove nested HTML tags
        clean = re.sub(r'<[^>]+>', '', snippet)
        # Unescape standard HTML entities
        clean = (clean.replace('&amp;', '&')
                      .replace('&lt;', '<')
                      .replace('&gt;', '>')
                      .replace('&quot;', '"')
                      .replace('&#x27;', "'")
                      .replace('&#39;', "'")
                      .replace('&nbsp;', ' '))
        text = clean.strip()
        if text:
            clean_snippets.append(text)
    return clean_snippets

async def search_web_async(query: str) -> str:
    """Execute an asynchronous query to retrieve web search snippets."""
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    params = {"q": query}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers, timeout=6.0)
            if resp.status_code == 200:
                snippets = parse_ddg_html(resp.text)
                if snippets:
                    return "\n".join(f"- {s}" for s in snippets)
                return "No structured search records found."
            else:
                logger.error(f"Search API returned code {resp.status_code}")
    except Exception as e:
        logger.error(f"Failed to query web search results: {e}")
        
    return "Search results currently unavailable."
