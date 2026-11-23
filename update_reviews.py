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
# Full review texts are displayed; never cut them to a fixed length.
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
  const textNode = q('.business-review-view__body-text, .spoiler-view__text-container, [itemprop="reviewBody"]');
  const text = textNode && (textNode.innerText || textNode.textContent || '').trim() || '';
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
    """One scheduled visit; expand truncated reviews without bypassing SmartCaptcha."""
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
            page.wait_for_timeout(2500)
            if _looks_like_captcha(page.content(), page.url):
                raise CaptchaError("Yandex SmartCaptcha: defer until next daily attempt")
            try:
                page.wait_for_selector(".business-review-view", timeout=12000)
            except Exception:
                log.warning("Yandex reviews DOM not available; keeping saved cards")
            # A small bounded scroll is enough to load recent reviews.
            for _ in range(2):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(700)
            # Yandex sometimes folds long reviews behind 'Ещё' or 'Читать
            # полностью'. Only click those controls INSIDE review cards.
            # Do not follow external links or trigger any CAPTCHA challenges.
            more = page.locator(
                ".business-review-view button, "
                ".business-review-view [role='button'], "
                ".business-review-view a"
            )
            expanded = 0
            for i in range(min(more.count(), 100) - 1, -1, -1):
                try:
                    item = more.nth(i)
                    label = (item.inner_text(timeout=500) or "").strip().lower()
                    if (label in ("ещё", "еще", "читать полностью",
                                  "показать полностью", "развернуть",
                                  "читать далее", "показать ещё", "показать еще")
                            or "business-review-view__expand" in (item.get_attribute("class") or "")):
                        item.click(timeout=1200)
                        expanded += 1
                except Exception:
                    continue
            if expanded:
                page.wait_for_timeout(350)
            html = page.content()
            if _looks_like_captcha(html, page.url):
                raise CaptchaError("Yandex SmartCaptcha: defer until next daily attempt")
            plain_text = page.locator("body").inner_text(timeout=10000)
            reviews = page.evaluate(_REVIEWS_JS) or []
            log.info("Yandex review cards: %d loaded, %d expanded", len(reviews), expanded)
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
    """Only use the organisation's explicit rating, never a 5-star REVIEW's rating.

    Yandex pages carry each review's ratingValue as well as the business
    rating. A generic first-match search can accidentally choose 5.0.
    """
    # The organisation summary badge is authoritative if rendered.
    patterns = [
        r'business-summary-rating-badge-view__rating-text[^>]*>\s*([1-5](?:[.,]\d)?)\s*<',
        r'business-rating-badge-view__rating-text[^>]*>\s*([1-5](?:[.,]\d)?)\s*<',
        r'business-rating-badge-view__rating-number[^>]*>\s*([1-5](?:[.,]\d)?)\s*<',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            rating = float(match.group(1).replace(",", "."))
            log.info("Yandex organisation rating from header badge: %.1f", rating)
            return rating, None

    # Fallback only to schema entities explicitly describing this BUSINESS.
    # In particular, don't mistake a Review/Person's aggregateRating for it.
    for match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.I
    ):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entries_to_check = entry.get('@graph', [entry])
            for business in entries_to_check:
                if not isinstance(business, dict):
                    continue
                types = business.get('@type', [])
                types = [types] if isinstance(types, str) else types
                valid_type = any(t in (
                    'LocalBusiness', 'Organization', 'HairSalon', 'BarberShop',
                    'HealthAndBeautyBusiness'
                ) for t in types)
                name = str(business.get('name', '')).casefold()
                if not valid_type or 'княз' not in name:
                    continue
                aggregate = business.get('aggregateRating', {})
                try:
                    rating = float(str(aggregate.get('ratingValue', '')).replace(',', '.'))
                except (ValueError, TypeError, AttributeError):
                    continue
                if 1 <= rating <= 5:
                    log.info("Yandex organisation rating from named business schema: %.1f", rating)
                    return rating, None
    log.warning("No reliably identified business rating; keeping last confirmed display")
    return None, None

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
    # Preserve complete words and paragraphs (even for long reviews).
    t = str(t or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip().strip("«»\"").strip()


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
    # Yandex sometimes renders the newest publication dates relatively.
    today = datetime.now(timezone.utc).date()
    if value.startswith("сегодня"):
        return today.isoformat()
    if value.startswith("вчера"):
        from datetime import timedelta
        return (today - timedelta(days=1)).isoformat()
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
    if review_count is not None or rating_count is not None:
        parts = [plural_otzyv(review_count)] if review_count is not None else []
        if rating_count is not None and (review_count is None or rating_count != review_count):
            parts.append(f"{rating_count} оценок")
        parts.append("Яндекс Карты")
        label = " · ".join(parts)
        content = re.sub(
            r'(<span class="rb-count"[^>]*>).*?(</span>)',
            lambda m: m.group(1) + label + m.group(2), content, count=1,
        )
        # The old design sometimes has a second rating tile.
        if review_count is not None:
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
    # A parsed count must be consistent with the reviews actually loaded.
    # Star ratings may exceed written reviews, but not vice versa.
    if rating_count is not None and review_count is not None and rating_count < review_count:
        log.warning("Yandex count mismatch: %s written reviews > %s ratings", review_count, rating_count)
        review_count = rating_count = None
    if review_count is not None and review_count < len(raw_reviews):
        log.warning("Yandex count below loaded review cards; keeping previous count")
        review_count = None
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
