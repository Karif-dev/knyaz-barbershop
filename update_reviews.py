#!/usr/bin/env python3
"""
update_reviews.py
Fetches the current rating and review count for КНЯЗЬ barbershop
from Yandex Maps and updates all occurrences in index.html.

Yandex отдаёт обычному urllib капчу / пустую оболочку без данных,
поэтому основной способ — headless-браузер (Playwright), urllib — запасной.
"""

import os
import re
import sys
import json
import ssl
import logging
import html as htmllib
import urllib.request

log = logging.getLogger(__name__)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "index.html")

ORG_URLS = [
    "https://yandex.ru/maps/org/knyaz/145439345097/",
    "https://yandex.com/maps/org/knyaz/145439345097/",
]

REVIEWS_LIMIT   = 6    # сколько карточек показывать на сайте
REVIEW_MIN_STAR = 4    # на сайт берём только 4-5★
REVIEW_MIN_LEN  = 40   # слишком короткие ("Супер!") пропускаем
REVIEW_MAX_LEN  = 300  # длинные обрезаем по границе слова
MARK_START = "<!--REVIEWS-START-->"
MARK_END   = "<!--REVIEWS-END-->"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class CaptchaError(Exception):
    pass


def _looks_like_captcha(html, url=""):
    low = html.lower()
    return ("showcaptcha" in url.lower() or "smartcaptcha" in low
            or "checkbox-captcha" in low or "вы не робот" in low)


# ── Fetching ─────────────────────────────────────────────────────────────────

_REVIEWS_JS = """
() => Array.from(document.querySelectorAll('.business-review-view')).map(el => {
  const q = s => el.querySelector(s);
  const txt = n => (n && n.textContent || '').trim();
  const author = txt(q('.business-review-view__author-name, [itemprop="author"] [itemprop="name"], .business-review-view__author span'));
  const text = txt(q('.business-review-view__body-text, .spoiler-view__text-container, [itemprop="reviewBody"]'));
  let rating = 0;
  const meta = q('[itemprop="reviewRating"] meta[itemprop="ratingValue"]');
  if (meta) rating = parseFloat(meta.content) || 0;
  if (!rating) rating = el.querySelectorAll('.business-rating-badge-view__star._full').length;
  if (!rating) {
    const a = q('[aria-label*="Оценка"]');
    const m = a && a.getAttribute('aria-label').match(/(\\d)/);
    if (m) rating = parseInt(m[1], 10);
  }
  return {author, text, rating};
})
"""


