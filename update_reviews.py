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
from datetime import datetime, timezone

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
  const dateNode = q('meta[itemprop="datePublished"], time[datetime], .business-review-view__date');
  const date = dateNode && (dateNode.getAttribute('content') || dateNode.getAttribute('datetime') || txt(dateNode)) || '';
  return {author, text, rating, date};
})
"""


def fetch_playwright(url):
    """One daily browser session; page and review text returned without extra retries."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="ru-RU",
                viewport={"width": 1366, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url.rstrip("/") + "/reviews/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            if _looks_like_captcha(page.content(), page.url):
                raise CaptchaError("Yandex SmartCaptcha: defer until next scheduled attempt")
            try:
                page.wait_for_selector(".business-review-view", timeout=12000)
            except Exception:
                log.warning("Yandex reviews DOM not available; retaining saved cards")
            # Lazy-load the most recent visible review cards without crawling all pages.
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(900)
            html, plain_text = page.content(), page.locator("body").inner_text(timeout=10000)
            if _looks_like_captcha(html, page.url):
                raise CaptchaError("Yandex SmartCaptcha: defer until next scheduled attempt")
            reviews = page.evaluate(_REVIEWS_JS) or []
            return html, reviews, plain_text
        finally:
            browser.close()


def fetch_urllib(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
        final_url = resp.geturl()
    if _looks_like_captcha(html, final_url):
        raise CaptchaError("Yandex SmartCaptcha")
    return html, [], re.sub(r"<[^>]*>", " ", html)


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

    # Rating is useful even if Yandex did not expose the text-review count.
    return rating, count


def parse_review_and_rating_counts(html, visible_text=""):
    """Never label number of ratings as number of written reviews."""
    text = re.sub(r"[\u00a0\u202f]", " ", visible_text or "")
    # Yandex review tab: separate visible counts for texts and star-only ratings.
    reviews_match = (
        re.search(r"(?:^|[^\w])Отзывы\s*[:·]?\s*(\d{1,6})\b", text, re.I)
        or re.search(r"\b(\d{1,6})\s+отзыв(?:ов|а)?\b", text, re.I)
    )
    ratings_match = re.search(r"\b(\d{1,6})\s+оцен(?:ка|ки|ок)\b", text, re.I)
    # Structured organization data can also carry both independent values.
    if not reviews_match:
        reviews_match = re.search(r'"reviewCount"\s*:\s*"?(\d{1,6})', html)
    if not ratings_match:
        ratings_match = re.search(r'"ratingCount"\s*:\s*"?(\d{1,6})', html)
    review_count = int(reviews_match.group(1)) if reviews_match else None
    rating_count = int(ratings_match.group(1)) if ratings_match else None
    return review_count, rating_count


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


_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _date_key(value):
    """ISO and visible Russian publication dates; unknown dates stay at the end."""
    value = str(value or "").strip().lower()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    if m:
        return m.group(1)
    m = re.search(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?", value)
    if not m:
        return ""
    day = int(m.group(1))
    month = _RU_MONTHS[m.group(2)]
    now = datetime.now(timezone.utc)
    year = int(m.group(3)) if m.group(3) else now.year
    if not m.group(3) and (month, day) > (now.month, now.day):
        year -= 1
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def select_reviews(raw):
    seen, out = set(), []
    for r in raw:
        txt = _clean_text(r.get("text"))
        score = float(r.get("rating", 0) or 0)
        if score < REVIEW_MIN_STAR or len(txt) < REVIEW_MIN_LEN:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        date = str(r.get("date") or "")
        out.append({"author": _short_author(r.get("author")), "text": txt,
                    "rating": min(5, max(1, int(round(score)))), "date": date,
                    "_date_key": _date_key(date)})
    # Select the latest six among everything loaded, not the first six before sorting.
    if any(r["_date_key"] for r in out):
        out.sort(key=lambda r: r["_date_key"], reverse=True)
    return out[:REVIEWS_LIMIT]


def render_cards(reviews):
    esc = lambda x: htmllib.escape(x, quote=False)
    cards = []
    for r in reviews:
        cards.append(
            '      <div class="review-card">\n'
            '        <div class="review-stars">' + '★' * min(5, max(1, r["rating"])) + '☆' * max(0, 5 - r["rating"]) + '</div>\n'
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


def update_html(content, rating, review_count, rating_count=None):
    """Change only metrics confirmed during the same successful Yandex fetch."""
    original = content
    if rating is not None and 1 <= rating <= 5:
        dot = f"{rating:.1f}"
        comma = dot.replace(".", ",")
        for cls, value in (("br-score", dot), ("rb-score", comma),
                           ("about-rating-score", dot)):
            content = re.sub(
                rf'(<(?:span|div) class="{cls}"[^>]*>).*?(</(?:span|div)>)',
                lambda m: m.group(1) + value + m.group(2), content, count=1,
            )
    if review_count is not None:
        parts = [plural_otzyv(review_count)]
        if rating_count is not None and rating_count != review_count:
            parts.append(f"{rating_count} оценок")
        parts.append("Яндекс Карты")
        label = " · ".join(parts)
        content = re.sub(
            r'(<span class="rb-count"[^>]*>).*?(</span>)',
            lambda m: m.group(1) + label + m.group(2), content, count=1,
        )
        # The old design sometimes has a second rating tile.
        content = re.sub(
            r'(<div class="br-count"[^>]*>).*?(</div>)',
            lambda m: m.group(1) + plural_otzyv(review_count) + m.group(2),
            content, count=1,
        )
    return content, content != original

# ── Main entry ───────────────────────────────────────────────────────────────

def sync():
    """One best-effort attempt; keep last saved reviews when Yandex blocks automation."""
    # The public reviews page is the only source for these metrics.
    url = ORG_URLS[0]
    try:
        page, raw_reviews, text = fetch_playwright(url)
    except CaptchaError as exc:
        log.warning("Yandex review sync deferred: %s", exc)
        return {"ok": False, "changed": False, "error": "captcha"}
    except Exception as exc:
        log.warning("Yandex reviews unavailable: %s", type(exc).__name__)
        return {"ok": False, "changed": False, "error": type(exc).__name__}

    rating, _ = parse_rating_count(page)
    review_count, rating_count = parse_review_and_rating_counts(page, text)
    reviews = select_reviews(raw_reviews)
    # No reliable count and too few actual reviews = do not replace the site
    # with empty or inaccurate data.
    if review_count is None and len(reviews) < 3:
        return {"ok": False, "changed": False, "error": "no reliable review data"}

    log.info("Yandex: rating=%s, text reviews=%s, ratings=%s, cards=%d",
             rating, review_count, rating_count, len(reviews))
    with open(HTML_FILE, encoding="utf-8") as f:
        content = f.read()
    new_content, _ = update_html(content, rating, review_count, rating_count)
    new_content = update_reviews_block(new_content, reviews)
    changed = new_content != content
    if changed:
        with open(HTML_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
    return {"ok": True, "changed": changed, "rating": rating,
            "review_count": review_count, "rating_count": rating_count,
            "texts_scraped": len(raw_reviews), "texts_used": len(reviews)}


def main():
    res = sync()
    print(res)
    return res


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
