"""Phoenix Career Radar: twice-weekly email for Elizabeth.

Flow (built for low cost):
1. Free: pull recent headlines from industry RSS feeds.
2. One Claude Haiku call: web search (capped) for Phoenix-area events,
   then returns JSON with trends, events and career strategy.
3. Render a pink-and-blue HTML email and send it via Gmail SMTP.
"""
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import feedparser

ROOT = Path(__file__).parent
PHX = ZoneInfo("America/Phoenix")
MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
MAX_SEARCHES = int(os.environ.get("MAX_SEARCHES", "3"))
SEEN_FILE = ROOT / "seen.json"

FEEDS = [
    "https://breakingdefense.com/feed/",
    "https://spacenews.com/feed/",
    "https://defensescoop.com/feed/",
    "https://www.satellitetoday.com/feed/",
    "https://www.airandspaceforces.com/feed/",
]

PINK = "#EC4899"
PINK_LIGHT = "#FCE7F3"
BLUE = "#2563EB"
BLUE_DARK = "#1E3A8A"
BLUE_LIGHT = "#DBEAFE"


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required env var: {name}")
    return val


def load_seen():
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"events": [], "trends": []}


def save_seen(seen, result):
    seen["events"] = (seen["events"] + [e.get("name", "") for e in result.get("events", [])])[-40:]
    seen["trends"] = (seen["trends"] + [t.get("title", "") for t in result.get("trends", [])])[-40:]
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def fetch_headlines(days=5, per_feed=6):
    cutoff = time.time() - days * 86400
    lines = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        except Exception as exc:  # network or parse failure: skip feed
            print(f"feed failed {url}: {exc}")
            continue
        count = 0
        for e in feed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts and time.mktime(ts) < cutoff:
                continue
            summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))[:180]
            lines.append(f"- {e.get('title', '').strip()} | {summary} | {e.get('link', '')}")
            count += 1
            if count >= per_feed:
                break
        print(f"{url}: {count} items")
    return "\n".join(lines) or "(no headlines retrieved)"


SYSTEM = """You are a career intelligence analyst for one person (profile below).
Return ONLY a JSON object, no prose, no markdown fences.

Schema:
{
 "subject_hook": "max 8 words summarizing the biggest theme",
 "trends": [ {"title": "", "what_happened": "1-2 sentences", "why_it_matters": "1 sentence tied to her skills", "source_url": ""} ],
 "events": [ {"name": "", "date": "YYYY-MM-DD", "time": "", "venue_city": "", "url": "", "why_go": "1 sentence", "cost": "Free/paid/unknown"} ],
 "events_note": "one sentence, only if fewer than 3 verified events",
 "career_strategy": {
   "headline": "one sentence",
   "moves": [ {"action": "", "why": "tied to a trend above", "timeframe": "this week / 30 days / 90 days"} ],
   "target_employers": ["only Arizona-area employers with evidence of relevant hiring or news"]
 }
}

Rules:
- 3 to 4 trends, from the provided headlines plus search. Skip anything in ALREADY_SENT.
- Events: use web search. Only Phoenix metro (Phoenix, Tempe, Scottsdale, Chandler, Mesa, Gilbert, Glendale) in the next 45 days. Aerospace, defense, space, RF/satcom, systems engineering, STEM leadership, networking, career fairs, clearance-friendly hiring events. Every event MUST come from a search result you actually saw, with its real URL and date. Never invent or guess. Fewer verified events is better than any unverified one. Skip anything in ALREADY_SENT.
- Career strategy: 3 concrete moves tied to the trends. She is a systems/electrical engineer currently based in California and wants to move to Arizona, so include relocation-aware moves. Do not assume a security clearance or compensation. Be specific, no generic advice.
- Short, plain, no hype."""


