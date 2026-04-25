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
    "weather forecast tomorrow", "python async tutorial", "best mechanical keyboard 2026",
    "how to make sourdough", "rtx 5070 ti review", "cheap flights to tokyo",
    "healthy breakfast ideas", "learn rust programming", "tesla stock price",
    "best coffee near me", "iphone 17 rumors", "how to tie a tie",
    "world cup schedule", "new movies this week", "best vr games 2026",
    "home workout routine", "recipe for chocolate chip cookies", "origin of halloween",
    "stretching routine for back pain", "how to fix a leaky faucet", "cat breeds friendly",
    "easy origami animals", "history of rome", "best noise cancelling headphones",
    "learn japanese hiragana", "git rebase vs merge", "fastest electric cars",
    "cherry blossom season japan", "budget gaming laptop 2026", "beginner yoga poses",
    "how to start a garden", "best indie games steam", "easy dinner recipes",
    "photography composition tips", "how to sleep better", "solar panel costs",
    "cryptocurrency explained simply", "best pizza toppings", "vintage camera brands",
    "how to meditate daily", "pomodoro technique focus", "markdown cheat sheet",
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
    """3-question multiple choice. We iterate: pick the option whose href carries WQSCORE:1."""
    log(f"  -> quiz: {card.title} ({card.points}p)")
    tab = await ctx.new_page()
    try:
        await tab.goto(card.href, wait_until="domcontentloaded", timeout=30_000)
        await tab.wait_for_timeout(3500)
        # Dismiss any consent banner.
        for _ in range(2):
            try:
                await tab.get_by_role("button", name=re.compile("Accept|同意|同意する", re.I)).click(timeout=1500)
            except Exception:
                break
        # Loop through up to 10 questions (safety cap; usual is 3).
        for qi in range(10):
            # Correct answer link pattern: URL contains WQSCORE%3A%221%22.
            target = None
            for selector in [
                'a[href*="WQSCORE%3A%221%22"]',
                'a[href*="WQSCORE%3A%221%22"][aria-disabled="false"]',
            ]:
                loc = tab.locator(selector)
                if await loc.count() > 0:
                    target = loc.first
                    break
            if target is None:
                # Fallback: any quiz option link.
                options = tab.locator('a[href*="WQCI"]')
                if await options.count() == 0:
                    log(f"     no more question options at q{qi + 1}; done.")
                    break
                target = options.first
            # Scroll into view and click (quiz answers can be below the fold).
            try:
                await target.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                await target.click(timeout=10_000)
            except Exception:
                # Force-click via JS if the element is obscured by overlays.
                try:
                    await target.evaluate("el => el.click()")
                except Exception:
                    log(f"     could not click q{qi + 1} option; skipping.")
                    break
            try:
                await tab.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PWTimeout:
                pass
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


async def run_search_quota(p, label: str, ua: str, cap: int, extra: int = 0) -> None:
    """`cap` = remaining points to fill (3p/search). `extra` = bonus searches for the
    "100 extra points/day" accumulator that ticks up on searches beyond the regular cap."""
    n = max(12, int(round(cap / 3)) + 3) + extra
    log(f"  -> {label} searches: {n} queries (cap-fill ~{cap // 3 + 3}, +{extra} bonus)")
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
        for i in range(n):
            q = random.choice(SEARCH_POOL) + f" {random.randint(1000, 9999)}"
            try:
                await page.goto(f"https://www.bing.com/search?q={quote(q)}&form=QBLH",
                                wait_until="domcontentloaded", timeout=20_000)
            except PWTimeout:
                pass
            await jitter(1.5, 3.0)
            if (i + 1) % 10 == 0:
                log(f"     {label}: {i + 1}/{n} done")
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


async def goto_rewards(page: Page) -> None:
    await page.goto(REWARDS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1800)


# ---- main loop -----------------------------------------------------------

async def main_run(headless: bool) -> None:
    if not AUTH_FILE.exists():
        log("auth.json not found. Run with --login first.")
        sys.exit(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=BROWSER_CHANNEL, headless=headless)
        ctx = await browser.new_context(
            storage_state=str(AUTH_FILE),
            user_agent=DESKTOP_UA,
            viewport={"width": 1280, "height": 860},
            locale="en-US",
        )
        page = await ctx.new_page()

        await goto_rewards(page)
        avail_before, today_before = await read_points(page)
        log(f"Before: available={avail_before} today={today_before}")

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
                await goto_rewards(page)
                await jitter(1, 2)
            except Exception as e:
                log(f"  !! error on {c.title}: {e}")
                failed += 1

        # PC / Mobile quotas (+ extra searches for the "100 extra points/day" bonus accumulator)
        pc_e, pc_c, mo_e, mo_c = await search_quota_status(page)
        log(f"Search quotas: PC {pc_e}/{pc_c}, Mobile {mo_e}/{mo_c}")
        # Bonus search count: enough so that at ~1pt-per-extra-search we top off 100/100.
        # Empirically the bonus credits slowly so we go generous: +25 PC, +15 Mobile.
        if pc_e < pc_c:
            await run_search_quota(p, "PC", DESKTOP_UA, pc_c - pc_e, extra=25)
        else:
            await run_search_quota(p, "PC-bonus", DESKTOP_UA, 0, extra=25)
        if mo_e < mo_c:
            await run_search_quota(p, "Mobile", MOBILE_UA, mo_c - mo_e, extra=15)
        else:
            await run_search_quota(p, "Mobile-bonus", MOBILE_UA, 0, extra=15)

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
