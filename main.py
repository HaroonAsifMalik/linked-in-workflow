"""
LinkedIn auto-poster (official API only).

Pipeline: queue/*.md (your own drafts, posted first)  OR  RSS feeds -> Gemini
          -> optional branded image card -> LinkedIn Posts API -> state/posted.json

Env vars:
  LINKEDIN_ACCESS_TOKEN   required to publish (scopes: openid profile w_member_social)
  GEMINI_API_KEY          required when generating from RSS
  LINKEDIN_PERSON_URN     optional, e.g. urn:li:person:AbC123 (otherwise looked up)
  LINKEDIN_TOKEN_CREATED  optional, YYYY-MM-DD, enables expiry warnings
  LINKEDIN_VERSION        optional, YYYYMM API version (default below)
  DRY_RUN                 "true" = print the post, publish nothing
Usage: python main.py [--dry-run]
"""
import datetime as dt
import html
import json
import os
import pathlib
import random
import re
import sys
import textwrap
import time

import ssl
import urllib.request

import certifi
import feedparser
import requests

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_HTTPS_HANDLER = urllib.request.HTTPSHandler(context=_SSL_CTX)

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state" / "posted.json"
QUEUE_DIR = ROOT / "queue"

LI_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
LI_VERSION = os.environ.get("LINKEDIN_VERSION") or "202608"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
DRY_RUN = "--dry-run" in sys.argv or os.environ.get("DRY_RUN", "").lower() == "true"


# ---------- helpers ----------
def log(msg):
    print(msg, flush=True)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"links": [], "history": []}


def save_state(state):
    state["links"] = state["links"][-500:]
    state["history"] = state["history"][-200:]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def check_token_age():
    created = os.environ.get("LINKEDIN_TOKEN_CREATED")
    if not created:
        return
    age = (dt.date.today() - dt.date.fromisoformat(created)).days
    if age >= 60:
        sys.exit(f"LinkedIn token is {age} days old and has expired. Regenerate it (see guide).")
    if age >= 50:
        # Shows as a yellow warning annotation on the GitHub Actions run
        log(f"::warning::LinkedIn token is {age} days old - regenerate it before day 60.")


# ---------- content sources ----------
def fetch_candidates(seen):
    items = []
    for url in CONFIG["rss_feeds"]:
        feed = feedparser.parse(url, agent="Mozilla/5.0 linkedin-autoposter", handlers=[_HTTPS_HANDLER])
        source = feed.feed.get("title", url)
        for e in feed.entries[:20]:
            link = e.get("link")
            if not link or link in seen:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            items.append({
                "title": strip_html(e.get("title", "")),
                "link": link,
                "summary": strip_html(e.get("summary", ""))[:1500],
                "source": source,
                "ts": time.mktime(ts) if ts else 0,
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    kws = [k.lower() for k in CONFIG.get("keywords", [])]
    if kws:  # prefer keyword matches, keep the rest as fallback
        items.sort(key=lambda x: not any(k in (x["title"] + x["summary"]).lower() for k in kws))
    return items


# ---------- AI writing (Gemini free tier) ----------
def gemini(prompt):
    if not GEMINI_KEY:
        sys.exit("GEMINI_API_KEY is not set.")
    errors = []
    for model in CONFIG["gemini_models"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.8}}
        for attempt in range(2):
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1))  # rate-limited / overloaded: back off
                continue
            if r.ok:
                parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if text:
                    return text
            errors.append(f"{model}: HTTP {r.status_code} {r.text[:200]}")
            break
        else:
            errors.append(f"{model}: still rate-limited after retries")
    raise RuntimeError("All Gemini models failed:\n" + "\n".join(errors))


_SHARED_PROMPT_HEADER = f"""You are Haroon Asif, a software engineer with 3+ years of experience in Python and Django, working on large-scale projects serving millions of users worldwide. You write daily LinkedIn posts about backend engineering.

Niche: {CONFIG['niche']}

Audience (pick the best fit for the topic):
- Junior to mid-level Python/Django developers: give practical tips and real lessons from production.
- Founders, CEOs, CTOs: explain technical ideas in business terms — cost, speed, reliability, user experience. No jargon.
- Tech leads and engineering managers: focus on team decisions, architecture trade-offs, hiring signals.

Voice: {CONFIG['voice']}"""