def call_claude(profile, headlines, seen):
    client = anthropic.Anthropic()
    today = datetime.now(PHX).strftime("%A %Y-%m-%d")
    user = (
        f"TODAY: {today}\n\nPROFILE:\n{profile}\n\n"
        f"ALREADY_SENT events: {seen['events']}\nALREADY_SENT trends: {seen['trends']}\n\n"
        f"RECENT HEADLINES (title | summary | link):\n{headlines}"
    )
    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": MAX_SEARCHES,
        "user_location": {
            "type": "approximate", "city": "Phoenix", "region": "Arizona",
            "country": "US", "timezone": "America/Phoenix",
        },
    }]
    for attempt in range(2):
        messages = [{"role": "user", "content": user}]
        blocks = []  # every content block across pause_turn continuations
        for _ in range(5):
            resp = client.messages.create(
                model=MODEL, max_tokens=10000, system=SYSTEM, tools=tools, messages=messages
            )
            blocks += list(resp.content)
            print(f"tokens in={resp.usage.input_tokens} out={resp.usage.output_tokens} stop={resp.stop_reason}")
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})

        # Keep only text written AFTER the last search result (drops "let me search..." narration)
        text = ""
        for b in blocks:
            if b.type == "web_search_tool_result":
                text = ""
            elif b.type == "text":
                text += b.text

        if resp.stop_reason == "max_tokens":
            print("Output truncated; retrying with a shorter-output request")
        else:
            start = text.find("{")
            if start != -1:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(text[start:])
                    return obj
                except json.JSONDecodeError as exc:
                    print(f"JSON parse failed: {exc}")
        user += "\n\nIMPORTANT: Keep every field very short. Max 3 trends and 4 events. No narration before or after the JSON."
    raise ValueError(f"No valid JSON after retries. Last text: {text[:300]}")


