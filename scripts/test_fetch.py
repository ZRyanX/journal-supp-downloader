#!/usr/bin/env python3
"""Test campus IP direct access via StealthyFetcher (bypasses Cloudflare)."""
import re
import time
from scrapling.fetchers import StealthyFetcher

url = "https://www.sciencedirect.com/science/article/pii/S0169136826002489"
print(f"Testing campus IP direct access via browser...")
print(f"URL: {url}")
t0 = time.time()

# No cookies needed - campus IP should be recognized by ScienceDirect
page = StealthyFetcher.fetch(
    url,
    headless=True,
    solve_cloudflare=True,
    network_idle=True,
    timeout=60000,
)

elapsed = time.time() - t0
html = str(page.html_content)
print(f"Status: {page.status}")
print(f"Time: {elapsed:.1f}s")
print(f"Content: {len(html)} chars")

# Check institutional access indicator
if "institutional" in html.lower() or "institution" in html.lower():
    print("RESULT: Institutional access detected!")
elif "purchase" in html.lower() or "subscribe" in html.lower():
    print("RESULT: No institutional access (paywall visible)")
else:
    print("RESULT: Access status unknown")

# Check supplement links
mmcs = set(re.findall(r"mmc\d+", html, re.I))
print(f"MMC refs: {mmcs}")

cdn_links = re.findall(r"https?://ars\.els-cdn\.com/content/image/[^\"\'\s>]+", html)
print(f"CDN links: {len(cdn_links)}")
for l in cdn_links:
    print(f"  {l}")

# Find all supplement hrefs
supp_pattern = re.compile(r'href="([^"]*(?:mmc|suppl|appendix)[^"]*)"', re.I)
supp_links = supp_pattern.findall(html)
print(f"Supplement hrefs: {len(supp_links)}")
for l in supp_links[:20]:
    print(f"  {l}")

title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.DOTALL)
if title:
    print(f"Title: {title.group(1).strip()[:150]}")
