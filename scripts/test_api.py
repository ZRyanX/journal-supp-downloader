#!/usr/bin/env python3
"""Explore Elsevier API 'objects' field for supplement download URLs."""
import requests
import json

api_key = "66e68474293c31b16c0e4f5f7e092bf0"
doi = "10.1016/j.oregeorev.2026.107349"

r = requests.get(
    f"https://api.elsevier.com/content/article/doi/{doi}",
    headers={"X-ELS-APIKey": api_key, "Accept": "application/json"},
    timeout=30,
)

data = r.json()
article = data["full-text-retrieval-response"]

# Check 'objects' field
objects = article.get("objects", {})
print(f"=== Objects ===")
print(f"Type: {type(objects)}")
if isinstance(objects, dict):
    print(json.dumps(objects, indent=2, ensure_ascii=False)[:3000])
elif isinstance(objects, list):
    for obj in objects[:10]:
        print(json.dumps(obj, indent=2, ensure_ascii=False)[:500])
        print("---")

# Also check coredata links
print("\n=== Coredata Links ===")
core = article.get("coredata", {})
links = core.get("link", [])
for link in links:
    print(f"  {link.get('@rel', 'N/A')}: {link.get('@href', 'N/A')[:120]}")

# Check if originalText has embedded object references
import re
orig = article.get("originalText", "")
# Find e-component references
ecomp = re.findall(r'e-component[^"]*"[^"]*"', orig[:5000])
if ecomp:
    print(f"\n=== e-component refs ===")
    for e in ecomp[:10]:
        print(f"  {e}")

# Find mmc attachment patterns
att_pattern = re.findall(r'<ce:e-component[^>]*>.*?</ce:e-component>', orig, re.DOTALL)
if att_pattern:
    print(f"\n=== ce:e-component elements ({len(att_pattern)}) ===")
    for a in att_pattern[:10]:
        print(f"  {a[:200]}")
        print()
