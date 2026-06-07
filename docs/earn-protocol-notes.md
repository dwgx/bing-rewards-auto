# Rewards `/earn` protocol notes

This file records observed behavior from local manual/trace runs. Do not put
cookies, auth JSON, or account identifiers here.

## Conservative run rules

- Only act on tasks that are visible on the Rewards page.
- Do one action at a time.
- After each action, verify points or card-state change.
- Stop on uncredited or unexpected state.
- Do not repeat the same uncredited action in the same run.

## Explore on Bing

Observed on 2026-06-08.

Flow:

1. Click the visible `/earn` Explore card.
2. Rewards sends a server action:
   - `POST https://rewards.bing.com/earn`
   - `Next-Action: 70babbc81d2724f60d29a95c03b3d739cba77cea92`
   - Body shape:
     `["<hash>",11,{"offerid":"ENUS_flight_exploreonbing_activation_Evergreen","isPromotional":"$undefined","timezoneOffset":"-540"}]`
3. The opened Bing page must keep Rewards parameters such as:
   - `form=ML2PCR`
   - `OCID=ML2PCR`
   - `PUBL=RewardsDO`
   - `CREA=ML2PCR`
   - `PC=ML2PCR`
   - `rwAutoFlyout=exb`
4. The Bing search page then sends:
   - `POST https://www.bing.com/rewardsapp/reportActivity?...form=ML2PCR...rwAutoFlyout=exb...`
   - `GET https://www.bing.com/rewardsapp/flyout?...rwAutoFlyout=exb`

The implementation now keeps the tab opened by the visible Rewards card click
and types the inferred topic into the Bing search box. The card's own URL still
has to carry `form=ML2PCR` / `rwAutoFlyout=exb`; final success is not inferred
from the request alone.

Recent trace results:

- `00:50:43`, first Explore card: card click and `reportActivity` succeeded,
  but available/today points stayed `8510/27`.
- `00:52:47`, second Explore card: card click and `reportActivity` succeeded,
  but available/today points stayed `8510/27`.
- `02:00:54`, visible Explore card was tried after the dashboard quiz completed;
  no points/card-state change was detected, so the run safety-stopped.

Interpretation: the protocol was followed, but the account did not receive more
points for these repeated Explore cards during this session. The safe behavior is
to stop instead of retrying.

## Dashboard daily activities

Observed on 2026-06-08.

- The dashboard "每日活动" section is a collapsed disclosure. Its child links exist
  in the DOM while the panel height is `0`, so automation must first expand the
  visible disclosure button and only then act on visible links.
- Some visible dashboard daily search links contain `rnoreward=1`; these can be
  already-completed/display-only links and are filtered out as non-earnable.
- The remaining `form=dsetqu` Bing quiz used standard `.btq_opt` answer links.
  Correct choices were identified from the highest cumulative `WQSCORE` on the
  currently visible question card, then the visible "下一个" button advanced the
  quiz.
- Result on this account: available/today increased `9366/880 -> 9376/890`, and
  `/earn` progress moved to `活动: 1/3`.

## Daily streak and Bing app check-in

Observed on 2026-06-08.

- `streak_activity` (`每日连签活动 +30`) outer button click produced no point
  change and no useful POST/reportActivity request.
- `app_checkin` (`必应应用连签 +5`) outer button click produced no point change
  and no useful POST/reportActivity request.

Interpretation: these outer controls are not direct claim actions on desktop.
They remain detected but skipped by default.

## Open questions

- Quest pages should be traced through their child tasks before automation.
- Search bonus should remain opt-in and progress-driven.
- App check-in may require a real mobile app session; desktop automation should
  not fake it.
