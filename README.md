# MySlot · 自己管理的约见日历

这是公开的预约意向网页。朋友选日期与时间后复制消息给你；**不会自动确认预约或向你的日历写事件**。你确认后自己在日历里记录或标上 `【休息】`。公开输出只有可约结果和你主动标记的漫展名称，不含私人事件或日历名。

## 第一次更新 GitHub

1. 下载并解压完整 ZIP，把**里面的文件**放在仓库 `lolinrl/myslot` 的根目录：`build.py`、`config.json`、`settings.html`、`site/index.html`、`tests/`、`.github/workflows/pages.yml` 等。Mac Finder 按 **⌘⇧.** 可显示 `.github`。删除旧的生成目录 `dist/`；不要提交账户密码。
2. 仓库设置 **Settings → Pages → Source** 选 **GitHub Actions**。已设置过就不用改。
3. 在 `settings.html` 勾选需要连接的账户；仓库 **Settings → Secrets and variables → Actions → Secrets** 分别填写这些账户自己的凭据与要监控的日历。**iCloud**：`ICLOUD_USER`（Apple 账号）、`ICLOUD_APP_PASSWORD`（App 专用密码）、`MYSLOT_BUSY_CALENDARS`（iCloud 分组中的日历名称，英文逗号分隔）。**Google**：`GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REFRESH_TOKEN`、`MYSLOT_GOOGLE_CALENDARS`（Google 日历名称或 ID，英文逗号分隔；读取标题标记需要日历只读权限）。**飞书**：`FEISHU_CALDAV_URL`（飞书提供的 HTTPS CalDAV 地址）、`FEISHU_CALDAV_USER`、`FEISHU_CALDAV_PASSWORD`、`MYSLOT_FEISHU_CALENDARS`（飞书账户中要读的日历名称）。飞书的 CalDAV 配置需要你自己的飞书账户允许；[飞书帮助中心](https://www.feishu.cn/hc/zh-CN/articles/665183342590-%E5%90%8C%E6%AD%A5%E7%AC%AC%E4%B8%89%E6%96%B9%E6%97%A5%E5%8E%86%E4%B8%8E%E9%A3%9E%E4%B9%A6%E6%97%A5%E5%8E%86)说明如何开启。不要把睡眠追踪、节假日订阅等日历加入监控。**不要勾选还没设置凭据的来源**，否则系统会全部关闭。
4. 在 **Actions → Build and deploy availability → Run workflow** 手动运行，**不要勾选虚构测试数据**。留意 `Compute real public state` 日志。若出现 `Calendar sync failed (代码)`，页面会全部关闭；日志不含账户密码、日历名或事件。成功日志是 `Published day-level states only.`，但它仍**不能证明选到了你想要的生活日历**；请按下一节做真实测试。
5. 真正验证：依次在每个已启用来源中选中的日历，新建未来普通工作日 **18:30–19:00** 的普通测试事件；每加一条都手动运行一次，看那一天是否变「不可用」。验证后删除测试事件，再运行一次。如果仍然可约，核对事件所在的账户分组、日历名称和授权范围。iPhone 把三个账户显示在同一个 App，不代表事件自动复制到了 iCloud。

若某个已启用来源在未来 90 天一个事件都没有返回，系统按「不可用」处理并给出 `<来源>_no_events_returned`。这避免错误地把一个空日历当成完整的生活行程。真实日历确实完全没有事件时，也会保守关闭；你可以暂时不启用这个来源。

## 日历里怎么标记

程序只看**已启用来源中选中的日历**，关键字写在标题任何位置都可以：

| 标题里含有 | 类型 | 效果 |
| --- | --- | --- |
| `聚会`、`拍照`、`cos`、`【休息】`；全天事件含 `休息` | 不可约 | 整天关闭 |
| 开头是 `已约` 或 `【已约】`（如 `已约-星星`） | 已约好 | 当天其他时间关闭，并计入每周上限 |
| `漫展-xx`（【】可加可不加）、`漫展`、`回老家` | 半公开 | 这段时间可以约吃饭；谁能看到名字见下表 |
| 全天 `预留-xx` | 专属 | 只有预留代号为 xx 的人看到「为你预留」，其他人看到不可用 |
| 其他定时事件 | 可约 | 扣掉事件时间；休息日前后再各留 1 小时 |
| 其他全天事件 | 可约 | 不影响 |

同一天有多种时，按「不可约 > 已约 > 预留 > 半公开 > 可约」。一周（周一到周日）的 `已约` 达到上限（默认 3 次）后，这周剩下的日子对所有人显示「本周已满」。

## 分享码：整页上锁

没有分享码的人打开网页，只能看到「私人日程 · 请输入分享码」。日历内容在 GitHub Actions 里就用每个分享码分别加密，网页和仓库里只有乱码，按 F12 也看不到内容。

用 `settings.html` 底部的「生成分享码」生成，把得到的那一行加进仓库 **Settings → Secrets and variables → Actions → Secrets** 的 `MYSLOT_SHARE_CODES`：

```json
[
  {"code": "星星-936265", "level": "close", "reserve": "星星", "name": "江江"},
  {"code": "阿拍-417302", "level": "friend", "home": true},
  {"code": "小姨-280411", "level": "elder"}
]
```

| 档位 `level` | 漫展那天 | 回老家那天 |
| --- | --- | --- |
| `friend` 朋友（默认，含同辈） | 有安排 | 不可用 |
| `elder` 长辈 | 有安排 | 回老家 |
| `con` 漫展圈 | 在 xx | 不可用 |
| `close` 密友 | 在 xx | 回老家 |

- `home: true`：给某个朋友额外开放「回老家」。
- `see: ["ijoy"]`：给某个人额外开放某个漫展名；写了它，这个码会在那个漫展结束后自动失效。
- `reserve`：日历里 `预留-xx` 的 xx，任何档位都可以设。
- `name`：解锁后显示的称呼；不写就显示 `config.json` 的 `owner`。
- `until`：失效日期，可不写。

页面上不会出现「你没有权限」之类的字样，每个人看到的样式都一样。**分享码请用生成器，不要用 CN 或生日**，太好猜；分享码用微信单独发，不要把它和网址放在一起。删掉一行，下次同步后那个码就失效了。

排班和时段也可以不放在公开的 `config.json`：把 `config.json` 的全部内容复制进 Secret `MYSLOT_CONFIG_JSON`，程序会优先用它。

## 可约规则与设置

双击打开包里的 `settings.html`，按表单设置排班、时段和预约提前时间。支持固定星期及上几天休几天的周期；轮班人可取消「法定假日覆盖排班」。设置页下载 `config.json` 后，将它提交到 GitHub 仓库根目录，重新运行 Actions。设置页是静态网页，不能直接写回 GitHub；访客也不能替你保存全站设置。

当前默认：上班日 18:30–19:00 聚餐提前 4 小时；休息日 **12:00–16:00** 聚餐提前 24 小时，想早点出门可在设置页改为 **11:00**；漫展现场碰面提前 24 小时；cos 计划提前 30 天。朋友选 cos 计划时不选具体时段，由你们商量。上班日有上班活动仍能约下班后，若私人活动与 18:30–19:00 冲突则关闭。普通定时活动会从可约时段中扣掉（例如休息日 13:15–16:15 有事，只剩 12:00–13:00 和 16:30 以后）；休息日若一项普通活动实际占用当天**超过 3 小时**，则关闭普通预约。**全天**普通事件（如回老家、出差）不关闭当天，只提示朋友见面地点另议；要整天关闭请用 `【休息】` 标记；自动睡眠日历请不要加入监控列表。预约只是一条意向消息，最终还需要你确认。

`MYSLOT_PRIVATE_JSON` 可选，用于手动锁定与临时修改某日的上班/休息分类。例如：

```json
{"lockedDates":["2026-10-12"],"dateTypes":{"2026-10-10":"restday"}}
```

把它作为 GitHub **Secret** 填写，不要放进公开仓库。日常锁定可直接在日历创建 `【休息】`，无需修改这个 JSON。2026 年法定假日及补班在 `holidays-cn.json`；未录入的新年份按照每周或轮班规则显示。

## 运行与隐私

```sh
python3 -m unittest discover -s tests
python3 build.py --fixture --output dist
python3 -m http.server 8000 --directory dist
```

虚构测试模式会生成示例漫展，仅供本地或首次体验，公开站点不要将测试数据当作真实行程。正式构建不把密码、事件、私密日期列表提交到仓库；Pages 部署的只是 `dist/index.html` 和 `dist/availability.json`。仓库和 Actions 日志在公开仓库中可见，所以故障信息只显示固定错误代码。未开启变量 `MYSLOT_LIVE=true` 时，每小时的定时同步会跳过；确认真实联动后在 **Settings → Secrets and variables → Actions → Variables** 添加该变量，才会定时更新。未再次同步之前，新加的日历事件不会立刻反映在网页上。

Google 可读选中的多个日历及标题标记，但必须先完成 Google 日历只读 OAuth 授权；飞书可用它自己的 CalDAV 账号。两者都不会因你已登录 iPhone 日历而自动得到授权。程序基于 [PolyForm Noncommercial 1.0.0](LICENSE.md) 授权非商业使用和修改。
