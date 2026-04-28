"""Bing Rewards auto farmer — Edge edition.

First run: `python bing_rewards.py --login`   # opens Edge, you sign in once, auth.json saved
Daily:     `python bing_rewards.py`           # headless, runs every still-earnable task

Handled task families (auto-discovered from the dashboard each run):
  - "Explore on Bing" category cards   : dashboard-click -> search topical keyword
  - Daily set / More activities links  : open or play (quiz, puzzle, this-or-that, search)
  - Bing Image Creator daily           : generate one image
  - Multi-question quizzes             : click the correct option (url has WQSCORE:1) per question
  - Image "Puzzle it"                  : click Skip (credits on skip)
  - PC search quota 90/90              : ~35 desktop searches with jitter
  - Mobile search quota 60/60          : ~25 mobile-UA searches with jitter

Auto-skips (with a logged reason) — flip them back on only if MS enables them for your market:
  - Cards marked "Offer is Locked" / "Available tomorrow" / "Earn -1 points"
  - Long-running punch cards (Sea of Thieves, etc.)
  - 3rd-party installs (Chrome, Edge mobile app, Bing Wallpaper, Rewards Extension)
  - Sweepstakes entries, Refer-a-friend, Redemption goals, Shop-to-earn
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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

REWARDS_URL = "https://rewards.bing.com/"
BREAKDOWN_URL = "https://rewards.bing.com/pointsbreakdown"

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


# ---- first-time login ----------------------------------------------------

async def first_time_login() -> None:
    log(f"Opening {BROWSER_CHANNEL}. Sign in to your Microsoft account in the browser window.")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=BROWSER_CHANNEL, headless=False, args=["--start-maximized"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 860})
        page = await ctx.new_page()
        # Go to rewards.bing.com — it will redirect to login or /welcome if not signed in.
        await page.goto(REWARDS_URL, wait_until="networkidle")
        await page.wait_for_timeout(3000)
        log(f"Landed on: {page.url}")
        # If already on the dashboard (user was logged in via Edge profile), we're done.
        # Otherwise wait for the user to sign in.
        needs_login = "/welcome" in page.url or "login." in page.url
        if needs_login:
            log("Not logged in. Please sign in in the browser window (up to 10 min)...")
            try:
                await page.wait_for_url(
                    lambda u: "rewards.bing.com" in u and "/welcome" not in u and "login." not in u,
                    timeout=600_000,
                )
                await page.wait_for_load_state("networkidle", timeout=30_000)
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
                await page.goto(REWARDS_URL, wait_until="networkidle", timeout=30_000)
            except PWTimeout:
                pass
        # Also hit bing.com to pick up search cookies.
        try:
            await page.goto("https://www.bing.com/", wait_until="networkidle", timeout=15_000)
        except PWTimeout:
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

SKIP_PATTERNS_ARIA = (
    "referral", "refer and earn", "紹介", "sweepstake", "entries",
    "install the", "set bing as your default", "bing wallpaper",
    "punch card", "ancient coin", "sea of thieves", "rewards extension",
    "redemption goal", "order history", "claim your gift", "shop to earn",
    "set goal", "目標", "ロボット",
)

SKIP_PATTERNS_HREF = (
    "sweepstakes/", "referandearn", "aka.ms/win", "workinprogress",
    "punchcard", "microsoft-store", "goal/all", "orderhistory",
    "/redeem", "/redeemgoal", "xbox.com/rewards",
)


@dataclass
class Card:
    title: str
    points: int
    href: str
    aria: str
    kind: str  # explore_search | quiz | daily_search | image_creator | image_puzzle | open_only | unknown


def classify(aria: str, href: str) -> str:
    low_a, low_h = aria.lower(), href.lower()
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


async def discover_cards(page: Page) -> list[Card]:
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


# ---- task handlers -------------------------------------------------------

SEARCH_KEYWORDS = [
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
    text = card.aria + " " + card.title
    for pat, kw in SEARCH_KEYWORDS:
        if pat.search(text):
            return kw
    return random.choice(SEARCH_POOL)


async def _click_card(dashboard: Page, card: Card, ctx: BrowserContext) -> Optional[Page]:
    """Click the dashboard card and return the popped-up tab (or a fresh tab on card.href fallback)."""
    try:
        async with ctx.expect_page(timeout=15_000) as new_page_info:
            await dashboard.locator(f'a[aria-label^="{card.title.replace(chr(34), "")[:60]}"]').first.click()
        return await new_page_info.value
    except Exception:
        # Fallback: just open the href
        try:
            tab = await ctx.new_page()
            await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
            return tab
        except Exception as e:
            log(f"     fallback navigation failed: {e}")
            return None


async def do_explore_search(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    log(f"  -> explore_search: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
        return False
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=20_000)
        await tab.wait_for_timeout(2500)
        kw = keyword_for(card)
        box = tab.get_by_role("combobox", name=re.compile("Enter your search here", re.I))
        await box.wait_for(timeout=10_000)
        await box.fill(kw)
        await box.press("Enter")
        await tab.wait_for_load_state("domcontentloaded", timeout=20_000)
        await jitter(2, 3.5)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def do_daily_search(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    """Daily-set search link — auto-credits on page load."""
    log(f"  -> daily_search: {card.title} ({card.points}p)")
    tab = await ctx.new_page()
    try:
        await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
        await tab.wait_for_timeout(4000)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def do_open_only(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    log(f"  -> open_only: {card.title} ({card.points}p)")
    tab = await ctx.new_page()
    try:
        await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
        await tab.wait_for_timeout(4000)
        return True
    except Exception as e:
        log(f"     failed: {e}")
        return False
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def do_quiz(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    """3-to-10-question multiple choice. Pick the option whose URL has WQSCORE:1.

    Hardened: stops on stale tabs / detached locators / context death so a single
    bad quiz can't take the whole run down with it. Returns True if at least one
    answer was clicked (which already credits partial points)."""
    log(f"  -> quiz: {card.title} ({card.points}p)")
    try:
        tab = await ctx.new_page()
    except Exception as e:
        log(f"     could not open quiz tab: {e}")
        return False
    answered = 0
    consecutive_misses = 0
    try:
        await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
        await tab.wait_for_timeout(3500)
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
            # Find the correct-answer link.
            target = None
            try:
                for selector in [
                    'a[href*="WQSCORE%3A%221%22"]',
                    'a[href*="WQCI"]',  # any option as fallback
                ]:
                    loc = tab.locator(selector)
                    if await loc.count() > 0:
                        target = loc.first
                        break
            except Exception as e:
                log(f"     locator query died at q{qi + 1}: {type(e).__name__}; stopping.")
                break
            if target is None:
                consecutive_misses += 1
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
        return answered > 0
    except Exception as e:
        log(f"     quiz outer error ({answered} answered): {type(e).__name__}: {e}")
        return answered > 0
    finally:
        try:
            if not tab.is_closed():
                await tab.close()
        except Exception:
            pass


async def do_image_puzzle(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    """Image jigsaw: 'Skip puzzle' link in the top-right credits the task."""
    log(f"  -> image_puzzle: {card.title} ({card.points}p)")
    tab = await ctx.new_page()
    try:
        await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
        await tab.wait_for_timeout(3500)
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
            await tab.close()
        except Exception:
            pass


async def do_image_creator(ctx: BrowserContext, dashboard: Page, card: Card) -> bool:
    log(f"  -> image_creator: {card.title} ({card.points}p)")
    tab = await _click_card(dashboard, card, ctx)
    if tab is None:
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
    pc_e, pc_c = (int(pc.group(1)), int(pc.group(2))) if pc else (0, 90)
    mo_e, mo_c = (int(mo.group(1)), int(mo.group(2))) if mo else (0, 60)
    return pc_e, pc_c, mo_e, mo_c


async def _do_one_search(page: Page, query: str, human: bool) -> None:
    """Either a fast goto (for quota fill) or a human-style typed search (for bonus)."""
    if not human:
        try:
            await page.goto(f"https://www.bing.com/search?q={quote(query)}&form=QBLH",
                            wait_until="domcontentloaded", timeout=20_000)
        except PWTimeout:
            pass
        await jitter(1.5, 3.0)
        return

    # Human mode: visit homepage, type into search box, scroll, sometimes click result.
    try:
        await page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=20_000)
        await jitter(0.8, 1.6)
        box = page.get_by_role("combobox", name=re.compile("search", re.I)).first
        if await box.count() == 0:
            box = page.locator("textarea[name='q'], input[name='q']").first
        await box.click(timeout=8000)
        await box.fill("")
        await box.type(query, delay=random.randint(40, 110))
        await jitter(0.3, 0.9)
        await box.press("Enter")
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        await jitter(1.6, 2.6)
        # Scroll a bit, like a real user reading results.
        for _ in range(random.randint(1, 3)):
            await page.mouse.wheel(0, random.randint(300, 900))
            await jitter(0.4, 1.0)
        # Sometimes click first organic result and bounce back.
        if random.random() < 0.25:
            try:
                first_result = page.locator("li.b_algo h2 a").first
                if await first_result.count() > 0:
                    await first_result.click(timeout=4000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    await jitter(2.0, 4.0)
                    await page.go_back(wait_until="domcontentloaded", timeout=10_000)
                    await jitter(0.8, 1.5)
            except Exception:
                pass
    except Exception as e:
        # One bad search shouldn't kill the whole batch.
        log(f"     (search '{query[:30]}…' had a glitch: {type(e).__name__})")


async def run_search_quota(p, label: str, ua: str, cap: int, extra: int = 0) -> None:
    """`cap` = remaining points to fill (3p/search). `extra` = bonus searches for the
    "100 extra points/day" accumulator that ticks up on searches beyond the regular cap.

    Strategy: do `cap // 3 + 3` fast searches (URL navigation) to satisfy the
    base 90/60 cap, then `extra` human-style searches (typed in the search box,
    with scroll + occasional result click) since those credit the bonus more
    reliably than rapid-fire URL hits.
    """
    fast_n = max(0, int(round(cap / 3)) + 3) if cap > 0 else 0
    bonus_n = extra
    log(f"  -> {label} searches: {fast_n} fast + {bonus_n} human (cap={cap}p, bonus={bonus_n})")

    browser = await p.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
    try:
        viewport = {"width": 412, "height": 915} if "Mobile" in ua else {"width": 1280, "height": 860}
        ctx = await browser.new_context(
            storage_state=str(AUTH_FILE),
            user_agent=ua,
            viewport=viewport,
            locale="en-US",
        )
        page = await ctx.new_page()

        # Phase 1: fast cap-fill.
        for i in range(fast_n):
            q = random.choice(SEARCH_POOL) + f" {random.randint(1000, 9999)}"
            await _do_one_search(page, q, human=False)
            if (i + 1) % 10 == 0:
                log(f"     {label} fast: {i + 1}/{fast_n}")

        # Phase 2: human bonus searches. Slower (~10-15s each), spaced out.
        if bonus_n > 0:
            await jitter(2.5, 4.5)
            log(f"     {label} starting {bonus_n} human searches (~10s each)")
            for i in range(bonus_n):
                q = random.choice(SEARCH_POOL)  # No random suffix — keep query natural.
                await _do_one_search(page, q, human=True)
                if (i + 1) % 5 == 0:
                    log(f"     {label} human: {i + 1}/{bonus_n}")
    finally:
        await browser.close()


# ---- points read helpers -------------------------------------------------

async def read_points(page: Page) -> tuple[Optional[int], Optional[int]]:
    """Read (available, today). The header cards use <mee-rewards-counter-animation>
    custom elements in card order: Available, Today's referral, Today's points."""
    def parse(x):
        if not x:
            return None
        m = re.search(r"\d[\d,]*", str(x))
        return int(m.group(0).replace(",", "")) if m else None
    try:
        await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2500)
        counters = page.locator("mee-rewards-counter-animation")
        n = await counters.count()
        values: list[int] = []
        for i in range(n):
            try:
                txt = await counters.nth(i).text_content(timeout=1500)
                v = parse(txt)
                if v is not None:
                    values.append(v)
            except Exception:
                continue
        # Layout: counters[0]=available, [1]="0 / 50" referral, [2]=today's points,
        # [3]=streak count etc. Take [0] and [2].
        if not values:
            return None, None
        available = values[0]
        # Today's points is the 3rd parsable counter (after referral "0/50").
        today = values[2] if len(values) >= 3 else (values[-1] if len(values) > 1 else None)
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
    browser = await p.chromium.launch(channel=BROWSER_CHANNEL, headless=headless)
    ctx = await browser.new_context(
        storage_state=str(AUTH_FILE),
        user_agent=DESKTOP_UA,
        viewport={"width": 1280, "height": 860},
        locale="en-US",
    )
    page = await ctx.new_page()
    await goto_rewards(page)
    return browser, ctx, page


async def main_run(headless: bool) -> None:
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
        await goto_rewards(page)

        cards = await discover_cards(page)
        log(f"Discovered {len(cards)} earnable cards:")
        for c in cards:
            log(f"  [{c.kind:<14}] {c.title[:50]:<52} +{c.points}p")

        ok = skipped = failed = 0
        for c in cards:
            handler = HANDLERS.get(c.kind)
            if handler is None:
                log(f"  !! no handler for kind={c.kind}: {c.title} — skipping")
                skipped += 1
                continue
            try:
                done = await handler(ctx, page, c)
                ok += int(bool(done))
                failed += int(not done)
            except Exception as e:
                log(f"  !! error on {c.title}: {type(e).__name__}: {str(e)[:120]}")
                failed += 1
            await ensure_alive()
            try:
                await goto_rewards(page)
            except Exception:
                await ensure_alive()
            await jitter(1, 2)

        # Copilot prompt — daily Copilot activity sometimes credits 5-15p.
        await ensure_alive()
        try:
            await do_copilot_prompt(ctx)
        except Exception as e:
            log(f"  copilot prompt skipped: {type(e).__name__}: {str(e)[:120]}")
            await ensure_alive()

        # PC / Mobile quotas (+ human-style searches for the "100 extra points/day" bonus)
        pc_e, pc_c, mo_e, mo_c = await search_quota_status(page)
        log(f"Search quotas: PC {pc_e}/{pc_c}, Mobile {mo_e}/{mo_c}")
        # Empirically: fast URL searches credit the cap; the 100-bonus accumulator
        # only credits human-style searches reliably. Bump bonus generously now
        # that those searches type into the box and scroll instead of just GET.
        await run_search_quota(p, "PC", DESKTOP_UA, max(0, pc_c - pc_e), extra=40)
        await run_search_quota(p, "Mobile", MOBILE_UA, max(0, mo_c - mo_e), extra=25)

        # Second sweep: some "Explore on Bing" / Daily-set cards unlock only after
        # finishing earlier ones. Re-scan and try the new ones.
        log("Second sweep for newly-unlocked cards...")
        await ensure_alive()
        try:
            await goto_rewards(page)
            new_cards = await discover_cards(page)
        except Exception as e:
            log(f"  second sweep discovery failed: {e}")
            new_cards = []
        new_only = [c for c in new_cards if (c.title, c.href) not in {(x.title, x.href) for x in cards}]
        if new_only:
            log(f"  found {len(new_only)} new card(s) after first pass:")
            for c in new_only:
                log(f"    [{c.kind:<14}] {c.title[:50]:<52} +{c.points}p")
                handler = HANDLERS.get(c.kind)
                if not handler:
                    skipped += 1
                    continue
                try:
                    if await handler(ctx, page, c):
                        ok += 1
                    else:
                        failed += 1
                except Exception as e:
                    log(f"    !! error on {c.title}: {type(e).__name__}: {str(e)[:120]}")
                    failed += 1
                await ensure_alive()
                try:
                    await goto_rewards(page)
                except Exception:
                    await ensure_alive()
                await jitter(1, 2)
        else:
            log("  no new cards.")

        await goto_rewards(page)
        avail_after, today_after = await read_points(page)
        dav = (avail_after or 0) - (avail_before or 0) if (avail_after and avail_before) else 0
        dto = (today_after or 0) - (today_before or 0) if (today_after and today_before) else 0
        log("-" * 60)
        log(f"DONE. cards: {ok} ok / {failed} failed / {skipped} unhandled")
        log(f"Available: {avail_before} -> {avail_after}  (delta {dav:+d})")
        log(f"Today:     {today_before} -> {today_after}  (delta {dto:+d})")

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
    ap.add_argument("--show", action="store_true",
                    help="Run non-headless (useful for debugging)")
    ap.add_argument("--browser", choices=["msedge", "chrome"], default="msedge",
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
        if args.login:
            asyncio.run(first_time_login())
        else:
            asyncio.run(main_run(headless=not args.show))
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