def generate_post(item, recent_titles):
    prompt = f"""{_SHARED_PROMPT_HEADER}

React to this article and write ONE LinkedIn post:
Title: {item['title']}
Source: {item['source']}
Summary: {item['summary']}

Rules:
- First line: one short punchy sentence that stops a developer mid-scroll. Opinionated or surprising. No "Did you know", no "I'm excited to share".
- Great hooks: a blunt trade-off ("X is fine until it isn't — here's when I switch"), a mistake framing ("Most devs do X wrong and it costs them"), a vs comparison, or a counterintuitive take.
- Body (2-3 short paragraphs): explain in plain words, give the real engineering or business trade-off, share a specific practical insight — like something you'd say to a colleague over coffee.
- If the topic fits a vs comparison or a "mistake I see" angle, use it.
- For founder-relevant topics, translate the technical impact into: what does this cost, how does it affect users, what breaks when you skip it.
- Only state facts present in the summary above; do not invent numbers, quotes, or names.
- Plain text only: no markdown, no asterisks, no headings. Short paragraphs separated by blank lines.
- 0-2 emojis maximum. End with one direct question people will actually want to answer. Last line: 3 lowercase hashtags.
- Under {CONFIG['max_post_chars']} characters. Do not include any URL.
- Avoid repeating the angle of these recent posts: {recent_titles or 'none'}.
Return only the post text."""
    return gemini(prompt)


def generate_evergreen_post(topic, recent_titles):
    prompt = f"""{_SHARED_PROMPT_HEADER}

Write ONE LinkedIn post on this topic: {topic}

Rules:
- First line: one short punchy sentence that stops a developer mid-scroll. Opinionated, direct. No "Did you know", no "I'm excited to share".
- Great hooks: a blunt trade-off, a mistake framing ("Most devs do X wrong"), a vs comparison, or a counterintuitive take from real production experience.
- Body (2-3 short paragraphs): share a concrete insight, explain the real trade-off, give a specific example a developer or founder would actually face. Write like you're talking to a colleague.
- Include a "what I actually do in production" or "what I've seen go wrong" moment.
- For founder-relevant topics, translate the technical impact into: cost, speed, reliability, user experience.
- Plain text only: no markdown, no asterisks, no headings. Short paragraphs separated by blank lines.
- 0-2 emojis maximum. End with one direct question people will actually want to answer. Last line: 3 lowercase hashtags.
- Under {CONFIG['max_post_chars']} characters. Do not include any URL.
- Avoid repeating the angle of these recent posts: {recent_titles or 'none'}.
Return only the post text."""
    return gemini(prompt)


def clean(text):
    text = re.sub(r"\*\*|__|^#+\s", "", text, flags=re.M)  # strip stray markdown
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


# ---------- image card (free, local, no API) ----------
def make_card(headline, path):
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1200, 627
    PAD_L = 90

    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)

    # Gradient background (top → bottom); falls back to flat if card_bg_end not set
    bg_s = _hex_rgb(CONFIG.get("card_bg_start", CONFIG["card_bg"]))
    bg_e = _hex_rgb(CONFIG.get("card_bg_end", CONFIG.get("card_bg_start", CONFIG["card_bg"])))
    for row in range(H):
        t = row / H
        r = int(bg_s[0] + (bg_e[0] - bg_s[0]) * t)
        g = int(bg_s[1] + (bg_e[1] - bg_s[1]) * t)
        b = int(bg_s[2] + (bg_e[2] - bg_s[2]) * t)
        d.line([(0, row), (W, row)], fill=(r, g, b))

    def font(size, bold=True):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        for p in (f"/usr/share/fonts/truetype/dejavu/{name}", name):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
        return ImageFont.load_default(size=size)

    accent = CONFIG["card_accent"]
    fg = CONFIG["card_fg"]

    # Left accent bar
    d.rectangle([0, 0, 12, H], fill=accent)

    # Headline — wrap tightly so each line reads like a punch
    lines = textwrap.wrap(headline, width=26)[:4]
    n = len(lines)
    size = 66 if n <= 2 else (54 if n == 3 else 46)
    f = font(size)
    line_h = size + 16
    block_h = n * line_h
    y = (H - block_h) // 2 - 28   # slightly above center
    for line in lines:
        d.text((PAD_L, y), line, font=f, fill=fg)
        y += line_h

    # Bottom separator + brand name
    sep_y = H - 90
    d.line([(PAD_L, sep_y), (W - PAD_L, sep_y)], fill=accent, width=2)
    d.text((PAD_L, sep_y + 16), CONFIG["brand_name"], font=font(28, bold=False), fill=accent)

    img.save(path, "PNG")
    return path


