"""Bing Rewards conservative automation — Edge edition.

First run: `python bing_rewards.py --login`   # opens Edge, you sign in once, auth.json saved
Daily:     `python bing_rewards.py`           # headless, runs visible still-earnable tasks

Handled task families (auto-discovered from the dashboard each run):
  - "Explore on Bing" category cards   : visible card click -> search topical keyword -> verify
  - Daily set / More activities links  : visible card click -> play/open -> verify
  - Bing Image Creator daily           : generate one image
  - Multi-question quizzes             : click the correct option (url has WQSCORE:1) per question
  - Image "Puzzle it"                  : click Skip (credits on skip)
  - PC/Mobile search quota             : small typed batches only when quota is readable
  - Extra search bonus                 : opt-in with --search-bonus, one search then verify

Auto-skips (with a logged reason) — flip them back on only if MS enables them for your market:
  - Cards marked "Offer is Locked" / "Available tomorrow" / "Earn -1 points"
  - Long-running punch cards (Sea of Thieves, etc.)
  - 3rd-party installs (Chrome, Edge mobile app, Bing Wallpaper, Rewards Extension)
  - Sweepstakes entries, Refer-a-friend, Redemption goals, Shop-to-earn
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urljoin, urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "last_run.log"

# Browser channel + auth-file are picked by cli() based on --browser. Defaults
# preserve backwards compatibility with the original single-browser layout.
BROWSER_CHANNEL = "msedge"
AUTH_FILE = HERE / "auth.json"

REWARDS_URL = "https://rewards.bing.com/dashboard"
EARN_URL = "https://rewards.bing.com/earn"
BREAKDOWN_URL = "https://rewards.bing.com/pointsbreakdown"
REWARDS_PAGES = (REWARDS_URL, EARN_URL)

# Conservative pacing. This is not an anti-detection or bypass layer; it simply
# keeps the automation close to normal manual use: one visible action, then wait,
# verify, and stop on unexpected state.
TASK_PAUSE_RANGE = (6.0, 14.0)
CREDIT_WAIT_RANGE = (4.5, 9.0)
MAX_CREDIT_WAIT_SECONDS = 70
MAX_CREDIT_POLLS = 5
SEARCH_DWELL_RANGE = (5.0, 11.0)
MAX_SEARCHES_PER_RUN = 8

# Recent Edge UA strings. Real Edge channel already sends an Edge UA; we set these only for
# the mobile search sub-context and as belt-and-suspenders on the desktop context.
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36 EdgA/131.0.0.0"
)

SEARCH_POOL = [
    # Natural-language questions (MS rewards these higher than keyword soup).
    "what is the weather forecast tomorrow",
    "how do I make sourdough bread at home",
    "where can I find cheap flights to tokyo",
    "why is the sky blue scientific explanation",
    "how to learn rust programming in 2026",
    "what time does the world cup final start",
    "how to fix a leaky kitchen faucet step by step",
    "what are the best vr games of 2026",
    "how to start a vegetable garden in spring",
    "where to watch new movies this week",
    "how to take care of a bonsai tree",
    "what is the difference between python async and threading",
    "how to meditate properly for beginners",
    "what is the origin of halloween traditions",
    "how do solar panels actually work",
    "what is the best mechanical keyboard for typing",
    "how to tie a windsor knot tie",
    "what causes northern lights aurora borealis",
    "how to brew the perfect espresso at home",
    "what are the symptoms of vitamin d deficiency",
    "how to sleep better naturally tonight",
    "why do cats purr when they are happy",
    # Place / news / shopping intent (also rewarded well).
    "best coffee shops in san francisco downtown",
    "italian restaurants near times square",
    "tokyo cherry blossom season 2026 forecast",
    "rtx 5070 ti benchmark vs rtx 4080 super",
    "iphone 17 release date and features",
    "tesla stock price today nasdaq",
    "best noise cancelling headphones under 300",
    "fastest electric cars 0 to 60 mph",
    "vintage camera brands collectors guide",
    "budget gaming laptop with rtx 4070 2026",
    # How-to / recipe (these often unlock answer panels).
    "easy chocolate chip cookies recipe from scratch",
    "30 minute home workout routine no equipment",
    "stretching exercises for lower back pain relief",
    "easy origami crane folding instructions",
    "git rebase vs merge which one to use",
    "markdown cheat sheet with examples",
    "japanese hiragana chart pronunciation",
    "ancient rome history quick overview",
    "pomodoro technique for focus and productivity",
    "healthy breakfast ideas under 10 minutes",
]

COPILOT_PROMPTS = [
    "Give me three quick dinner ideas using chicken and pasta.",
    "Explain quantum entanglement in a paragraph for a beginner.",
    "What's a fun weekend project I can do with a Raspberry Pi?",
    "Suggest a 7-day Tokyo travel itinerary focused on food.",
    "Help me write a polite reminder email to a coworker.",
]

IMAGE_PROMPTS = [
    "Earth Day celebration with a lush green planet, blooming flowers, wind turbines at sunrise",
    "A peaceful forest at dawn with golden sunlight filtering through tall trees",
    "Cozy coffee shop interior with warm lighting and wooden tables",
    "A futuristic city skyline at dusk with flying cars and neon lights",
    "Watercolor painting of a Japanese garden with koi pond and cherry blossoms",
]

# ---- utilities -----------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


async def jitter(lo: float = 0.8, hi: float = 2.2) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def launch_browser(p, *, headless: bool, args: Optional[list[str]] = None):
    launch_args = {"headless": headless}
    if args:
        launch_args["args"] = args
    if BROWSER_CHANNEL != "chromium":
        launch_args["channel"] = BROWSER_CHANNEL
    return await p.chromium.launch(**launch_args)


def browser_user_data_dir(browser: str) -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    if browser == "msedge":
        return local / "Microsoft" / "Edge" / "User Data"
    if browser == "chrome":
        return local / "Google" / "Chrome" / "User Data"
    raise ValueError(f"{browser} does not have a system profile directory")


def default_profile_dir(user_data_dir: Path) -> str:
    local_state = user_data_dir / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        return data.get("profile", {}).get("last_used") or "Default"
    except Exception:
        return "Default"


async def import_existing_profile(profile_dir: Optional[str]) -> None:
    if BROWSER_CHANNEL not in {"msedge", "chrome"}:
        raise RuntimeError("--import-profile supports --browser msedge or --browser chrome")

    user_data_dir = browser_user_data_dir(BROWSER_CHANNEL)
    profile = profile_dir or default_profile_dir(user_data_dir)
    if not (user_data_dir / profile).exists():
        raise RuntimeError(f"profile not found: {user_data_dir / profile}")

    log(f"Importing cookies from {BROWSER_CHANNEL} profile '{profile}'.")
    log(f"If this fails, close all {BROWSER_CHANNEL} windows and rerun the import.")
    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel=BROWSER_CHANNEL,
                headless=True,
                args=[f"--profile-directory={profile}"],
                viewport={"width": 1280, "height": 860},
                locale="en-US",
            )
        except Exception as e:
            raise RuntimeError(
                f"could not open the existing {BROWSER_CHANNEL} profile. "
                f"Close all {BROWSER_CHANNEL} windows and retry. ({type(e).__name__}: {e})"
            ) from e
        try:
            page = await ctx.new_page()
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)
            log(f"Rewards URL after import probe: {page.url}")
            await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=15_000)
            await ctx.storage_state(path=str(AUTH_FILE))
            cookies = [c["name"] for c in (await ctx.cookies()) if "bing.com" in c.get("domain", "")]
            logged_in = any(n in cookies for n in ["_U", "_C_Auth", "SRCHUSR"])
            log(f"Saved auth state -> {AUTH_FILE}")
            log(f"Import check: {'OK' if logged_in else 'WEAK'} (bing cookies: {len(cookies)})")
            if "login." in page.url or "/welcome" in page.url:
                log("Profile opened, but Rewards still looks signed out. Use --login if the saved auth does not work.")
        finally:
            await ctx.close()


async def import_from_cdp(cdp_url: str) -> None:
    log(f"Importing cookies from running browser at {cdp_url}.")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError(f"connected to {cdp_url}, but no browser context was available")
        ctx = contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)
        log(f"Rewards URL after CDP probe: {page.url}")
        try:
            await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass
        await ctx.storage_state(path=str(AUTH_FILE))
        cookies = [c["name"] for c in (await ctx.cookies()) if "bing.com" in c.get("domain", "")]
        logged_in = any(n in cookies for n in ["_U", "_C_Auth", "SRCHUSR"])
        log(f"Saved auth state -> {AUTH_FILE}")
        log(f"CDP import check: {'OK' if logged_in else 'WEAK'} (bing cookies: {len(cookies)})")
        if "login." in page.url or "/welcome" in page.url:
            log("Connected browser still looks signed out for Rewards. Open Rewards in Edge and confirm the account.")


# ---- first-time login ----------------------------------------------------

async def first_time_login() -> None:
    log(f"Opening {BROWSER_CHANNEL}. Sign in to your Microsoft account in the browser window.")
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=False, args=["--start-maximized"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 860})
        page = await ctx.new_page()
        # Go to rewards.bing.com — it will redirect to login or /welcome if not signed in.
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)
        log(f"Landed on: {page.url}")
        if "/welcome" in page.url:
            log("On the welcome page; opening the sign-in flow...")
            for label in ["Start earning", "Sign in"]:
                clicked = False
                for role in ["link", "button"]:
                    try:
                        await page.get_by_role(role, name=re.compile(label, re.I)).first.click(timeout=3000)
                        clicked = True
                        await page.wait_for_timeout(3000)
                        break
                    except Exception:
                        pass
                if clicked:
                    break
        # If already on the dashboard (user was logged in via Edge profile), we're done.
        # Otherwise wait for the user to sign in.
        needs_login = "/welcome" in page.url or "login." in page.url
        if needs_login:
            log("Not logged in. Please sign in in the browser window. Do not close it until this script saves auth.")
            try:
                await page.wait_for_url(
                    lambda u: "rewards.bing.com" in u and "/welcome" not in u and "login." not in u,
                    timeout=600_000,
                )
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except PWTimeout:
                pass
        else:
            log("Already logged in!")
        # If still stuck, try clicking sign-in.
        if "/welcome" in page.url or "login." in page.url:
            # If still on welcome page, try clicking "Start earning" / "Sign in".
            log("Timed out waiting for auto-redirect; trying to click sign-in buttons...")
            for label in ["Start earning", "Sign in", "サインイン", "今すぐ参加"]:
                try:
                    await page.get_by_role("link", name=re.compile(label, re.I)).first.click(timeout=3000)
                    await page.wait_for_timeout(5000)
                except Exception:
                    pass
        # Make sure we land on the dashboard.
        if "rewards.bing.com" not in page.url or "/welcome" in page.url:
            try:
                await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=30_000)
            except PWTimeout:
                pass
        # Also hit bing.com to pick up search cookies.
        try:
            await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass
        await ctx.storage_state(path=str(AUTH_FILE))
        log(f"Saved auth state -> {AUTH_FILE}")
        # Verify
        cookies = [c["name"] for c in (await ctx.cookies()) if "bing.com" in c.get("domain", "")]
        logged_in = any(n in cookies for n in ["_U", "ANON", "MUID", "_C_Auth"])
        log(f"Auth check: {'OK' if logged_in else 'FAILED'} (bing cookies: {len(cookies)})")
        await browser.close()


# ---- card discovery ------------------------------------------------------

LOCKED_MARKERS = (
    "Available tomorrow", "Offer is Locked", "Earn -1 points", "offer is locked",
)
LOCKED_MARKERS += ("已锁定", "明天可用", "明天解锁", "ロック", "明日")

COMPLETED_MARKERS = (
    "points earned", "completed", "complete", "已完成", "已赚取", "完了", "獲得済み",
)

SKIP_PATTERNS_ARIA = (
    "referral", "refer and earn", "紹介", "sweepstake", "entries",
    "install the", "set bing as your default", "bing wallpaper",
    "punch card", "ancient coin", "sea of thieves", "rewards extension",
    "redemption goal", "order history", "claim your gift", "shop to earn",
    "set goal", "目標", "ロボット",
)
SKIP_PATTERNS_ARIA += (
    "推荐", "邀请", "抽奖", "奖品", "兑换", "礼品卡", "订单历史", "目标",
    "优惠券", "ギフト", "懸賞", "寄付",
    "签到", "移动应用", "必应应用", "app streak",
)

SKIP_PATTERNS_HREF = (
    "sweepstakes/", "referandearn", "aka.ms/win", "workinprogress",
    "punchcard", "microsoft-store", "goal/all", "orderhistory",
    "/redeem", "/redeemgoal", "xbox.com/rewards",
)
SKIP_PATTERNS_HREF += ("/refer", "rewards.bing.com/redeem")


@dataclass
class Card:
    title: str
    points: int
    href: str
    aria: str
    kind: str  # explore_search | quiz | daily_search | image_creator | image_puzzle | open_only | unknown
    source_url: str = REWARDS_URL
    selector: str = ""
    text: str = ""


def clean_text(text: str) -> str:
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def absolute_url(href: str, base: str = REWARDS_URL) -> str:
    if not href:
        return ""
    return urljoin(base, href)


def extract_points(text: str) -> int:
    text = clean_text(text)
    patterns = [
        r"Earn\s+(\d+)\s+points?",
        r"\+(\d+)\s*(?:points?|分|点)?\b",
        r"(\d+)\s*(?:points?|分|点)\s*(?:$|已完成|完了)",
        r"(\d+)\s+points?\s*$",
        r"(\d+)\s*[分点]\s*$",
        r"\+(\d+)\s*p\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return int(m.group(1))
    return 0


def extract_progress(text: str) -> Optional[tuple[int, int]]:
    matches = re.findall(r"(\d+)\s*/\s*(\d+)", clean_text(text))
    valid = [(int(a), int(b)) for a, b in matches if int(b) > 1]
    if not valid:
        return None
    return max(valid, key=lambda pair: pair[1])


def parse_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d[\d,]*", str(text))
    return int(m.group(0).replace(",", "")) if m else None


def parse_labeled_number(text: str, labels: list[str]) -> Optional[int]:
    lines = [clean_text(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    lower_labels = [label.lower() for label in labels]
    for i, line in enumerate(lines):
        low = line.lower()
        for label in lower_labels:
            idx = low.find(label)
            if idx < 0:
                continue
            value = parse_int(line[idx + len(label):])
            if value is not None:
                return value
            for next_line in lines[i + 1:i + 4]:
                value = parse_int(next_line)
                if value is not None:
                    return value
    return None


def points_delta(before: tuple[Optional[int], Optional[int]],
                 after: tuple[Optional[int], Optional[int]]) -> tuple[bool, str]:
    before_avail, before_today = before
    after_avail, after_today = after
    if before_avail is not None and after_avail is not None and after_avail > before_avail:
        return True, f"available +{after_avail - before_avail}"
    if before_today is not None and after_today is not None and after_today > before_today:
        return True, f"today +{after_today - before_today}"
    return False, "no points increase detected"


async def read_bing_header_points(page: Page) -> Optional[int]:
    """Read the Bing SERP rewards medallion when the current page is already on Bing."""
    try:
        if "bing.com" not in urlparse(page.url).netloc.lower():
            return None
    except Exception:
        return None
    for selector in ("#rh_rwm", ".kumo_rewards", ".medallion"):
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                continue
            value = parse_int(await loc.text_content(timeout=1200))
            if value is not None and value >= 100:
                return value
        except Exception:
            continue
    try:
        body = await page.locator("body").inner_text(timeout=2500)
    except Exception:
        return None
    lines = [clean_text(line) for line in body.splitlines()]
    lines = [line for line in lines if line]
    for line in lines[:16]:
        if re.fullmatch(r"\d[\d,]{2,}", line):
            value = parse_int(line)
            if value is not None and 100 <= value <= 10_000_000:
                return value
    return None


async def remember_bing_header_points(target: Page, source: Page) -> Optional[int]:
    points = await read_bing_header_points(source)
    if points is not None:
        try:
            prior = getattr(target, "_last_bing_available", None)
            if prior is None or points > prior:
                setattr(target, "_last_bing_available", points)
        except Exception:
            pass
    return points


def title_from_text(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    title = re.split(r"\s{2,}|,\s*Earn\s+\d+|Earn\s+\d+\s+points?|(\d+)\s+points?$", text, maxsplit=1, flags=re.I)[0]
    title = clean_text(title)
    return title[:90] or text[:90]


def css_attr(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def keyword_from_reward_text(text: str) -> Optional[str]:
    text = clean_text(text)
    if not text:
        return None
    text = re.sub(r"\+\d+\s*(?:points?|分|点)?", " ", text, flags=re.I)
    text = re.sub(r"\d+\s*/\s*\d+\s*(?:个任务|tasks?)?", " ", text, flags=re.I)
    text = re.sub(r"(?:Earn|赚取|获得)\s*\d+\s*(?:points?|积分|分|点)", " ", text, flags=re.I)
    text = re.sub(r"\b(?:on|with)\s+Bing\b", " ", text, flags=re.I)
    text = re.sub(r"(?:在|用)\s*Bing\s*(?:上)?\s*(?:搜索|查找|寻找|查看|比较|以)?", " ", text, flags=re.I)
    text = re.sub(r"(?:在|用)\s*必应\s*(?:上)?\s*(?:搜索|查找|寻找|查看|比较|以)?", " ", text, flags=re.I)
    text = re.sub(r"\b(search|find|look up|compare|browse|explore)\b", " ", text, flags=re.I)
    text = clean_text(text)

    cjk_chunks = re.findall(r"[\u3400-\u9fff\u3040-\u30ff][\u3400-\u9fff\u3040-\u30ffA-Za-z0-9\s]{1,40}", text)
    cleaned_chunks = []
    stop = re.compile(r"(闪耀光芒|明天解锁|已激活|到期日期|任务|积分|每日|活动|完成|查看您的新仪表板)")
    for chunk in cjk_chunks:
        chunk = clean_text(stop.sub(" ", chunk))
        chunk = re.sub(r"[，。、“”：「」『』（）()]+", " ", chunk)
        chunk = clean_text(chunk)
        if 2 <= len(chunk) <= 30:
            cleaned_chunks.append(chunk)
    if cleaned_chunks:
        return cleaned_chunks[-1]

    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text)
    stop_words = {
        "earn", "points", "point", "search", "bing", "microsoft", "reward", "rewards",
        "complete", "activity", "activities", "more", "daily", "today", "this", "that",
        "with", "using", "your", "find", "look", "explore", "browse",
    }
    terms = [w.lower() for w in words if w.lower() not in stop_words]
    if terms:
        return " ".join(terms[:6])
    return None


def classify(aria: str, href: str, text: str = "") -> str:
    low_a, low_h = (aria + " " + text).lower(), href.lower()
    if "每日连签活动" in low_a or "daily streak" in low_a or "daily check" in low_a:
        return "streak_activity"
    if "必应应用连签" in low_a or "bing app" in low_a or "app streak" in low_a:
        return "app_checkin"
    if "/earn/quest/" in low_h:
        return "quest"
    if "ml2xqd" in low_h or "每天赚取100" in low_a or "extra100" in low_a:
        return "search_bonus"
    if "images/create" in low_h or "image creator" in low_a:
        return "image_creator"
    if "rwautoflyout=exb" in low_h or "explore on bing" in low_a:
        return "explore_search"
    if "form=dsetqu" in low_h or "form=ml2bf1" in low_h or "quiz" in low_a or "トリビア" in low_a:
        return "quiz"
    if "spotlight/imagepuzzle" in low_h or "puzzle" in low_a or "パズル" in low_a:
        return "image_puzzle"
    # Daily-set style search: rewards tracking form like ML2X9*, tgrew*, etc.
    if "bing.com/search" in low_h and re.search(r"form=(ml2|tgrew|dset|ml1[0-9])", low_h):
        return "daily_search"
    if "bing.com" in low_h:
        return "open_only"
    return "unknown"


async def _collect_card_candidates(page: Page) -> list[dict]:
    return await page.evaluate(
        """() => {
            const clean = (s) => (s || '').replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '').replace(/\\s+/g, ' ').trim();
            const cssEscape = (value) => {
                if (window.CSS && CSS.escape) return CSS.escape(value);
                return String(value).replace(/["\\\\]/g, '\\\\$&');
            };
            const selectorFor = (el) => {
                if (!el || !el.tagName) return '';
                if (el.id) return `${el.tagName.toLowerCase()}#${cssEscape(el.id)}`;
                const aria = el.getAttribute('aria-label');
                if (aria) return `${el.tagName.toLowerCase()}[aria-label="${cssEscape(aria)}"]`;
                const href = el.getAttribute('href');
                if (href) return `${el.tagName.toLowerCase()}[href="${cssEscape(href)}"]`;
                const role = el.getAttribute('role');
                if (role) return `${el.tagName.toLowerCase()}[role="${cssEscape(role)}"]`;
                return el.tagName.toLowerCase();
            };
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight
                    && r.right > 0 && r.left < innerWidth
                    && st.display !== 'none' && st.visibility !== 'hidden';
            };
            const elements = new Set([
                ...document.querySelectorAll('a[href], a[aria-label], button, [role="button"], [role="link"]'),
                ...document.querySelectorAll('[data-bi-id], [data-m], [data-testid], [aria-label*="point" i], [aria-label*="earn" i]')
            ]);
            const out = [];
            for (const el of elements) {
                const clickable = el.closest('a[href], button, [role="button"], [role="link"]') || el;
                if (!visible(clickable)) continue;
                const href = clickable.getAttribute('href') || el.getAttribute('href') || '';
                const aria = clickable.getAttribute('aria-label') || el.getAttribute('aria-label') || '';
                const text = clean([aria, clickable.innerText, el.innerText, clickable.getAttribute('title'), el.getAttribute('title')].filter(Boolean).join(' '));
                if (!href && !aria && !text) continue;
                out.push({
                    tag: clickable.tagName.toLowerCase(),
                    role: clickable.getAttribute('role') || '',
                    href,
                    aria,
                    text,
                    selector: selectorFor(clickable)
                });
            }
            return out;
        }"""
    )


async def discover_cards_legacy(page: Page) -> list[Card]:
    cards: list[Card] = []
    # Trigger lazy-loaded sections (More activities only renders after a scroll).
    try:
        for y in (400, 1200, 2400, 3600):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await page.wait_for_timeout(400)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(700)
    except Exception:
        pass
    anchors = page.locator("a[aria-label]")
    count = await anchors.count()
    for i in range(count):
        a = anchors.nth(i)
        try:
            aria = (await a.get_attribute("aria-label")) or ""
            href = (await a.get_attribute("href")) or ""
        except Exception:
            continue
        if not aria or not href:
            continue
        # Skip href=# (tomorrow's locked cards) and empty hrefs.
        if href.strip() in ("#", ""):
            continue
        if any(m.lower() in aria.lower() for m in LOCKED_MARKERS):
            continue
        # Already-completed cards: "N points earned" or "Complete".
        if "points earned" in aria.lower():
            continue
        # Match points: "Earn 10 points" OR trailing "10 points" / "5 点" / "10 分".
        pts = 0
        m = re.search(r"Earn\s+(\d+)\s+points?", aria)
        if m:
            pts = int(m.group(1))
        else:
            # Daily-set style: "title   description   10 points" (no "earned" after it).
            m2 = re.search(r"(\d+)\s+points?\s*$", aria.strip())
            if not m2:
                m2 = re.search(r"(\d+)\s*[分点]\s*$", aria.strip())
            if m2:
                pts = int(m2.group(1))
        if pts <= 0:
            continue
        if any(p in aria.lower() for p in SKIP_PATTERNS_ARIA):
            continue
        if any(p in href.lower() for p in SKIP_PATTERNS_HREF):
            continue
        title = re.sub(r"[​‌‍﻿]", "", aria.split(",", 1)[0].strip())
        # Daily-set titles use multi-space separation, take first segment.
        if "," not in aria and "   " in aria:
            title = re.sub(r"[​‌‍﻿]", "", aria.split("   ", 1)[0].strip())
        cards.append(Card(title=title, points=pts, href=href, aria=aria,
                          kind=classify(aria, href)))
    # de-dupe (title,href)
    seen = set()
    uniq: list[Card] = []
    for c in cards:
        key = (c.title, c.href)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


async def expand_dashboard_daily_section(page: Page) -> None:
    """Open the dashboard Daily activities disclosure so its task links are visible."""
    try:
        if "/dashboard" not in urlparse(page.url).path:
            return
    except Exception:
        return
    try:
        await page.evaluate("window.scrollTo(0, 1000)")
        await page.wait_for_timeout(600)
    except Exception:
        pass
    try:
        button = await page.evaluate_handle(
            """() => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight
                        && r.right > 0 && r.left < innerWidth
                        && st.display !== 'none' && st.visibility !== 'hidden';
                };
                return [...document.querySelectorAll('button[aria-expanded="false"][aria-controls]')]
                    .find((el) => visible(el) && clean(el.getAttribute('aria-label') || el.innerText || el.textContent) === '每日活动')
                    || [...document.querySelectorAll('button[aria-expanded="false"][aria-controls]')]
                    .find((el) => visible(el) && /Daily activities|Daily activity/i.test(clean(el.getAttribute('aria-label') || el.innerText || el.textContent)))
                    || null;
            }"""
        )
        element = button.as_element()
        if element is not None:
            await element.click(timeout=5000)
            await page.wait_for_timeout(700)
    except Exception:
        pass


async def discover_dashboard_daily_cards(page: Page) -> list[Card]:
    """Collect the dashboard daily activity links that live inside carousel/panel sections."""
    try:
        for y in (0, 650, 950, 1200):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await page.wait_for_timeout(500)
        await expand_dashboard_daily_section(page)
    except Exception:
        pass
    try:
        items = await page.evaluate(
            """() => {
                const clean = (s) => (s || '').replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight
                        && r.right > 0 && r.left < innerWidth
                        && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const cssEscape = (value) => {
                    if (window.CSS && CSS.escape) return CSS.escape(value);
                    return String(value).replace(/["\\\\]/g, '\\\\$&');
                };
                const selectorFor = (el) => {
                    const href = el.getAttribute('href') || '';
                    if (href) return `a[href="${cssEscape(href)}"]`;
                    return 'a[href]';
                };
                return [...document.querySelectorAll('a[href]')].map((el) => ({
                    href: el.getAttribute('href') || '',
                    text: clean([el.getAttribute('aria-label'), el.innerText, el.textContent, el.getAttribute('title')].filter(Boolean).join(' ')),
                    selector: selectorFor(el),
                    visible: visible(el)
                })).filter((x) => {
                    const h = x.href.toLowerCase();
                    const t = x.text.toLowerCase();
                    return (
                        h.includes('rewardsquiz_dailyset')
                        || h.includes('dsetqu')
                        || h.includes('tgrew')
                        || /form=ml2x[0-9a-z]/i.test(h)
                        || (h.includes('bing.com/search') && (t.includes('+10') || t.includes('10 points')))
                    ) && x.visible;
                });
            }"""
        )
    except Exception as e:
        log(f"  dashboard daily scan failed: {type(e).__name__}: {str(e)[:120]}")
        return []

    cards: list[Card] = []
    for item in items:
        href = absolute_url(item.get("href", ""), page.url)
        text = clean_text(item.get("text", ""))
        if not href or not text:
            continue
        low = text.lower()
        if any(m.lower() in low for m in COMPLETED_MARKERS):
            continue
        pts = extract_points(text) or 10
        if pts <= 0:
            continue
        cards.append(Card(
            title=title_from_text(text),
            points=pts,
            href=href,
            aria=text,
            kind=classify(text, href, text),
            source_url=page.url,
            selector=item.get("selector", ""),
            text=text,
        ))
    seen = set()
    uniq: list[Card] = []
    for c in cards:
        key = (c.href, c.title.lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


async def discover_cards(page: Page) -> list[Card]:
    """Discover earnable activities on the current Rewards page.

    New Rewards layouts do not always expose tasks as a[aria-label], so discovery
    uses a broader clickable/data-attribute scan and normalizes candidates here.
    """
    cards: list[Card] = []
    try:
        for y in (400, 1200, 2400, 3600, 5200):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await page.wait_for_timeout(400)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(700)
    except Exception:
        pass
    try:
        candidates = await _collect_card_candidates(page)
    except Exception as e:
        log(f"  candidate collection failed: {type(e).__name__}: {str(e)[:120]}")
        candidates = []
    if "/dashboard" in urlparse(page.url).path:
        cards.extend(await discover_dashboard_daily_cards(page))
    for item in candidates:
        aria = clean_text(item.get("aria", ""))
        text = clean_text(item.get("text", ""))
        href = absolute_url(item.get("href", ""), page.url)
        combined = clean_text(f"{aria} {text}")
        if not combined and not href:
            continue
        if not href and not item.get("selector"):
            continue
        if href.strip() in ("#", "") and not item.get("selector"):
            continue
        low = combined.lower()
        if any(m.lower() in low for m in LOCKED_MARKERS):
            continue
        if any(m.lower() in low for m in COMPLETED_MARKERS):
            continue
        pts = extract_points(combined)
        if pts <= 0:
            continue
        kind = classify(aria, href, text)
        title = title_from_text(aria or text)
        if not href and (len(title) < 4 or re.fullmatch(r"(?:\+?\d+\s*){1,3}(?:points?|积分|分|点)?", title, re.I)):
            continue
        if any(p in low for p in SKIP_PATTERNS_ARIA) and kind not in {"streak_activity", "app_checkin"}:
            continue
        low_href = href.lower()
        if any(p in low_href for p in SKIP_PATTERNS_HREF) and "/earn/quest/" not in low_href:
            continue
        cards.append(Card(
            title=title,
            points=pts,
            href=href,
            aria=aria,
            kind=kind,
            source_url=page.url,
            selector=item.get("selector", ""),
            text=text,
        ))
    seen = set()
    uniq: list[Card] = []
    for c in cards:
        key = (c.title.lower(), c.href or c.selector, c.points)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


async def discover_rewards_cards(page: Page) -> list[Card]:
    all_cards: list[Card] = []
    for url in REWARDS_PAGES:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            log(f"  discovery page failed {url}: {type(e).__name__}: {str(e)[:120]}")
            continue
        cards = await discover_cards(page)
        log(f"  discovery {urlparse(url).path or '/'}: {len(cards)} card(s)")
        all_cards.extend(cards)
    seen = set()
    uniq: list[Card] = []
    for c in all_cards:
        key = (c.title.lower(), c.href or c.selector, c.points)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def card_key(card: Card) -> tuple[str, str, int]:
    return (card.title.lower(), card.href or card.selector, card.points)


def card_text_snippets(card: Card) -> list[str]:
    raw = clean_text(card.text or card.aria or card.title)
    raw = re.sub(r"\+\d+\s*(?:points?|分|点)?[\s\S]*$", "", raw, flags=re.I)
    raw = clean_text(raw)
    snippets: list[str] = []
    if raw:
        snippets.append(raw[:70])
        snippets.append(raw[:35])
    title = clean_text(card.title)
    if title and title not in snippets:
        snippets.append(title[:50])
    out: list[str] = []
    for snippet in snippets:
        snippet = clean_text(snippet)
        if len(snippet) >= 6 and snippet not in out:
            out.append(snippet)
    return out


async def wait_for_credit(page: Page, card: Card,
                          before: tuple[Optional[int], Optional[int]]) -> tuple[bool, str, tuple[Optional[int], Optional[int]]]:
    start = time.monotonic()
    after = before
    last_reason = "no points/card-state change detected"
    for attempt in range(MAX_CREDIT_POLLS):
        await jitter(*CREDIT_WAIT_RANGE)
        after = await read_points(page)
        credited, reason = points_delta(before, after)
        if credited:
            suffix = "" if attempt == 0 else f" after {int(time.monotonic() - start)}s"
            return True, reason + suffix, after
        try:
            cards = await discover_rewards_cards(page)
            if card_key(card) not in {card_key(c) for c in cards}:
                suffix = "" if attempt == 0 else f" after {int(time.monotonic() - start)}s"
                return True, "card removed from earn list" + suffix, after
        except Exception as e:
            last_reason = f"credit check discovery failed: {type(e).__name__}"
        if time.monotonic() - start >= MAX_CREDIT_WAIT_SECONDS:
            break
    return False, last_reason, after


async def wait_for_points_increase(page: Page,
                                   before: tuple[Optional[int], Optional[int]]) -> tuple[bool, str, tuple[Optional[int], Optional[int]]]:
    start = time.monotonic()
    after = before
    for attempt in range(MAX_CREDIT_POLLS):
        await jitter(*CREDIT_WAIT_RANGE)
        after = await read_points(page)
        credited, reason = points_delta(before, after)
        if credited:
            suffix = "" if attempt == 0 else f" after {int(time.monotonic() - start)}s"
            return True, reason + suffix, after
        if time.monotonic() - start >= MAX_CREDIT_WAIT_SECONDS:
            break
    return False, "no points increase detected", after


# ---- task handlers -------------------------------------------------------

SEARCH_KEYWORDS = [
    (re.compile(r"保险|insurance", re.I), "最适合我的保险计划"),
    (re.compile(r"贷款|学生贷款|personal loans?", re.I), "个人贷款和学生贷款比较"),
    (re.compile(r"手机套餐|通话|短信|mobile plan|phone plan", re.I), "适合我的手机套餐"),
    (re.compile(r"邮轮|cruise", re.I), "邮轮优惠和目的地"),
    (re.compile(r"互联网套餐|internet plan", re.I), "比较附近互联网套餐"),
    (re.compile(r"珠宝|jewel", re.I), "适合任何场合的惊艳珠宝"),
    (re.compile(r"住宿|酒店|hotel", re.I), "下一次冒险的住宿酒店"),
    (re.compile(r"房屋|可售房产|real estate|house", re.I), "梦想小镇可售房产"),
    (re.compile(r"球队|比赛|sports", re.I), "最近球队比赛结果"),
    (re.compile(r"航班|假期|flight", re.I), "完美假期航班"),
    (re.compile(r"coupon|discount|save more", re.I), "best coupon codes and discounts"),
    (re.compile(r"car|hit the road|vehicle", re.I), "new cars for sale near me"),
    (re.compile(r"home|upgrade your space|remodel", re.I), "home improvement tools kitchen"),
    (re.compile(r"stream|netflix|hulu|favorites", re.I), "best streaming services"),
    (re.compile(r"flower", re.I), "fresh flower delivery"),
    (re.compile(r"job|role|career", re.I), "open job roles tech companies"),
    (re.compile(r"restaurant|cook|food|yummi|cook\?", re.I), "restaurants near me open now"),
    (re.compile(r"concert|music|live event", re.I), "live music events this weekend"),
    (re.compile(r"underwater|ocean|sea cave", re.I), "underwater photography tips"),
    (re.compile(r"earth day|recycle|planet", re.I), "earth day activities 2026"),
    (re.compile(r"credit|score|report", re.I), "how to check credit score free"),
    (re.compile(r"mattress|bed", re.I), "top rated mattresses 2026"),
    (re.compile(r"pet|furry|dog|cat", re.I), "best pet food brands"),
    (re.compile(r"weather|upcoming weather", re.I), "weather forecast this week"),
    (re.compile(r"quote", re.I), "quote of the day inspirational"),
]


def keyword_for(card: Card) -> str:
    try:
        q = parse_qs(urlparse(card.href).query).get("q", [""])[0]
        q = clean_text(unquote(q))
        if q:
            return q
    except Exception:
        pass
    text = clean_text(f"{card.aria} {card.text} {card.title}")
    for pat, kw in SEARCH_KEYWORDS:
        if pat.search(text):
            return kw
    inferred = keyword_from_reward_text(text)
    if inferred:
        return inferred
    return random.choice(SEARCH_POOL)


def bing_search_url(query: str, card: Optional[Card] = None) -> str:
    params: list[tuple[str, str]] = []
    if card:
        try:
            params = [(k, v) for k, v in parse_qsl(urlparse(card.href).query, keep_blank_values=True)
                      if k.lower() != "q"]
        except Exception:
            params = []
    if not any(k.lower() == "form" for k, _ in params):
        params.append(("form", "QBLH"))
    if card and not any(k.lower() == "rwautoflyout" for k, _ in params):
        params.append(("rwAutoFlyout", "exb"))
    return "https://www.bing.com/search?" + urlencode([("q", query), *params])


async def submit_bing_search(page: Page, query: str, *, human: bool = True) -> bool:
    selectors = [
        "textarea[name='q']",
        "input[name='q']",
        "#sb_form_q",
        "textarea#searchbox",
        "input[type='search']",
        "textarea[placeholder*='Search']",
        "input[placeholder*='Search']",
        "textarea[placeholder*='搜索']",
        "input[placeholder*='搜索']",
        "textarea[aria-label*='Search']",
        "input[aria-label*='Search']",
        "textarea[aria-label*='搜索']",
        "input[aria-label*='搜索']",
    ]
    locators = []
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0:
                locators.append(loc)
        except Exception:
            continue
    for pattern in [r"search|搜索|搜尋|検索"]:
        try:
            loc = page.get_by_role("combobox", name=re.compile(pattern, re.I)).first
            if await loc.count() > 0:
                locators.append(loc)
        except Exception:
            continue
    for loc in locators:
        try:
            await loc.click(timeout=5000)
            await loc.fill("")
            if human:
                await loc.type(query, delay=random.randint(40, 110))
            else:
                await loc.fill(query)
            await jitter(0.3, 0.9)
            await loc.press("Enter")
            return True
        except Exception:
            continue
    return False


async def _click_card(dashboard: Page, card: Card, ctx: BrowserContext, *,
                      allow_fallback: bool = False,
                      return_same_page_on_click: bool = False) -> Optional[Page]:
    """Click the Rewards card and return the popped-up tab or same-page navigation page."""
    try:
        if card.source_url and dashboard.url.split("#", 1)[0] != card.source_url.split("#", 1)[0]:
            await dashboard.goto(card.source_url, wait_until="domcontentloaded", timeout=30_000)
            await dashboard.wait_for_timeout(2500)
        await expand_dashboard_daily_section(dashboard)
    except Exception:
        pass
    locators = []
    for snippet in card_text_snippets(card):
        locators.append(("text", snippet))
    if card.selector:
        locators.append(("css", card.selector))
    if card.aria:
        locators.append(("css", f'[aria-label="{css_attr(card.aria)}"]'))
    if card.href:
        href_path = urlparse(card.href).path
        href_query = urlparse(card.href).query
        if href_query:
            locators.append(("css", f'a[href*="{css_attr(href_query[:80])}"]'))
        if href_path and href_path != "/":
            locators.append(("css", f'a[href*="{css_attr(href_path)}"]'))
    try:
        for locator_kind, locator_value in locators:
            try:
                if locator_kind == "text":
                    text_loc = dashboard.get_by_text(re.compile(re.escape(locator_value), re.I)).first
                    if await text_loc.count() == 0:
                        continue
                    loc = text_loc.locator("xpath=ancestor-or-self::*[self::a or self::button or @role='button' or @role='link'][1]").first
                else:
                    loc = dashboard.locator(locator_value).first
                if await loc.count() == 0:
                    continue
                before_url = dashboard.url
                before_pages = set(ctx.pages)
                await loc.click(timeout=8000)
                try:
                    await dashboard.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass
                await dashboard.wait_for_timeout(1200)
                new_pages = [p for p in ctx.pages if p not in before_pages and not p.is_closed()]
                if new_pages:
                    setattr(new_pages[-1], "_rewards_auto_close", True)
                    setattr(new_pages[-1], "_rewards_click_method", "click")
                    return new_pages[-1]
                if dashboard.url != before_url:
                    setattr(dashboard, "_rewards_auto_close", False)
                    setattr(dashboard, "_rewards_click_method", "click")
                    return dashboard
                if return_same_page_on_click:
                    setattr(dashboard, "_rewards_auto_close", False)
                    setattr(dashboard, "_rewards_click_method", "click")
                    return dashboard
            except Exception:
                continue
        if card.title:
            text_loc = dashboard.get_by_text(re.compile(re.escape(card.title[:50]), re.I)).first
            if await text_loc.count() > 0:
                clickable = text_loc.locator("xpath=ancestor-or-self::*[self::a or self::button][1]").first
                if await clickable.count() > 0:
                    try:
                        before_url = dashboard.url
                        before_pages = set(ctx.pages)
                        await clickable.click(timeout=8000)
                        try:
                            await dashboard.wait_for_load_state("domcontentloaded", timeout=10_000)
                        except Exception:
                            pass
                        await dashboard.wait_for_timeout(1200)
                        new_pages = [p for p in ctx.pages if p not in before_pages and not p.is_closed()]
                        if new_pages:
                            setattr(new_pages[-1], "_rewards_auto_close", True)
                            setattr(new_pages[-1], "_rewards_click_method", "click")
                            return new_pages[-1]
                        if dashboard.url != before_url:
                            setattr(dashboard, "_rewards_auto_close", False)
                            setattr(dashboard, "_rewards_click_method", "click")
                            return dashboard
                        if return_same_page_on_click:
                            setattr(dashboard, "_rewards_auto_close", False)
                            setattr(dashboard, "_rewards_click_method", "click")
                            return dashboard
                    except Exception:
                        pass
    except Exception:
        pass
    if not allow_fallback:
        return None
    # Fallback is reserved for explicit diagnostics. Normal runs only use real
    # clickable elements that were visible on the Rewards page.
    try:
        if not card.href:
            return None
        tab = await ctx.new_page()
        await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
        setattr(tab, "_rewards_auto_close", True)
        setattr(tab, "_rewards_click_method", "fallback")
        return tab
    except Exception as e:
        log(f"     fallback navigation failed: {e}")
        return None


async def do_explore_search(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    log(f"  -> explore_search: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return False
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     card could not be clicked from the visible Rewards page; skipping direct fallback")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return False
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=20_000)
        await jitter(1.8, 4.2)
        kw = keyword_for(card)
        if not await submit_bing_search(tab, kw, human=True):
            log("     Bing search box not found after visible card click")
            return False
        await tab.wait_for_load_state("domcontentloaded", timeout=20_000)
        await jitter(*SEARCH_DWELL_RANGE)
        await remember_bing_header_points(dashboard, tab)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass


async def do_daily_search(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    """Daily-set search link. Prefer the real Rewards click path over naked URL navigation."""
    log(f"  -> daily_search: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return False
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     card could not be clicked from the visible Rewards page; skipping direct fallback")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return False
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=30_000)
        await jitter(4.0, 8.0)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass


async def do_open_only(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    log(f"  -> open_only: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return False
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     card could not be clicked from the visible Rewards page; skipping direct fallback")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return False
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=30_000)
        await jitter(4.0, 8.0)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass


async def do_search_bonus(ctx: BrowserContext, dashboard: Page, card: Card) -> Optional[bool]:
    log(f"  -> activate search_bonus: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return None
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     search bonus was not reachable via a visible card click; skipping")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return None
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=20_000)
    except Exception:
        pass
    try:
        await tab.wait_for_timeout(3000)
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
    return None


async def do_skip_known(ctx: BrowserContext, dashboard: Page, card: Card) -> Optional[bool]:
    log(f"  -> skip {card.kind}: {card.title} ({card.points}p)")
    return None


async def do_quiz(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    """3-to-10-question multiple choice.

    Hardened: stops on stale tabs / detached locators / context death so a single
    bad quiz can't take the whole run down with it. Returns True if at least one
    answer was clicked (which already credits partial points)."""
    log(f"  -> quiz: {card.title} ({card.points}p)")
    try:
        tab = await _click_card(dashboard, card, ctx)
    except Exception as e:
        log(f"     could not open quiz tab: {e}")
        return False
    if tab is None:
        return False
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     quiz card could not be clicked from the visible Rewards page; skipping direct fallback")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return False
    answered = 0
    consecutive_misses = 0
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=30_000)
        await jitter(3.0, 5.5)
        await remember_bing_header_points(dashboard, tab)
        # Dismiss any consent banner.
        for _ in range(2):
            try:
                await tab.get_by_role("button", name=re.compile("Accept|同意|同意する", re.I)).click(timeout=1500)
            except Exception:
                break
        # Iterate up to 12 question rounds (max real quiz is ~10).
        for qi in range(12):
            if tab.is_closed():
                log(f"     tab closed before q{qi + 1}; stopping.")
                break
            target = None
            target_label = ""
            try:
                option = await tab.evaluate(
                    """() => {
                        const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight
                                && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        const cssEscape = (value) => {
                            if (window.CSS && CSS.escape) return CSS.escape(value);
                            return String(value).replace(/["\\\\]/g, '\\\\$&');
                        };
                        const cards = [...document.querySelectorAll('.btq_card.btq_quesP, .btq_opts')]
                            .filter(visible)
                            .filter((el) => !String(el.className || '').includes('btq_hideCompulsary'));
                        const root = cards[0] || document;
                        const links = [...root.querySelectorAll('.btq_opt a[href*="WQCI"], .btq_opt a[href*="WQSCORE"]')]
                            .filter(visible)
                            .map((a, index) => {
                                const href = a.getAttribute('href') || '';
                                const decoded = (() => {
                                    try { return decodeURIComponent(href); } catch { return href; }
                                })();
                                const score = /WQSCORE[:=]"?(\\d+)/i.exec(decoded)?.[1]
                                    || /WQSCORE%3A%22(\\d+)%22/i.exec(href)?.[1]
                                    || /WQSCORE(?:%3a|=)(?:%22)?(\\d+)/i.exec(href)?.[1]
                                    || '0';
                                return {
                                    index,
                                    href,
                                    text: clean(a.innerText || a.textContent || a.getAttribute('aria-label') || ''),
                                    score: Number.parseInt(score, 10) || 0,
                                };
                            });
                        if (!links.length) return null;
                        links.sort((a, b) => b.score - a.score || a.index - b.index);
                        return links[0];
                    }"""
                )
                if option:
                    href = absolute_url(option.get("href", ""), tab.url)
                    target_label = clean_text(option.get("text", ""))
                    if href:
                        target = tab.locator(f'a[href="{css_attr(href)}"]').first
                        if await target.count() == 0:
                            target = tab.locator(f'a[href="{css_attr(urlparse(href).path + "?" + urlparse(href).query)}"]').first
                    if target is None or await target.count() == 0:
                        target = tab.locator('a[href*="WQCI"], a[href*="WQSCORE"]').filter(has_text=target_label).first
            except Exception as e:
                log(f"     locator query died at q{qi + 1}: {type(e).__name__}; stopping.")
                break
            if target is None:
                next_button = None
                try:
                    if await tab.locator(".btq_card.btq_ansP .btq_nxtQues button").count() > 0:
                        next_button = tab.locator(".btq_card.btq_ansP .btq_nxtQues button").first
                    else:
                        for label in ("下一个", "Next", "次へ"):
                            loc = tab.get_by_text(label).locator(
                                "xpath=ancestor-or-self::*[self::button or @role='button'][1]"
                            ).first
                            if await loc.count() > 0:
                                next_button = loc
                                break
                except Exception:
                    next_button = None
                if next_button is not None:
                    try:
                        await next_button.scroll_into_view_if_needed(timeout=4000)
                    except Exception:
                        pass
                    try:
                        await next_button.click(timeout=8000)
                        await jitter(1.8, 3.4)
                        consecutive_misses = 0
                        continue
                    except Exception:
                        pass
                consecutive_misses += 1
                try:
                    result_link = tab.locator(".btq_card.btq_ansP a[href*='WQId'], .btq_card.btq_ansP a[href*='WQOskey']").filter(
                        has_text=re.compile("查看结果|View results|結果", re.I)
                    ).first
                    if await result_link.count() > 0:
                        await result_link.click(timeout=8000)
                        await jitter(2.5, 4.0)
                        await remember_bing_header_points(dashboard, tab)
                        break
                except Exception:
                    pass
                if consecutive_misses >= 2:
                    log(f"     no options at q{qi + 1} (twice); quiz complete or unreachable.")
                    break
                await tab.wait_for_timeout(2000)
                continue
            consecutive_misses = 0
            # Scroll & click.
            try:
                await target.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            clicked = False
            try:
                await target.click(timeout=8000)
                clicked = True
            except Exception:
                # JS fallback for obscured/animated elements.
                try:
                    await target.evaluate("el => el.click()")
                    clicked = True
                except Exception as e:
                    msg = str(e)
                    if "closed" in msg.lower() or "detached" in msg.lower():
                        log(f"     tab/context closed during q{qi + 1}; stopping.")
                        break
                    log(f"     could not click q{qi + 1}: {type(e).__name__}; trying next.")
            if not clicked:
                continue
            answered += 1
            if target_label:
                log(f"     answered q{answered}: {target_label[:40]}")
            # Wait for navigation (clicking answer triggers a page change).
            try:
                await tab.wait_for_load_state("domcontentloaded", timeout=12_000)
            except PWTimeout:
                pass
            except Exception:
                # Likely a context error — bail.
                log(f"     wait_for_load died at q{qi + 1}; stopping.")
                break
            await jitter(2.5, 4.0)
            await remember_bing_header_points(dashboard, tab)
        return answered > 0
    except Exception as e:
        log(f"     quiz outer error ({answered} answered): {type(e).__name__}: {e}")
        return answered > 0
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True) and not tab.is_closed():
                await tab.close()
        except Exception:
            pass


async def do_image_puzzle(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    """Image jigsaw: 'Skip puzzle' link in the top-right credits the task."""
    log(f"  -> image_puzzle: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return False
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     puzzle card could not be clicked from the visible Rewards page; skipping direct fallback")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return False
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=30_000)
        await jitter(3.0, 5.5)
        for pattern in [r"Skip puzzle", r"スキップ"]:
            loc = tab.get_by_role("link", name=re.compile(pattern, re.I))
            if await loc.count():
                await loc.first.click()
                await tab.wait_for_load_state("domcontentloaded", timeout=15_000)
                break
        await tab.wait_for_timeout(3000)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass


async def do_image_creator(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    log(f"  -> image_creator: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return False
    if getattr(tab, "_rewards_click_method", "") != "click":
        log("     image creator card could not be clicked from the visible Rewards page; skipping direct fallback")
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass
        return False
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=20_000)
        await tab.wait_for_timeout(2500)
        try:
            await tab.get_by_role("button", name=re.compile("^Later$", re.I)).click(timeout=3000)
        except Exception:
            pass
        prompt_box = tab.get_by_role("textbox", name=re.compile("Describe the image", re.I))
        await prompt_box.wait_for(timeout=15_000)
        await prompt_box.fill(random.choice(IMAGE_PROMPTS))
        await tab.get_by_role("button", name=re.compile("^Create$", re.I)).click()
        await tab.wait_for_timeout(20_000)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            if getattr(tab, "_rewards_auto_close", True):
                await tab.close()
        except Exception:
            pass


HANDLERS = {
    "explore_search": do_explore_search,
    "daily_search":   do_daily_search,
    "quiz":           do_quiz,
    "image_puzzle":   do_image_puzzle,
    "image_creator":  do_image_creator,
    "open_only":      do_open_only,
    "quest":          do_skip_known,
    "search_bonus":   do_search_bonus,
    "streak_activity": do_skip_known,
    "app_checkin":    do_skip_known,
}


async def do_copilot_prompt(ctx: BrowserContext) -> bool:
    """Submit one Bing Chat / Copilot prompt — sometimes credited as a daily activity."""
    log("  -> copilot: submitting one prompt")
    tab = await ctx.new_page()
    try:
        await tab.goto("https://www.bing.com/chat?form=NTPCHB", wait_until="domcontentloaded", timeout=30_000)
        await tab.wait_for_timeout(4000)
        # Dismiss any onboarding modal.
        for label in ["Get started", "Continue", "Accept", "I accept", "Maybe later", "Later"]:
            try:
                await tab.get_by_role("button", name=re.compile(f"^{label}$", re.I)).click(timeout=1500)
            except Exception:
                pass
        # The chat input is a contenteditable div in modern Copilot, fall back to textarea.
        prompt = random.choice(COPILOT_PROMPTS)
        for selector in [
            "textarea#searchbox",
            "textarea[placeholder*='Ask']",
            "textarea[placeholder*='Message']",
            "div[contenteditable='true']",
            "cib-serp >>> cib-action-bar >>> textarea",
        ]:
            try:
                box = tab.locator(selector).first
                if await box.count() == 0:
                    continue
                await box.click(timeout=4000)
                await box.fill("")
                await box.type(prompt, delay=random.randint(30, 80))
                await jitter(0.5, 1.2)
                await box.press("Enter")
                await tab.wait_for_timeout(8000)
                return True
            except Exception:
                continue
        log("     copilot input not found; skipping (UI changed?)")
        return False
    except Exception as e:
        log(f"     copilot failed: {e}")
        return False
    finally:
        try:
            await tab.close()
        except Exception:
            pass


# ---- search quota --------------------------------------------------------

async def search_quota_status(page: Page) -> tuple[int, int, int, int]:
    """Returns (pc_earned, pc_cap, mobile_earned, mobile_cap). Tolerant of layout changes."""
    await page.goto(BREAKDOWN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    html = await page.content()
    pc = re.search(r"PC search[\s\S]{0,400}?(\d+)\s*/\s*(\d+)", html)
    mo = re.search(r"Mobile search[\s\S]{0,400}?(\d+)\s*/\s*(\d+)", html)
    pc_e, pc_c = (int(pc.group(1)), int(pc.group(2))) if pc else (0, 0)
    mo_e, mo_c = (int(mo.group(1)), int(mo.group(2))) if mo else (0, 0)
    if not pc and not mo:
        try:
            body = await page.locator("body").inner_text(timeout=5000)
            pc_local = re.search(r"(?:PC|电脑|桌面)\s*(?:Search|搜索)[\s\S]{0,120}?(\d+)\s*/\s*(\d+)", body, re.I)
            mo_local = re.search(r"(?:Mobile|移动端|手机)\s*(?:Search|搜索)[\s\S]{0,120}?(\d+)\s*/\s*(\d+)", body, re.I)
            if pc_local:
                pc_e, pc_c = int(pc_local.group(1)), int(pc_local.group(2))
            if mo_local:
                mo_e, mo_c = int(mo_local.group(1)), int(mo_local.group(2))
        except Exception:
            pass
    return pc_e, pc_c, mo_e, mo_c


async def _do_one_search(page: Page, query: str) -> bool:
    """One conservative typed Bing search with reading time."""
    try:
        await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=20_000)
        await jitter(1.2, 3.0)
        if not await submit_bing_search(page, query, human=True):
            log("     search box not found; stopping search batch")
            return False
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        await jitter(*SEARCH_DWELL_RANGE)
        # Scroll a bit, like a real user reading results.
        for _ in range(random.randint(1, 3)):
            await page.mouse.wheel(0, random.randint(300, 900))
            await jitter(0.8, 1.8)
        # Sometimes click first organic result and bounce back.
        if random.random() < 0.18:
            try:
                first_result = page.locator("li.b_algo h2 a").first
                if await first_result.count() > 0:
                    await first_result.click(timeout=4000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    await jitter(3.0, 7.0)
                    await page.go_back(wait_until="domcontentloaded", timeout=10_000)
                    await jitter(1.0, 2.4)
            except Exception:
                pass
        return True
    except Exception as e:
        log(f"     search '{query[:30]}…' failed: {type(e).__name__}")
        return False


async def run_search_quota(p, label: str, ua: str, cap: int, extra: int = 0) -> None:
    """Run only a small visible, typed search batch.

    No fast URL-fill path is used. If quota parsing is unavailable, the caller
    passes cap=0 and this does nothing unless explicit extra searches were
    requested.
    """
    search_n = 0
    if cap > 0:
        search_n += min(MAX_SEARCHES_PER_RUN, max(0, int(round(cap / 3))))
    if extra > 0:
        search_n += min(MAX_SEARCHES_PER_RUN, extra)
    log(f"  -> {label} searches: {search_n} typed (cap={cap}p, bonus={extra})")
    if search_n <= 0:
        return

    browser = await launch_browser(p, headless=True)
    try:
        viewport = {"width": 412, "height": 915} if "Mobile" in ua else {"width": 1280, "height": 860}
        ctx = await browser.new_context(
            storage_state=str(AUTH_FILE),
            user_agent=ua,
            viewport=viewport,
            locale="en-US",
        )
        page = await ctx.new_page()

        for i in range(search_n):
            q = random.choice(SEARCH_POOL)
            before = await read_points(page)
            if not await _do_one_search(page, q):
                log(f"     {label} stopping after failed search action")
                break
            credited, reason, _ = await wait_for_points_increase(page, before)
            if not credited:
                log(f"     {label} stopping after uncredited search: {reason}")
                break
            log(f"     {label}: {i + 1}/{search_n} credited ({reason})")
            if i + 1 < search_n:
                await jitter(8.0, 18.0)
    finally:
        await browser.close()


# ---- points read helpers -------------------------------------------------

async def read_points(page: Page) -> tuple[Optional[int], Optional[int]]:
    """Read (available, today) from old counters or the new dashboard/earn copy."""
    try:
        cached_bing = getattr(page, "_last_bing_available", None)
        live_bing = await read_bing_header_points(page)
        if live_bing is not None:
            cached_bing = max(cached_bing or 0, live_bing)
            try:
                setattr(page, "_last_bing_available", cached_bing)
            except Exception:
                pass
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2500)
        available = today = None
        try:
            body = await page.locator("body").inner_text(timeout=5000)
            available = parse_labeled_number(body, ["Available points", "可用积分"])
        except Exception:
            body = ""
        counters = page.locator("mee-rewards-counter-animation")
        n = await counters.count()
        values: list[int] = []
        for i in range(n):
            try:
                txt = await counters.nth(i).text_content(timeout=1500)
                v = parse_int(txt)
                if v is not None:
                    values.append(v)
            except Exception:
                continue
        if available is None and values:
            available = values[0]
        if values:
            today = values[2] if len(values) >= 3 else (values[-1] if len(values) > 1 else None)
        try:
            await page.goto(EARN_URL, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2500)
            earn_body = await page.locator("body").inner_text(timeout=5000)
            today = parse_labeled_number(earn_body, ["Today's points", "今日积分"]) or today
            available = parse_labeled_number(earn_body, ["Available points", "可用积分"]) or available
        except Exception:
            pass
        if cached_bing is not None:
            available = max(available or 0, cached_bing)
            try:
                setattr(page, "_last_bing_available", available)
            except Exception:
                pass
        return available, today
    except Exception:
        return None, None


async def goto_rewards(page: Page) -> bool:
    """Reload dashboard. Returns False if the page/context is dead."""
    try:
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        log(f"  goto_rewards failed: {type(e).__name__}: {str(e)[:120]}")
        return False
    # Slower wait + a scroll triggers the dashboard's lazy-loaded card lists.
    try:
        await page.wait_for_timeout(3500)
        await page.mouse.wheel(0, 600)
        await page.wait_for_timeout(800)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
    except Exception:
        pass
    return True


async def is_context_alive(ctx: BrowserContext) -> bool:
    try:
        # Cheap probe: just create and close a blank page.
        p = await ctx.new_page()
        await p.close()
        return True
    except Exception:
        return False


# ---- main loop -----------------------------------------------------------

async def _open_browser(p, headless: bool):
    """Launch a fresh browser + context + dashboard page, fully isolated from any
    dead predecessor. Returns (browser, ctx, page)."""
    browser = await launch_browser(p, headless=headless)
    ctx = await browser.new_context(
        storage_state=str(AUTH_FILE),
        user_agent=DESKTOP_UA,
        viewport={"width": 1280, "height": 860},
        locale="en-US",
    )
    page = await ctx.new_page()
    await goto_rewards(page)
    return browser, ctx, page


async def main_run(headless: bool, *, run_search_bonus: bool = False,
                   run_search_quota_tasks: bool = False,
                   run_copilot: bool = False) -> None:
    if not AUTH_FILE.exists():
        log("auth.json not found. Run with --login first.")
        sys.exit(2)

    async with async_playwright() as p:
        browser, ctx, page = await _open_browser(p, headless)

        async def ensure_alive():
            """If the context has died (e.g. quiz triggered a tab close that took
            it down), tear down and relaunch so the rest of the run can proceed."""
            nonlocal browser, ctx, page
            if await is_context_alive(ctx):
                return
            log("  ** context died — relaunching browser to continue **")
            try:
                await browser.close()
            except Exception:
                pass
            browser, ctx, page = await _open_browser(p, headless)

        avail_before, today_before = await read_points(page)
        log(f"Before: available={avail_before} today={today_before}")
        cards = await discover_rewards_cards(page)
        log(f"Discovered {len(cards)} earnable cards:")
        for c in cards:
            log(f"  [{c.kind:<14}] {urlparse(c.source_url).path:<11} {c.title[:50]:<52} +{c.points}p")
        bonus_searches = 0
        if run_search_bonus:
            for c in cards:
                if c.kind != "search_bonus":
                    continue
                progress = extract_progress(f"{c.aria} {c.text} {c.title}")
                if not progress:
                    bonus_searches = max(bonus_searches, 25)
                    continue
                earned, target = progress
                remaining_points = max(0, target - earned)
                bonus_searches = max(bonus_searches, min(35, int((remaining_points + 3) / 4)))

        ok = skipped = failed = 0
        stop_reason: Optional[str] = None
        for c in cards:
            if stop_reason:
                break
            handler = HANDLERS.get(c.kind)
            if handler is None:
                log(f"  !! no handler for kind={c.kind}: {c.title} — skipping")
                skipped += 1
                continue
            try:
                points_before_card = await read_points(page)
                done = await handler(ctx, page, c)
                if done is None:
                    skipped += 1
                else:
                    credited, reason, _ = await wait_for_credit(page, c, points_before_card)
                    if done and credited:
                        log(f"     credited: {reason}")
                        ok += 1
                    else:
                        log(f"     uncredited: {reason}")
                        failed += 1
                        stop_reason = f"stopped after uncredited task: {c.title[:80]}"
            except Exception as e:
                log(f"  !! error on {c.title}: {type(e).__name__}: {str(e)[:120]}")
                failed += 1
                stop_reason = f"stopped after task error: {c.title[:80]}"
            await ensure_alive()
            try:
                await goto_rewards(page)
            except Exception:
                await ensure_alive()
            if not stop_reason:
                await jitter(*TASK_PAUSE_RANGE)

        # Copilot prompt — opt-in because it is not tied to a visible Rewards card.
        if stop_reason:
            log(f"Safety stop: {stop_reason}")
        elif not run_copilot:
            log("Copilot prompt disabled; pass --copilot to run it.")
        else:
            await ensure_alive()
            try:
                await do_copilot_prompt(ctx)
            except Exception as e:
                log(f"  copilot prompt skipped: {type(e).__name__}: {str(e)[:120]}")
                await ensure_alive()

        # PC / Mobile quotas (+ human-style searches for the "100 extra points/day" bonus)
        if not stop_reason:
            pc_e, pc_c, mo_e, mo_c = await search_quota_status(page)
            log(f"Search quotas: PC {pc_e}/{pc_c}, Mobile {mo_e}/{mo_c}")
            if not run_search_quota_tasks:
                log("Search quota fill disabled; pass --search-quota to run small typed quota searches.")
            if not run_search_bonus:
                log("Search bonus extra searches disabled; pass --search-bonus to run small typed bonus searches.")
            elif bonus_searches:
                log(f"Search bonus target: {bonus_searches} typed PC searches")
            pc_cap = max(0, pc_c - pc_e) if run_search_quota_tasks else 0
            mo_cap = max(0, mo_c - mo_e) if run_search_quota_tasks else 0
            await run_search_quota(p, "PC", DESKTOP_UA, pc_cap, extra=bonus_searches if run_search_bonus else 0)
            await run_search_quota(p, "Mobile", MOBILE_UA, mo_cap, extra=0)
        else:
            log("Searches skipped because a previous visible task did not credit.")

        # Second sweep: some "Explore on Bing" / Daily-set cards unlock only after
        # finishing earlier ones. Re-scan and try the new ones.
        if stop_reason:
            log("Second sweep skipped because of the safety stop.")
            new_only = []
        else:
            log("Second sweep for newly-unlocked cards...")
            await ensure_alive()
            try:
                new_cards = await discover_rewards_cards(page)
            except Exception as e:
                log(f"  second sweep discovery failed: {e}")
                new_cards = []
            new_only = [c for c in new_cards if (c.title, c.href) not in {(x.title, x.href) for x in cards}]
        if new_only:
            log(f"  found {len(new_only)} new card(s) after first pass:")
            for c in new_only:
                if stop_reason:
                    break
                log(f"    [{c.kind:<14}] {urlparse(c.source_url).path:<11} {c.title[:50]:<52} +{c.points}p")
                handler = HANDLERS.get(c.kind)
                if not handler:
                    skipped += 1
                    continue
                try:
                    points_before_card = await read_points(page)
                    done = await handler(ctx, page, c)
                    if done is None:
                        skipped += 1
                    else:
                        credited, reason, _ = await wait_for_credit(page, c, points_before_card)
                    if done is not None and done and credited:
                        log(f"       credited: {reason}")
                        ok += 1
                    elif done is not None:
                        log(f"       uncredited: {reason}")
                        failed += 1
                        stop_reason = f"stopped after uncredited second-sweep task: {c.title[:80]}"
                except Exception as e:
                    log(f"    !! error on {c.title}: {type(e).__name__}: {str(e)[:120]}")
                    failed += 1
                    stop_reason = f"stopped after second-sweep task error: {c.title[:80]}"
                await ensure_alive()
                try:
                    await goto_rewards(page)
                except Exception:
                    await ensure_alive()
                if not stop_reason:
                    await jitter(*TASK_PAUSE_RANGE)
        else:
            log("  no new cards.")

        await goto_rewards(page)
        avail_after, today_after = await read_points(page)
        dav = (avail_after - avail_before) if (avail_after is not None and avail_before is not None) else 0
        dto = (today_after - today_before) if (today_after is not None and today_before is not None) else 0
        log("-" * 60)
        log(f"DONE. cards: {ok} ok / {failed} failed / {skipped} unhandled")
        if stop_reason:
            log(f"Safety stop reason: {stop_reason}")
        log(f"Available: {avail_before} -> {avail_after}  (delta {dav:+d})")
        log(f"Today:     {today_before} -> {today_after}  (delta {dto:+d})")

        await ctx.storage_state(path=str(AUTH_FILE))
        await browser.close()


async def dump_rewards(headless: bool) -> None:
    if not AUTH_FILE.exists():
        log("auth.json not found. Run with --login or --import-profile first.")
        sys.exit(2)
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=headless)
        ctx = await browser.new_context(
            storage_state=str(AUTH_FILE),
            user_agent=DESKTOP_UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = await ctx.new_page()
        for url in REWARDS_PAGES:
            log("-" * 60)
            log(f"DUMP {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3500)
            except Exception as e:
                log(f"  load failed: {type(e).__name__}: {str(e)[:160]}")
                continue
            log(f"  final url: {page.url}")
            cards = await discover_cards(page)
            log(f"  normalized cards: {len(cards)}")
            for c in cards[:80]:
                href = c.href[:90] if c.href else "-"
                log(f"    [{c.kind:<14}] +{c.points:<3} {c.title[:55]:<55} href={href}")
            try:
                candidates = await _collect_card_candidates(page)
            except Exception as e:
                log(f"  raw candidate dump failed: {type(e).__name__}: {str(e)[:120]}")
                continue
            interesting = []
            for item in candidates:
                text = clean_text(f"{item.get('aria', '')} {item.get('text', '')}")
                href = absolute_url(item.get("href", ""), page.url)
                if extract_points(text) > 0 or "earn" in text.lower() or "point" in text.lower():
                    interesting.append((text, href, item.get("selector", "")))
            log(f"  raw interesting clickables: {len(interesting)}")
            for text, href, selector in interesting[:120]:
                log(f"    raw text={text[:90]!r} href={href[:90]!r} selector={selector[:90]!r}")
        await browser.close()


async def trace_card(headless: bool, kind: str, *, search: bool = False, index: int = 0) -> None:
    if not AUTH_FILE.exists():
        log("auth.json not found. Run with --login or --import-profile first.")
        sys.exit(2)
    async with async_playwright() as p:
        browser = await launch_browser(p, headless=headless)
        ctx = await browser.new_context(
            storage_state=str(AUTH_FILE),
            user_agent=DESKTOP_UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        events: list[tuple[str, str, str]] = []

        def interesting_url(url: str) -> bool:
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            path = parsed.path.lower()
            query = parsed.query.lower()
            if path.endswith((".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico", ".woff", ".woff2")):
                return False
            return (
                ("rewards.bing.com" in host and (path in {"/dashboard", "/earn"} or "_rsc=" in query or "api" in path))
                or ("www.bing.com" in host and (
                    path in {"/", "/search"}
                    or "reportactivity" in path
                    or "rewardsapp" in path
                    or "xlsc.aspx" in path
                    or "ml2" in query
                    or "rwautoflyout" in query
                ))
                or ("bing.com" == host and (path in {"/", "/search"} or "ml2" in query))
            )

        def request_line(req) -> str:
            url = req.url[:500]
            if req.method.upper() == "POST" and "rewards.bing.com/earn" in req.url:
                headers = req.headers
                action = headers.get("next-action", "")
                referer = headers.get("referer", "")
                body = (req.post_data or "")[:500]
                url += f" next-action={action} referer={referer} body={body!r}"
            return url

        def attach(page: Page) -> None:
            page.on("request", lambda req: events.append(("REQ", req.method, request_line(req)))
                    if interesting_url(req.url) else None)
            page.on("response", lambda res: events.append(("RES", str(res.status), res.url[:500]))
                    if interesting_url(res.url) else None)

        page = await ctx.new_page()
        attach(page)
        try:
            before = await read_points(page)
            cards = await discover_rewards_cards(page)
            matching_cards = [c for c in cards if c.kind == kind]
            if index < 0 or index >= len(matching_cards):
                log(f"trace: no card with kind={kind!r} at index={index}; found {len(matching_cards)}")
                return
            card = matching_cards[index]
            log(f"trace: before available={before[0]} today={before[1]}")
            log(f"trace: card kind={card.kind} points={card.points} title={card.title[:90]}")
            log(f"trace: href={card.href[:300]}")
            log(f"trace: keyword={keyword_for(card)}")
            tab = await _click_card(page, card, ctx, return_same_page_on_click=True)
            if tab is None:
                log("trace: click returned no tab/page")
                return
            attach(tab)
            await tab.wait_for_timeout(2500)
            log(f"trace: clicked url={tab.url[:500]}")
            if search and card.kind == "explore_search":
                target = bing_search_url(keyword_for(card), card)
                log(f"trace: search target={target[:500]}")
                await tab.goto(target, wait_until="domcontentloaded", timeout=20_000)
                await tab.wait_for_timeout(7000)
                log(f"trace: after search url={tab.url[:500]}")
            elif search and card.kind == "search_bonus":
                await tab.wait_for_timeout(7000)
            try:
                if getattr(tab, "_rewards_auto_close", True):
                    await tab.close()
            except Exception:
                pass
            after = await read_points(page)
            log(f"trace: after available={after[0]} today={after[1]}")
            log("trace: network events")
            seen = set()
            for typ, meta, url in events:
                key = (typ, meta, url)
                if key in seen:
                    continue
                seen.add(key)
                log(f"  {typ:<3} {meta:<4} {url}")
        finally:
            await ctx.storage_state(path=str(AUTH_FILE))
            await browser.close()


def cli() -> None:
    # Force UTF-8 stdout on Windows so Unicode in card titles never crashes the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                    help="First-time interactive login; saves auth.json")
    ap.add_argument("--import-profile", action="store_true",
                    help="Import auth from the existing Edge/Chrome profile for this Windows user")
    ap.add_argument("--import-cdp", default=None,
                    help="Import auth from a running browser with remote debugging, e.g. http://127.0.0.1:9222")
    ap.add_argument("--profile-dir", default=None,
                    help="Profile directory to import, e.g. Default or 'Profile 1' (default: browser last_used)")
    ap.add_argument("--show", action="store_true",
                    help="Run non-headless (useful for debugging)")
    ap.add_argument("--dump-rewards", action="store_true",
                    help="Print dashboard/earn page diagnostics without running tasks")
    ap.add_argument("--trace-card", choices=[
        "explore_search", "daily_search", "search_bonus", "quest", "quiz",
        "image_puzzle", "image_creator", "open_only", "streak_activity",
        "app_checkin", "unknown",
    ], default=None, help="Trace one discovered card kind and print network diagnostics")
    ap.add_argument("--trace-search", action="store_true",
                    help="With --trace-card explore_search, also perform the derived Bing search")
    ap.add_argument("--trace-index", type=int, default=0,
                    help="With --trace-card, choose the Nth discovered card of that kind (default: 0)")
    ap.add_argument("--search-bonus", action="store_true",
                    help="Run small typed searches for the 100-point search bonus card")
    ap.add_argument("--search-quota", action="store_true",
                    help="Run small typed searches for readable PC/mobile search quotas")
    ap.add_argument("--copilot", action="store_true",
                    help="Submit one Copilot prompt (off by default; not tied to a visible Rewards card)")
    ap.add_argument("--browser", choices=["msedge", "chrome", "chromium"], default="msedge",
                    help="Which Playwright channel to drive (default: msedge)")
    ap.add_argument("--auth-file", default=None,
                    help="Path to the auth.json (default: auth_<browser>.json, falls back to auth.json)")
    args = ap.parse_args()

    global BROWSER_CHANNEL, AUTH_FILE
    BROWSER_CHANNEL = args.browser
    if args.auth_file:
        AUTH_FILE = Path(args.auth_file).resolve()
    else:
        per_browser = HERE / f"auth_{args.browser}.json"
        legacy = HERE / "auth.json"
        # Prefer per-browser file; if it doesn't exist but the legacy single file does
        # (and we're on the default channel), migrate to that for back-compat.
        if per_browser.exists():
            AUTH_FILE = per_browser
        elif legacy.exists() and args.browser == "msedge":
            AUTH_FILE = legacy
        else:
            AUTH_FILE = per_browser

    log("=" * 60)
    log(f"START  browser={BROWSER_CHANNEL} auth={AUTH_FILE.name} args={vars(args)}")
    try:
        if args.import_cdp:
            asyncio.run(import_from_cdp(args.import_cdp))
        elif args.import_profile:
            asyncio.run(import_existing_profile(args.profile_dir))
        elif args.login:
            asyncio.run(first_time_login())
        elif args.dump_rewards:
            asyncio.run(dump_rewards(headless=not args.show))
        elif args.trace_card:
            asyncio.run(trace_card(
                headless=not args.show,
                kind=args.trace_card,
                search=args.trace_search,
                index=args.trace_index,
            ))
        else:
            asyncio.run(main_run(
                headless=not args.show,
                run_search_bonus=args.search_bonus,
                run_search_quota_tasks=args.search_quota,
                run_copilot=args.copilot,
            ))
        log("EXIT 0")
    except KeyboardInterrupt:
        log("EXIT 130 (keyboard interrupt)")
        sys.exit(130)
    except Exception as e:
        import traceback
        log(f"FATAL: {type(e).__name__}: {e}")
        log(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    cli()
