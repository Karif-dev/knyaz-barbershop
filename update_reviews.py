#!/usr/bin/env python3
"""
update_reviews.py
Fetches the current rating and review count for КНЯЗЬ barbershop
from Yandex Maps and updates all occurrences in index.html.
"""

import re
import sys
import json
import urllib.request
import ssl

ORG_URL  = "https://yandex.com/maps/org/knyaz/145439345097/"
HTML_FILE = "index.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch_page(url):
    ctx = ssl.create_default_context()
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, context=ctx, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_rating_count(html):
    # 1. JSON-LD structured data (schema.org)
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    ):
        try:
            data = json.loads(m.group(1))
            for item in (data if isinstance(data, list) else [data]):
                agg = item.get("aggregateRating", {})
                if agg.get("ratingValue") and agg.get("reviewCount"):
                    return float(str(agg["ratingValue"])), int(str(agg["reviewCount"]).replace(" ", ""))
        except Exception:
            pass

    # 2. Embedded JS state blobs
    m = re.search(r'"rating"\s*:\s*\{[^}]*"value"\s*:\s*([\d.]+)[^}]*"count"\s*:\s*(\d+)', html)
    if m:
        return float(m.group(1)), int(m.group(2))

    # 3. Plain attribute patterns
    rating = count = None
    m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', html)
    if m:
        rating = float(m.group(1))
    m = re.search(r'"reviewCount"\s*:\s*(\d+)', html)
    if m:
        count = int(m.group(1))
    if rating and count:
        return rating, count

    return None, None


def plural_otzyv(n):
    """Russian grammatical form of 'отзыв'."""
    if 11 <= n % 100 <= 14:
        return f"{n} отзывов"
    r = n % 10
    if r == 1:
        return f"{n} отзыв"
    if 2 <= r <= 4:
        return f"{n} отзыва"
    return f"{n} отзывов"


def update_html(content, rating, count):
    rating_dot   = f"{rating:.1f}"             # "5.0"  — used in .br-score
    rating_comma = rating_dot.replace(".", ",") # "5,0"  — used in .rb-score
    count_str    = plural_otzyv(count)

    changed = False

    # .br-score  (bento block)
    new, n = re.subn(
        r'(<div class="br-score">)[\d.,]+(</div>)',
        rf'\g<1>{rating_dot}\2', content
    )
    if n: changed = True; content = new

    # .br-count  (bento block)
    new, n = re.subn(
        r'(<div class="br-count">)\d+\s+отзыв[а-я]*(</div>)',
        rf'\g<1>{count_str}\2', content
    )
    if n: changed = True; content = new

    # .rb-score  (reviews section)
    new, n = re.subn(
        r'(<span class="rb-score">)[\d.,]+(</span>)',
        rf'\g<1>{rating_comma}\2', content
    )
    if n: changed = True; content = new

    # .rb-count  (reviews section)
    new, n = re.subn(
        r'(<span class="rb-count">)\d+\s+отзыв[а-я]*\s+·\s+Яндекс Карты(</span>)',
        rf'\g<1>{count_str} · Яндекс Карты\2', content
    )
    if n: changed = True; content = new

    return content, changed


def main():
    print(f"Fetching {ORG_URL} ...")
    try:
        html = fetch_page(ORG_URL)
    except Exception as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        sys.exit(0)   # non-fatal — don't break CI

    rating, count = parse_rating_count(html)
    if rating is None or count is None:
        print("Could not extract rating/count from page. Skipping.", file=sys.stderr)
        sys.exit(0)

    print(f"Yandex → rating={rating}, reviews={count}")

    with open(HTML_FILE, encoding="utf-8") as f:
        content = f.read()

    new_content, changed = update_html(content, rating, count)

    if not changed:
        print("index.html already up to date — nothing to commit.")
        sys.exit(0)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"index.html updated: {rating} / {count} reviews")


if __name__ == "__main__":
    main()
