# Bing Rewards 自动签到

每天自动扫 `rewards.bing.com` 上所有还能领的积分任务。**支持 Edge、Chrome 和 Playwright Chromium**，可独立各一份登录态、独立运行。

## 自动识别的任务

脚本每次运行都会从 dashboard DOM 里实时扫出**所有 "Earn N points" 且未过期**的卡片，按类型分发处理：

| 类型 | 识别方式 | 处理方式 |
|---|---|---|
| **Explore on Bing** 分类卡 | `rwAutoFlyout=exb` | 点卡片 → 新 tab 搜主题关键词 |
| **每日任务 · 搜索** | URL 含 `form=ML2X*` / `tgrew*` / `ML1*` | 开链接即得分 |
| **每日任务 · Quiz** (3 题选择) | URL 含 `form=dsetqu` / `ML2BF1` | 逐题点正确答案（URL 里 `WQSCORE:1` 的） |
| **图片拼图 Puzzle it** | `spotlight/imagepuzzle` | 点 "Skip puzzle" 即得分 |
| **Bing Image Creator 每日** | `images/create` | 自动写 prompt 生成一张图 |
| **其他 More Activities** 链接 | 兜底 `bing.com` | 打开等 4 秒 |
| **PC 搜索** (0→90p) | pointsbreakdown 扫余量 | ~35 次随机词 + 25 次额外凑 100-bonus |
| **Mobile 搜索** (0→60p) | 手机 UA + 小视口 | ~25 次随机词 + 15 次额外凑 100-bonus |
| **"Earn 100 extra points/day"** | 累积器 | 由 PC/Mobile 额外搜索带动 |

## 自动跳过（带原因）

通过 aria-label / href 的 pattern 过滤：
- `Offer is Locked` / `Available tomorrow` / `Earn -1 points` — 还未解锁
- Sea of Thieves / punch card / ancient coin — 跨天任务
- Install the (Chrome/Edge mobile/Rewards Extension/Bing Wallpaper) — 需装 app
- sweepstakes / 抽奖 / entries — 抽奖入口
- Refer and Earn / 紹介 — 邀请好友
- 目標 / Set goal / Redeem — 兑换目标

## 首次使用

前置：Python 3.10+ + Microsoft **Edge** 或 **Chrome**（任一即可，两者都装也行）。

`setup*.bat` 会先尝试复用你当前 Windows 用户的浏览器 profile 登录态，导出到 `auth_*.json`。如果浏览器 profile 正在被打开的浏览器锁住，会自动退回到交互式登录。

### Edge 版

```
双击 setup.bat
```

优先从当前 Edge profile 导入登录态，成功后 cookie 存到 `auth_msedge.json`。如果导入失败，会弹出 Edge 让你登一次。

如果你 Edge 已经登录 Microsoft，但导入时提示 profile 被占用：

```
双击 import-edge-cookies.bat
```

它会让你关闭所有 Edge 窗口，然后用同一个 Edge profile 带调试端口重新打开 Rewards，并导出 `auth_msedge.json`。

### Chrome 版

```
双击 setup-chrome.bat
```

同上，但用 Chrome 登录，cookie 存到 `auth_chrome.json`。

### Chromium 兜底

```
双击 setup-chromium.bat
```

使用 Playwright 自带 Chromium 登录，cookie 存到 `auth_chromium.json`。适合本机 Edge/Chrome channel 被策略、崩溃或 profile 锁挡住时使用。

> 两套 auth 文件互不干扰；想用哪边跑就执行对应的 `run-*.bat`。

## 日常运行

```
双击 run.bat            ← Edge
双击 run-chrome.bat     ← Chrome
双击 run-chromium.bat   ← Playwright Chromium
```

如果 `run.bat` 找不到 Edge 登录态，会先尝试导入 Edge profile；导入失败时会提示你使用 `import-edge-cookies.bat`，再退回交互登录或 Chromium 兜底。

控制台会实时显示进度，同时写入 `logs/run_YYYYMMDD_HHMMSS.log`（每次一份带时间戳）+ `last_run.log`（最近一次的副本）。跑完按任意键关窗，60 秒不操作自动关。

如果脚本崩了，窗口不会闪退，错误堆栈会留在窗口里 + 日志里。

典型输出：
```
[11:00:02] START  browser=msedge auth=auth_msedge.json
[11:00:05] Before: available=1671 today=0
[11:00:05] Discovered 4 earnable cards:
[11:00:05]   [explore_search] Shine bright                                +10p
[11:00:05]   [daily_search  ] Cultural dances                             +10p
[11:00:05]   [daily_search  ] Winter bliss in Vancouver                   +10p
[11:00:05]   [quiz          ] Sacred Peak?                                +10p
...
[11:08:42] DONE. cards: 4 ok / 0 failed / 0 unhandled
[11:08:42] Available: 1671 -> 1921  (delta +250)
[11:08:42] Today:     0 -> 250  (delta +250)
```

## 设定每天自动跑（Windows Task Scheduler）

```powershell
schtasks /create /tn "BingRewardsEdge"   /tr "D:\Project\bing-rewards-auto\run.bat --quiet"        /sc daily /st 11:00 /f
schtasks /create /tn "BingRewardsChrome" /tr "D:\Project\bing-rewards-auto\run-chrome.bat --quiet" /sc daily /st 11:30 /f
```

> `--quiet` 跳过结尾的暂停，让窗口任务结束直接关闭（适合定时任务）。

取消：
```powershell
schtasks /delete /tn "BingRewardsEdge" /f
```

## 文件总览

| 文件 | 作用 |
|---|---|
| `bing_rewards.py` | 主脚本，支持 `--browser msedge|chrome` |
| `setup.bat` / `setup-chrome.bat` / `setup-chromium.bat` | 首次装依赖 + 导入/登录 |
| `run.bat` / `run-chrome.bat` / `run-chromium.bat` | 日常跑（带日志/暂停） |
| `import-edge-cookies.bat` | 从现有 Edge profile/CDP 导出 `auth_msedge.json` |
| `requirements.txt` | Python 依赖（仅需 playwright） |
| `auth_msedge.json` / `auth_chrome.json` / `auth_chromium.json` | 登录 state（脚本生成，**禁止外传**） |
| `logs/run_*.log` | 每次运行的完整日志 |
| `last_run.log` | 最近一次的日志副本 |
| `.gitignore` | 防 auth/log 被提交 |

## 调试 & 扩展

```
python bing_rewards.py --show                      # 不无头跑，看浏览器实际操作
python bing_rewards.py --browser chrome --show     # Chrome 版可视化
python bing_rewards.py --browser chromium --show   # Chromium 兜底版可视化
python bing_rewards.py --import-profile            # 从当前 Edge profile 导入 auth_msedge.json
python bing_rewards.py --import-profile --browser chrome
python bing_rewards.py --import-cdp http://127.0.0.1:9222
python bing_rewards.py --auth-file path/to/x.json  # 自定义 auth 路径
```

加新任务类型：在 `classify()` 里加 URL pattern → 在 `HANDLERS` 映射 handler 函数。

## 安全注意

- `auth_*.json` 是你 MS 账户登录态，能完整代表你的账号。**别分享、别上云盘**
- 脚本不会下单 / 不会兑换 / 不会发消息 / 不改账户设置 — 只做"搜索 / 浏览 / 生成图片"这些等同于浏览 Bing 的动作
- Cookie 一般可用数月。被踢出（登录页又出现）就重跑对应的 `setup-*.bat`