def fetch_playwright(url):
    """Returns (main_page_html, reviews_list). reviews_list may be empty."""
    from playwright.sync_api import sync_playwright
    reviews = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ])
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="ru-RU",
                viewport={"width": 1366, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(
                    ".business-summary-rating-badge-view__rating-text, "
                    ".business-rating-amount-view",
                    timeout=20000,
                )
            except Exception:
                page.wait_for_timeout(4000)
            html, final_url = page.content(), page.url
            if _looks_like_captcha(html, final_url):
                raise CaptchaError("Yandex SmartCaptcha")

            # Тексты отзывов — на отдельной вкладке /reviews/
            try:
                page.goto(url.rstrip("/") + "/reviews/",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector(".business-review-view", timeout=20000)
                page.mouse.wheel(0, 3000)          # подгрузить ещё немного
                page.wait_for_timeout(1500)
                reviews = page.evaluate(_REVIEWS_JS) or []
            except Exception as e:
                log.warning("Reviews tab scrape failed: %s", e)
        finally:
            browser.close()
    return html, reviews


def fetch_urllib(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
        final_url = resp.geturl()
    if _looks_like_captcha(html, final_url):
        raise CaptchaError("Yandex SmartCaptcha")
    return html, []


# ── Parsing ──────────────────────────────────────────────────────────────────

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
                    return (float(str(agg["ratingValue"]).replace(",", ".")),
                            int(str(agg["reviewCount"]).replace(" ", "")))
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

    # 4. Rendered DOM (what a real browser sees)
    m = re.search(
        r'business-summary-rating-badge-view__rating-text[^>]*>\s*([\d.,]+)\s*<', html)
    if m:
        rating = float(m.group(1).replace(",", "."))
    # tab "Отзывы 44" is preferred, rating-amount ("44 оценки") is the fallback
    m = (re.search(r'Отзывы\s*(?:<[^>]*>\s*)*(\d+)\s*<', html)
         or re.search(r'business-rating-amount-view[^>]*>\s*(\d+)', html)
         or re.search(r'(\d+)\s+(?:отзыв|оцен)', html))
    if m:
        count = int(m.group(1))
    if rating and count:
        return rating, count

    return None, None


def parse_reviews_ld(html):
    """Fallback: schema.org Review entries from JSON-LD (if Yandex embeds any)."""
    out = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            for r in item.get("review", []) or []:
                try:
                    out.append({
                        "author": (r.get("author") or {}).get("name", ""),
                        "text": r.get("reviewBody", ""),
                        "rating": float((r.get("reviewRating") or {}).get("ratingValue", 0)),
                    })
                except Exception:
                    pass
    return out


def _clean_text(t):
    t = re.sub(r"\s+", " ", t or "").strip().strip("«»\"").strip()
    if len(t) > REVIEW_MAX_LEN:
        t = t[:REVIEW_MAX_LEN].rsplit(" ", 1)[0].rstrip(" ,;:-—") + "…"
    return t


def _short_author(name):
    """'Иван Петров' -> 'Иван П.' (как в текущих карточках)."""
    name = re.sub(r"\s+", " ", name or "").strip()
    parts = name.split(" ")
    if len(parts) >= 2 and len(parts[1]) > 1 and parts[1][0].isalpha():
        return f"{parts[0]} {parts[1][0].upper()}."
    return name or "Гость"


def select_reviews(raw):
    seen, out = set(), []
    for r in raw:
        text = _clean_text(r.get("text"))
        if r.get("rating", 0) < REVIEW_MIN_STAR or len(text) < REVIEW_MIN_LEN:
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append({"author": _short_author(r.get("author")), "text": text})
        if len(out) >= REVIEWS_LIMIT:
            break
    return out


def render_cards(reviews):
    esc = lambda x: htmllib.escape(x, quote=False)
    cards = []
    for r in reviews:
        cards.append(
            '      <div class="review-card">\n'
            '        <div class="review-stars">★★★★★</div>\n'
            f'        <p class="review-text">«{esc(r["text"])}»</p>\n'
            '        <div class="review-footer">\n'
            f'          <div class="review-author">{esc(r["author"])}</div>\n'
            '          <div class="review-source">Яндекс Карты</div>\n'
            '        </div>\n'
            '      </div>'
        )
    return "\n".join(cards)


def update_reviews_block(content, reviews):
    """Replace cards between the markers. Needs >=3 good reviews, else keeps old ones."""
    if len(reviews) < 3:
        return content
    pat = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.DOTALL)
    if not pat.search(content):
        log.warning("Reviews markers not found in index.html — cards not updated")
        return content
    block = f"{MARK_START}\n{render_cards(reviews)}\n      {MARK_END}"
    return pat.sub(lambda m: block, content, count=1)


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
    rating_dot   = f"{rating:.1f}"              # "5.0"  — .br-score
    rating_comma = rating_dot.replace(".", ",")  # "5,0"  — .rb-score
    count_str    = plural_otzyv(count)
    original = content

    content = re.sub(r'(<div class="br-score">)[\d.,]+(</div>)',
                     rf'\g<1>{rating_dot}\2', content)
    content = re.sub(r'(<div class="br-count">)\d+\s+отзыв[а-я]*(</div>)',
                     rf'\g<1>{count_str}\2', content)
    content = re.sub(r'(<span class="rb-score">)[\d.,]+(</span>)',
                     rf'\g<1>{rating_comma}\2', content)
    content = re.sub(
        r'(<span class="rb-count">)\d+\s+отзыв[а-я]*\s+·\s+Яндекс Карты(</span>)',
        rf'\g<1>{count_str} · Яндекс Карты\2', content)

    # changed = реально изменилось (а не просто "паттерн нашёлся")
    return content, content != original


# ── Main entry ───────────────────────────────────────────────────────────────

def sync():
    """Never raises, never calls sys.exit (safe inside a background thread)."""
    html, raw_reviews, errors = None, [], []
    for url in ORG_URLS:
        for name, fetch in (("playwright", fetch_playwright), ("urllib", fetch_urllib)):
            try:
                page, reviews = fetch(url)
            except Exception as e:
                errors.append(f"{name} {url}: {e}")
                log.warning("Reviews fetch via %s failed (%s): %s", name, url, e)
                continue
            rating, count = parse_rating_count(page)
            if rating and count:
                html, raw_reviews = page, reviews or parse_reviews_ld(page)
                break
            errors.append(f"{name} {url}: rating/count not found in page")
            log.warning("Reviews: %s got page (%d bytes) but no rating/count", name, len(page))
        if html:
            break

    if not html:
        return {"ok": False, "changed": False, "error": "; ".join(errors)}

    reviews = select_reviews(raw_reviews)
    log.info("Yandex → rating=%s, reviews=%s, texts scraped=%d, texts used=%d",
             rating, count, len(raw_reviews), len(reviews))
    with open(HTML_FILE, encoding="utf-8") as f:
        content = f.read()
    new_content, changed = update_html(content, rating, count)
    new_content = update_reviews_block(new_content, reviews)
    changed = new_content != content
    if changed:
        with open(HTML_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
    return {"ok": True, "changed": changed, "rating": rating, "count": count,
            "texts_scraped": len(raw_reviews), "texts_used": len(reviews)}


def main():
    res = sync()
    print(res)
    return res


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