def clean_events(events):
    """Drop events without a URL, with a bad date, or already past."""
    today = datetime.now(PHX).date()
    keep = []
    for e in events:
        url = e.get("url", "")
        try:
            d = datetime.strptime(e.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if url.startswith("http") and today <= d <= today + timedelta(days=60):
            keep.append(e)
    return sorted(keep, key=lambda e: e["date"])[:5]


def link(url, label):
    return f'<a href="{escape(url, quote=True)}" style="color:{BLUE};font-weight:600;text-decoration:none">{escape(label)}</a>'


def render(result, events):
    today = datetime.now(PHX).strftime("%B %-d, %Y")
    trend_html = ""
    for t in result.get("trends", []):
        src = link(t["source_url"], "Source →") if str(t.get("source_url", "")).startswith("http") else ""
        trend_html += f"""
        <div style="border-left:5px solid {PINK};background:{PINK_LIGHT};padding:12px 16px;margin:12px 0;border-radius:6px">
          <div style="font-weight:700;color:{BLUE_DARK};font-size:16px">{escape(t.get('title',''))}</div>
          <div style="margin:6px 0;color:#333">{escape(t.get('what_happened',''))}</div>
          <div style="color:{BLUE_DARK}"><b>Why it matters:</b> {escape(t.get('why_it_matters',''))}</div>
          <div style="margin-top:6px">{src}</div>
        </div>"""

    if events:
        event_html = ""
        for e in events:
            d = datetime.strptime(e["date"], "%Y-%m-%d").strftime("%a, %b %-d")
            event_html += f"""
            <div style="border-left:5px solid {BLUE};background:{BLUE_LIGHT};padding:12px 16px;margin:12px 0;border-radius:6px">
              <div style="font-weight:700;color:{BLUE_DARK};font-size:16px">{link(e['url'], e.get('name',''))}</div>
              <div style="color:{PINK};font-weight:700;margin:4px 0">{escape(d)} {escape(e.get('time',''))} · {escape(e.get('venue_city',''))} · {escape(e.get('cost',''))}</div>
              <div style="color:#333">{escape(e.get('why_go',''))}</div>
            </div>"""
    else:
        event_html = f'<p style="color:#555">No verified Phoenix-area events found this round.</p>'
    if result.get("events_note"):
        event_html += f'<p style="color:#555;font-size:13px">{escape(result["events_note"])}</p>'

    cs = result.get("career_strategy", {})
    moves = ""
    for i, m in enumerate(cs.get("moves", []), 1):
        moves += f"""
        <tr>
          <td style="vertical-align:top;padding:8px 10px 8px 0"><div style="background:{PINK};color:#fff;border-radius:50%;width:28px;height:28px;line-height:28px;text-align:center;font-weight:700">{i}</div></td>
          <td style="padding:8px 0;color:#333"><b style="color:{BLUE_DARK}">{escape(m.get('action',''))}</b><br>
            {escape(m.get('why',''))}<br>
            <span style="color:{PINK};font-weight:700;font-size:13px">{escape(m.get('timeframe','').upper())}</span></td>
        </tr>"""
    employers = ""
    if cs.get("target_employers"):
        chips = "".join(
            f'<span style="display:inline-block;background:{BLUE};color:#fff;padding:4px 10px;border-radius:12px;margin:3px 4px 3px 0;font-size:13px">{escape(x)}</span>'
            for x in cs["target_employers"]
        )
        employers = f'<div style="margin-top:10px"><b style="color:{BLUE_DARK}">Watch list:</b><br>{chips}</div>'

    def h2(text, color):
        return f'<h2 style="color:{color};font-size:18px;border-bottom:3px solid {color};padding-bottom:4px;margin:28px 0 8px">{text}</h2>'

    return f"""<html><body style="margin:0;background:#f5f7ff;font-family:-apple-system,Segoe UI,Arial,sans-serif">
<div style="max-width:640px;margin:0 auto;background:#fff">
  <div style="background:linear-gradient(90deg,{BLUE},{PINK});padding:24px;color:#fff">
    <div style="font-size:24px;font-weight:800">Phoenix Career Radar</div>
    <div style="opacity:.95">{escape(result.get('subject_hook',''))} · {today}</div>
  </div>
  <div style="padding:8px 24px 24px">
    {h2('Industry trends', PINK)}{trend_html}
    {h2('Events near Phoenix', BLUE)}{event_html}
    {h2('Your career strategy', PINK)}
    <p style="color:{BLUE_DARK};font-size:16px;font-weight:600">{escape(cs.get('headline',''))}</p>
    <table style="width:100%;border-collapse:collapse">{moves}</table>{employers}
  </div>
  <div style="background:{BLUE_DARK};color:#BFDBFE;padding:12px 24px;font-size:12px">
    Verify event details on the organizer's page before attending.
  </div>
</div></body></html>"""


def wait_until_10am():
    """Cron fires early; hold the send until 10:00 Phoenix (max 45 min wait)."""
    now = datetime.now(PHX)
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    secs = (target - now).total_seconds()
    if 0 < secs <= 45 * 60:
        print(f"Waiting {int(secs)}s until 10:00 Phoenix")
        time.sleep(secs)


def send_email(subject, html):
    user = env("GMAIL_USER", required=True)
    pw = env("GMAIL_APP_PASSWORD", required=True)
    to = [a.strip() for a in env("TO_EMAIL", required=True).split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, f"Phoenix Career Radar <{user}>", ", ".join(to)
    msg.attach(MIMEText("Open this email in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())


def main():
    profile = (ROOT / "profile.md").read_text()
    seen = load_seen()
    result = call_claude(profile, fetch_headlines(), seen)
    result["events"] = clean_events(result.get("events", []))
    html = render(result, result["events"])
    (ROOT / "last_email.html").write_text(html)
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        wait_until_10am()
    send_email(f"Phoenix Career Radar: {result.get('subject_hook', 'Your update')}", html)
    save_seen(seen, result)
    print("Sent.")


if __name__ == "__main__":
    main()
