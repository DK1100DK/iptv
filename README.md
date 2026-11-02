# IPTV 直播源自动更新工具

自动采集公开直播源 → 逐个测活 → 生成 TVBox 可用的订阅链接。
定时任务跑在 **GitHub Actions** 上，**每 2 小时自动更新一次**，不需要你的电脑开机，也不需要服务器。

## 最终你要用到的链接

推送成功、Actions 跑完第一轮后，下面三个链接即可使用（把 `<用户名>` 换成你的 GitHub 用户名，本项目默认为 `DK1100DK`）：

| 用途 | 链接 |
| --- | --- |
| **贴进 TVBox「配置地址」（推荐）** | `https://raw.githubusercontent.com/DK1100DK/iptv/main/tvbox.json` |
| 纯 m3u 直播源（TiviMate / PotPlayer / Kodi / 部分 TVBox 衍生版） | `https://raw.githubusercontent.com/DK1100DK/iptv/main/docs/iptv.m3u` |
| 纯 txt 直播源（TVBox「直播源地址」直接填写） | `https://raw.githubusercontent.com/DK1100DK/iptv/main/docs/iptv.txt` |

如果 `raw.githubusercontent.com` 在你的网络里打不开，改用下面两个等价镜像（内容完全相同）：

- GitHub Pages 镜像：`https://dk1100dk.github.io/iptv/tvbox.json`（需先在仓库 Settings → Pages 里启用，来源选 `main 分支 / docs 文件夹`）
- jsDelivr CDN 镜像：`https://cdn.jsdelivr.net/gh/DK1100DK/iptv@main/tvbox.json`

`tvbox.json` 里已经内置 3 条 m3u 线路（raw 直连、jsDelivr CDN、GitHub Pages），在 TVBox 直播界面按菜单可以手动切换线路，任一线路失效时换一条即可。上表的 txt 链接不参与 JSON 内置，需要时单独填入 TVBox 的「直播源地址」。

## 目录结构

```
IPTV/
├── .github/workflows/update.yml   # 每 2 小时自动测活并更新
├── config/sources.txt             # 上游直播源（可自己增删）
├── config/channels.txt            # 频道白名单（留空=全部输出）
├── config/config.json             # 参数：超时、并发、每组上限、分组排序
├── scripts/update.py              # 采集 + 测活 + 生成，纯 Python 标准库
├── tvbox.json                     # TVBox 配置入口（指向下面的输出）
├── data/cache.json                # 历史测活记录（由 Actions 自动维护）
└── docs/                          # 生成结果，同时是 GitHub Pages 站点根目录
    ├── iptv.m3u
    ├── iptv.txt
    ├── status.json                # 每轮存活频道数、耗时等统计
    └── .nojekyll
```

## 部署（3 步，约 5 分钟）

### 1. 建仓库
在 GitHub 上新建仓库，名字 `IPTV`，可见性 **Public**（必须是 Public，否则电视端拿不到文件）。

### 2. 把本项目的文件传上去
两种方式任选：

**A. 用附带脚本一次传完（本机没装 Git 也能用，只需 Python）**

1. 到 https://github.com/settings/tokens 生成一个 token：
   - 选 `Fine-grained tokens` → 仓库范围选 `IPTV` → Permissions 里给 **Contents: Read and write**
   - 或经典 token 勾选 `repo` 权限
2. 在 PowerShell 里执行（`xxxx` 换成你的 token，token 只在你本机使用，不会上传）：

```powershell
cd <本项目所在目录>
$env:GH_TOKEN = "xxxx"
python tools/init_repo.py
```

脚本会自动创建仓库、上传全部文件并触发第一次运行。

**B. 网页手动上传**
在仓库页面 Add file → Upload files，把 `config`、`scripts`、`docs`、`data`、`tvbox.json`、`README.md` 拖进去；
`.github/workflows/update.yml` 网页拖拽传不了隐藏目录，需要手动创建：
仓库 → Add file → Create new file → 文件名栏输入 `.github/workflows/update.yml`（输入第一个 `.` 后会自动出现 `.github` 提示）→ 粘贴内容 → Commit。

### 3. 开启 Actions 与 Pages
- 仓库 → **Settings** → **Actions** → General → Workflow permissions 选 **Read and write permissions**（否则无法回写文件）。
- 首次进 **Actions** 页面点 **I understand my workflows, go ahead and enable them**。
- 推送后会自动跑第一轮；之后每 2 小时（UTC 整点 0/2/4…）自动跑一次。也可以在 Actions 里选 “IPTV 自动更新” → **Run workflow** 手动立即更新。
- （可选）Settings → Pages → Source 选 `Deploy from a branch`，Branch 选 `main` + `/docs` → Save，获得 Pages 镜像链接。

## 自己的电视想只保留部分频道

编辑 `config/channels.txt`，一行一个频道名（支持 CCTV-1 / cctv1 / 央视一套 之类的写法，会自动做名称归一）。
**留空或整文件全是注释 = 输出所有测活的频道。**
改完 push，下一次定时任务（或手动 Run workflow）生效。

## 可调参数（`config/config.json`）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `timeout_seconds` | 6 | 单条链接测活超时 |
| `workers` | 32 | 并发测活线程数 |
| `max_per_channel` | 5 | 每个频道最多保留几条可用地址 |
| `min_valid_channels` | 40 | 统计口径：本轮真正可用的频道数低于此值时，日志会告警（不再清空列表） |
| `cache_hours` | 48 | 历史可用记录有效期（小时），用于本轮全挂时兜底 |
| `keep_untested` | true | 组播/rtmp 等无法在云端测活的地址是否保留 |
| `strict_filter` | false | **false=测活结果只用于排序**（失败的地址仍保留、排在末尾）；true=只输出本轮测活的地址 |
| `max_channels` | 0 | 输出频道数上限，0=不限制 |
| `group_priority` | 央视/卫视/地方/体育… | 分组在列表里的先后顺序 |

### 为什么默认不严格过滤

公开直播源里大量是**运营商组播/内网/地域限制**地址：GitHub Actions 的出口 IP 在海外机房，用它的视角去判定「这个台能不能播」会误杀很多在你家电视上正常播放的频道。
所以默认策略是：**测活结果用来排序（把确实拉通的台排到最前面），不用来删除**，同时保留历史缓存兜底。
如果你确认自己在国内网络下这些源都能用，也可以把 `strict_filter` 改成 `true`，只留 100% 测活的地址（列表会短很多）。

## 说明与免责

- 本工具只做「链接聚合 + 连通性测试」，本身不存储任何视频内容。上游源来自 GitHub 开源项目（iptv-org、YanG-1989/m3u 等）与公开分享地址，版权归原作者所有，仅供个人学习使用。
- 公开直播源本身会失效，这是这个领域的常态；本工具的作用是**每 2 小时把不能用的自动挑出去**，而不是保证永远不失效。
- 云端测活是「服务器视角」，电视在家庭宽带的实际播放体验可能略有色差；如果某些频道云端通过、电视打不开，切一条线路或降低 `max_per_channel` 通常能改善。