# ---------- LinkedIn official API ----------
def li_headers():
    return {
        "Authorization": f"Bearer {LI_TOKEN}",
        "LinkedIn-Version": LI_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def li_check(r, what):
    if r.status_code == 401:
        sys.exit(f"{what}: 401 Unauthorized - the LinkedIn token is invalid or expired. Regenerate it.")
    if r.status_code == 426 or "NONEXISTENT_VERSION" in r.text:
        sys.exit(f"{what}: API version {LI_VERSION} is not active. Set LINKEDIN_VERSION to a recent YYYYMM.")
    if not r.ok:
        sys.exit(f"{what}: HTTP {r.status_code} {r.text[:500]}")


def get_person_urn():
    urn = os.environ.get("LINKEDIN_PERSON_URN")
    if urn:
        return urn
    r = requests.get("https://api.linkedin.com/v2/userinfo",
                     headers={"Authorization": f"Bearer {LI_TOKEN}"}, timeout=30)
    li_check(r, "userinfo")
    return f"urn:li:person:{r.json()['sub']}"


def upload_image(owner, path):
    r = requests.post("https://api.linkedin.com/rest/images?action=initializeUpload",
                      headers=li_headers(), json={"initializeUploadRequest": {"owner": owner}}, timeout=30)
    li_check(r, "image init")
    value = r.json()["value"]
    with open(path, "rb") as fh:
        up = requests.put(value["uploadUrl"], data=fh,
                          headers={"Authorization": f"Bearer {LI_TOKEN}"}, timeout=120)
    li_check(up, "image upload")
    return value["image"]


def escape_little_text(s):
    """Posts API 'commentary' uses LinkedIn's little-text format: reserved characters
    must be backslash-escaped or the post gets silently truncated (e.g. at a parenthesis)."""
    s = re.sub(r"([\\|{}@\[\]()<>#*_~])", r"\\\1", s)
    return re.sub(r"\\#(\w+)", r"{hashtag|\\#|\1}", s)  # keep hashtags clickable


def create_post(owner, text, image_urn=None, alt_text=""):
    body = {
        "author": owner,
        "commentary": escape_little_text(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                         "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        body["content"] = {"media": {"id": image_urn, "altText": alt_text[:300]}}
    r = requests.post("https://api.linkedin.com/rest/posts", headers=li_headers(), json=body, timeout=30)
    li_check(r, "create post")
    return r.headers.get("x-restli-id", "")


# ---------- main ----------
def main():
    state = load_state()
    queue_file, link, title = None, None, ""
    recent = [h["title"] for h in state["history"][-5:]]

    queued = sorted(QUEUE_DIR.glob("*.md"))
    if queued:  # your own reviewed drafts always win
        queue_file = queued[0]
        text = clean(queue_file.read_text(encoding="utf-8").strip())
        title = text.splitlines()[0][:120]
        log(f"Using queued draft: {queue_file.name}")
    elif (
        CONFIG.get("evergreen_topics") and
        random.random() < CONFIG.get("evergreen_ratio", 0)
    ):
        used = {h.get("title", "") for h in state["history"][-15:]}
        pool = [t for t in CONFIG["evergreen_topics"] if t not in used] or CONFIG["evergreen_topics"]
        topic = random.choice(pool)
        log(f"Using evergreen topic: {topic}")
        text = generate_evergreen_post(topic, recent)
        title = topic
    else:
        items = fetch_candidates(set(state["links"]))
        if not items:
            log("No new items in feeds; nothing to post today.")
            return
        item = items[0]
        log(f"Picked: {item['title']} ({item['link']})")
        text = generate_post(item, recent)
        title, link = item["title"], item["link"]

    text = clean(text)
    if CONFIG.get("append_source_link") and link:
        text += f"\n\nSource: {link}"
    text = text[:2900]

    # Use the generated hook (first line) as the card headline — more engaging than the article title
    card_headline = text.splitlines()[0][:120]

    log("\n----- POST -----\n" + text + "\n----------------")
    if DRY_RUN:
        if CONFIG.get("image_mode") == "card":
            log(f"Image card preview saved: {make_card(card_headline, ROOT / 'preview.png')}")
        log("DRY RUN: nothing published.")
        return

    if not LI_TOKEN:
        sys.exit("LINKEDIN_ACCESS_TOKEN is not set.")
    check_token_age()
    owner = get_person_urn()

    image_urn = None
    if CONFIG.get("image_mode") == "card":
        image_urn = upload_image(owner, make_card(card_headline, ROOT / "card.png"))

    post_urn = create_post(owner, text, image_urn, alt_text=card_headline)
    log(f"Published: {post_urn}")

    if link:
        state["links"].append(link)
    state["history"].append({"date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                             "title": title, "urn": post_urn})
    save_state(state)

    if queue_file:
        done = QUEUE_DIR / "posted"
        done.mkdir(exist_ok=True)
        queue_file.rename(done / queue_file.name)


if __name__ == "__main__":
    main()
