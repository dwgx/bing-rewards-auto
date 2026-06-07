# AI handoff notes

Last updated: 2026-06-08 JST.

Project status: abandoned / no longer maintained. The user explicitly decided
to stop operating this project. Future agents should not continue automation
development unless the user clearly reopens the project with a new scope.

This document is for the next AI/operator taking over this repository. It is
intentionally explicit: preserve the workflow, constraints, user expectations,
and observed Microsoft Rewards behavior. Do not put cookies, auth JSON, account
IDs, or hidden prompts in this file.

## Current repository state

- Workspace: `D:\Project\bing-rewards-auto`
- Remote: `https://github.com/dwgx/bing-rewards-auto.git`
- Main branch after the latest documentation release: `v1.1.5`
- Key runtime files are ignored and must stay ignored:
  - `.venv/`
  - `auth_msedge.json`, `auth_chrome.json`, `auth_chromium.json`, `auth_*.json`
  - `last_run.log`, `logs/`
  - `.rewards_failures.json`
- Primary script: `bing_rewards.py`
- Primary docs:
  - `README.md`
  - `docs/earn-protocol-notes.md`
  - `docs/review-2026-06-08.md`

## User style and collaboration contract

The user writes mostly in Chinese and expects direct, hands-on execution. They
prefer the agent to run the project, observe the browser, keep records, and
avoid stopping at proposals. They often asks for "你自己跑", "计划然后开始",
"开 subagent", "review", "push", and "release".

Respond in Chinese unless the user explicitly asks otherwise. Be concrete:
state what was tested, what changed, what remains blocked, and what command or
release exists. Avoid vague reassurance.

Important: do not promise that automation can guarantee no account ban. The
project must be framed as conservative visible-task assistance and diagnostics,
not a way to evade Microsoft Rewards enforcement.

## Non-negotiable safety boundary

Microsoft's official terms define a Rewards Search as a user's manual search for
good-faith personal research, and explicitly exclude bot, macro, automated, or
fraudulent means. Microsoft Support also says not to use programs, bots, or
macros to assist searches and warns that repeated violations can lead to account
suspension and invalidated points.

Therefore:

- Do not add proxy rotation, fingerprint spoofing, CAPTCHA bypass, account
  farming, region/VPN tricks, or ban-evasion logic.
- Do not claim the tool is safe from bans.
- Do not run high-volume or rapid searches.
- Do not retry uncredited tasks in a loop.
- Do not treat ordinary Bing search points as proof that an Explore card
  completed.
- Keep default behavior conservative:
  - only act on visible Rewards tasks;
  - one action at a time;
  - wait after each action;
  - verify points, browse progress, completion text, or card state;
  - stop on unexpected/no-credit state;
  - cache same-day failures in `.rewards_failures.json`.

## Current observed Rewards state

Observed in visible Edge on 2026-06-08:

- `/dashboard` daily tasks were completed earlier.
- `/earn` "在必应上浏览" became readable as `0/18`, then advanced to `10/18`
  after the `保护重要的事物` Explore card.
- The completed card showed as `10 已完成`.
- These cards did not advance browse progress during testing:
  - `计划一次快速的短途旅行`
  - `规划你的未来`
  - `在 Bing 上找到优惠`
- Some of the failed cards still produced ordinary Bing search points (`+3`),
  but browse progress stayed unchanged. This is not Explore-card success.
- After `v1.1.4`, same-day failed cards are skipped on later runs.

Do not assume this account state is still current. Always run read-only
diagnostics first.

## Code map

Important functions in `bing_rewards.py`:

- `discover_cards(page)`: broad DOM scan for visible candidate tasks.
- `discover_dashboard_daily_cards(page)`: dashboard daily search/quiz discovery.
- `classify(aria, href, text)`: maps candidates into handler kinds.
- `_click_card(dashboard, card, ctx)`: only normal-run click path; direct
  navigation fallback is reserved for diagnostics.
- `do_explore_search(ctx, dashboard, card)`: clicks visible Explore card, types
  an inferred search in the opened Bing page, then reads `/earn` browse progress.
- `read_browse_progress(page)`: parses `/earn` label such as `在必应上浏览 10/18`.
- `wait_for_credit(page, card, before)`: verifies points, browse-progress
  success reason, or card disappearance.
- `remember_failed_card(card, reason)` / `card_failed_today(card)`: same-day
  failed-card cache.
- `trace_card(...)`: diagnostics helper that records relevant network requests.
- `do_quiz(...)`: current quiz implementation; needs further hardening.
- `HANDLERS`: maps known task kinds. `streak_activity`, `app_checkin`, and
  `quest` are intentionally skipped by default.

## Commands to use

Prefer these in this order:

```powershell
git status --short --ignored
.venv\Scripts\python.exe -m py_compile .\bing_rewards.py
.venv\Scripts\python.exe -u .\bing_rewards.py --dump-rewards --show
.venv\Scripts\python.exe -u .\bing_rewards.py --trace-card explore_search --show
```

Only run a full visible task pass when the user clearly wants execution:

```powershell
.venv\Scripts\python.exe -u .\bing_rewards.py --show
```

Opt-in commands remain off by default:

```powershell
.venv\Scripts\python.exe -u .\bing_rewards.py --search-quota --show
.venv\Scripts\python.exe -u .\bing_rewards.py --search-bonus --show
.venv\Scripts\python.exe -u .\bing_rewards.py --copilot --show
```

Release workflow:

```powershell
git diff --check
.venv\Scripts\python.exe -m py_compile .\bing_rewards.py
git add <changed tracked files>
git commit -m "<message>"
git tag -a vX.Y.Z -m "vX.Y.Z - <title>"
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z --repo dwgx/bing-rewards-auto --title "vX.Y.Z - <title>" --notes "<notes>"
```

Never commit ignored auth/log/cache files.

## Official sources to preserve

- Microsoft Services Agreement, published 2025-07-30 and effective 2025-09-30:
  `https://www.microsoft.com/en-us/servicesagreement`
- Microsoft Support, search limitations in Rewards:
  `https://support.microsoft.com/en-us/accounts-billing/rewards/limiting-your-searches-in-microsoft-rewards`
- Microsoft Support, earning Rewards points:
  `https://support.microsoft.com/en-us/account-billing/how-to-earn-microsoft-rewards-points-83179747-1807-7a5e-ce9d-a7c544327174`
- Playwright Python auth docs:
  `https://playwright.dev/python/docs/auth`
- Playwright Python network docs:
  `https://playwright.dev/python/docs/network`
- GitHub CLI release create manual:
  `https://cli.github.com/manual/gh_release_create`

## Current high-priority next work

1. Parse `/earn` Next.js/React Flight data.

   The stable task identity is likely `offerId` plus a hash/status/progress, not
   only href. Multiple Explore cards share the same `ML2PCR` href, so title+href
   is fragile.

2. Bind DOM cards to offer metadata.

   A `Card` should eventually carry fields such as `offer_id`, `hash`,
   `progress`, `is_completed`, `is_locked`, and `is_promotional`.

3. Improve credit verification.

   `wait_for_credit()` still navigates between pages and can treat "card
   missing from discovery" as success. The next version should read stable
   offer metadata before/after and only accept explicit completion/progress. For
   Explore tasks that did not advance browse progress, do not accept one missed
   discovery scan as success.

4. Harden quiz handling.

   Do not click arbitrary quiz options. Only answer when the current visible
   question exposes an option whose URL/metadata clearly marks the correct
   answer. Otherwise fail and stop.

5. Make trace tooling closer to the real handler.

   `trace_card(..., search=True)` still has an older direct search-goto path.
   Align it with `do_explore_search()` typed-search behavior or clearly label it
   as protocol-only diagnostics.

6. Replace the zero-point activated-card skip.

   `conservative_skip_reason()` currently skips zero-point activated Explore
   cards because one such card failed to advance progress on 2026-06-08. The
   better next step is failure-cache driven: if it has not failed today, allow
   one visible attempt, require browse progress, then cache and stop on failure.

7. Preserve the `rnoreward=1` lesson.

   Do not blanket-filter `rnoreward=1`. Version `v1.1.3` intentionally allowed
   visible daily links with that parameter because one credited. Use visibility,
   locked/completed markers, and post-action verification instead.

8. Add fixtures/tests.

   Capture sanitized HTML/Flight snippets for `/earn` and dashboard daily tasks.
   Unit-test parsing, classification, locked/completed filters, same-day failure
   cache, and safety-stop behavior.

## What not to do next

- Do not "solve" failed cards by retrying them repeatedly.
- Do not run full search quota by default.
- Do not click `app_checkin` or `streak_activity` outer buttons as if they were
  direct claim actions; traces showed no useful POST/reportActivity and no
  points change.
- Do not treat `reportActivity` alone as success.
- Do not hide risk language from docs.

## Last known successful release

`v1.1.4 - Track earn browse progress`

- Commit: `a688773`
- Release URL:
  `https://github.com/dwgx/bing-rewards-auto/releases/tag/v1.1.4`
- Verified:
  - `py_compile` passed;
  - visible Edge run advanced browse progress `0/18 -> 10/18`;
  - ordinary `+3` search points without browse progress were not accepted as
    Explore success;
  - follow-up visible run skipped same-day failed cards and made no repeated
    clicks.
