# nflsoj / hydrooj — OJ 命令行工具集

> 同步 OJ 的比赛与题目、管理做题进度、在终端里统计完成度，
> 并把比赛首页与题面归档成本地 Markdown。

本仓库提供**两个前端完全一致**的命令行工具，分别对接两套不同的 OJ：

| 工具 | 对接站点 | 实现基础 | 默认数据目录 |
|---|---|---|---|
| **`nflsoj`** | ports.cc / NFLSOJ（`https://nflsoi.cc:20035`） | 站点私有 JSON REST API（`/api/...`） | `~/.nflsoj` |
| **`hydrooj`** | 基于 **Hydro v4.x** 搭建的 OJ（默认 `http://nflsoi.cc:10611`） | Hydro 的 JSON 视图（`Accept: application/json`） | `~/.hydrooj` |

两者共用同一套终端渲染层、存储层与 Markdown 归档渲染器（`hydrooj` 直接复用
`nflsoj_archive.nflsoj` 里的实现），因此配色、表格、进度条、命令名、选项、提示语
和归档目录结构**完全相同**；差别只在「怎么跟服务器说话」和「ID 长什么样」。

`nflsoj` 的详细文档见下文；Hydro 版的差异与用法见 [Hydro 版：hydrooj](#hydro-版hydrooj)。

| 项目 | 值 |
|---|---|
| 当前版本 | `1.2.0` |
| 许可证 | [GNU General Public License v3.0](#许可) |
| 依赖 | 仅 Python 标准库，无需 `pip install` 任何包 |
| Python | `pyproject.toml` 声明 `requires-python = ">=3.14"` |
| 包入口 | `src/nflsoj_archive/` 与 `src/hydrooj_archive/` |
| 控制台命令 | `nflsoj`、`nflsoj-archive`、`hydrooj`、`hydrooj-archive`，或 `python -m nflsoj_archive` / `python -m hydrooj_archive` |
| 实现 | `src/nflsoj_archive/nflsoj.py`（共享前端 + NFLSOJ 后端）、`src/hydrooj_archive/hydrooj.py`（Hydro 后端） |
| 默认站点 | nflsoj：`https://nflsoi.cc:20035`；hydrooj：`http://nflsoi.cc:10611` |

---

## 目录

- [功能特性](#功能特性)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [登录](#登录)
- [全局选项](#全局选项)
- [命令总览](#命令总览)
- [命令参考](#命令参考)
  - [`login` — 登录](#login--登录)
  - [`logout` — 退出登录](#logout--退出登录)
  - [`whoami` — 当前登录状态](#whoami--当前登录状态)
  - [`config` — 本地配置](#config--本地配置)
  - [`sync` — 同步比赛与题目](#sync--同步比赛与题目)
  - [`contests` — 比赛列表](#contests--比赛列表)
  - [`problems` — 题目列表](#problems--题目列表)
  - [`mark` — 标记做题状态](#mark--标记做题状态)
  - [`stats` — 做题统计](#stats--做题统计)
  - [`open` — 浏览器打开](#open--浏览器打开)
  - [`export` — 导出做题记录](#export--导出做题记录)
  - [`archive` — Markdown 归档](#archive--markdown-归档)
  - [`videos` — 讲解视频](#videos--讲解视频)
  - [`origin` — 原题查询](#origin--原题查询)
- [Markdown 归档详解](#markdown-归档详解)
- [讲解视频](#讲解视频)
- [原题查询](#原题查询)
- [离线模式](#离线模式)
- [性能](#性能)
- [从 1.0 升级](#从-10-升级)
- [数据存储](#数据存储)
- [站点 API 说明](#站点-api-说明)
- [常见问题（FAQ）](#常见问题faq)
- [Hydro 版：hydrooj](#hydro-版hydrooj)
- [项目结构](#项目结构)
- [贡献](#贡献)
- [许可](#许可)

---

## 功能特性

- **多种登录方式**：浏览器 Cookie、Bearer Token、`session-swr`、用户名 + 密码。粘贴任意文本
  （裸 token、完整 Cookie 串、`session-swr` 的 JSON、`Authorization` 头）都会被自动识别。
- **并发 + 连接复用 + 增量同步**：每线程一条 keep-alive 长连接，默认 8 并发抓取比赛详情；
  已有的比赛直接跳过，连续两页都已同步就停止翻页。
- **做题状态管理**：`未完成 / 正在做 / 已完成` 三态；`mark` 省略状态时**默认标记为已完成**，
  一次可以标记多道题（`mark 2643 1 2 3`）。
- **进度统计**：总完成度进度条、三态计数、最近 7 天标记活跃度（sparkline）、
  按比赛维度统计、最近标记记录。
- **本地 Markdown 归档**：每场比赛生成「比赛首页 + 每题一份题面」，自动生成总目录 `index.md`，
  带 YAML front matter、相对链接与原始链接。
- **讲解视频**：从「比赛说明」里解析 `/videoshow/<uuid>` 链接，抓取标题、时长、分辨率、
  DASH 清单与 ClearKey 密钥；默认每场只取前 3 个，媒体文件按需下载并附解密助手脚本。
- **原题查询**：把题面送进 [CPRet](https://cpret.online/) 做相似度检索，最高相似度
  **严格大于阈值**（默认 `0.7`）时判为命中原题，并缓存成 VJudge 题号
  （`洛谷-P1000`、`CodeForces-1234A` 这种格式）。
- **离线模式**：`--offline` 下只读本地数据，绝不发起任何请求；`stats` / `export` 等命令
  本来就不联网，毫秒级返回。
- **终端渲染**：256 色状态徽标、按东亚字符宽度对齐的表格、单行实时进度条（含速率与剩余时间）；
  非 TTY 自动降级为百分比打点，`--ascii` 可退化为纯 ASCII 边框与符号；终端偏窄时
  （`contests` / `problems` < 96 列、`stats` < 104 列）自动精简表格列，避免内容被截断。
- **健壮性**：5xx / 429 自动重试（指数退避）、超时与证书错误的中文提示、原子写入本地 JSON、
  归档时内容未变则不重写文件。

> 以上特性除**讲解视频**外，对 **`hydrooj`**（Hydro 版）同样成立
> （Hydro 没有 `/api/video/getVideo`，所以 `hydrooj` 没有 `videos` 命令）；
> Hydro 特有的差异见 [Hydro 版：hydrooj](#hydro-版hydrooj)。

---

## 环境要求

- **Python 3.14 或更高版本**（`pyproject.toml` 中 `requires-python = ">=3.14"`，仓库内
  `.python-version` 也是 `3.14`）。用 `uv` / `pip` 安装时会强制检查这个下限。
- 支持 Windows / macOS / Linux。Windows 下会自动尝试开启控制台 ANSI 彩色支持与 UTF-8 代码页。
- 不需要任何第三方库：源码只 `import` 标准库模块。

> 源码本身没有引入 3.14 专有语法，直接用较低版本的 `python` 运行脚本通常也能工作，
> 但项目只在 3.14 上验证过，不作保证；`uv sync` 则严格要求 3.14+。

---

## 安装

### 方式一：uv（推荐）

```bash
git clone <本仓库地址> nflsoj-archive
cd nflsoj-archive

uv sync                       # 按 pyproject.toml 创建虚拟环境并安装本项目
uv run nflsoj --version       # 输出：nflsoj 1.1.0
uv run hydrooj --version      # 输出：hydrooj 1.2.0
uv run nflsoj --help
uv run hydrooj --help
```

`uv sync` 会注册四个控制台命令：

| 命令 | 说明 |
|---|---|
| `nflsoj` | NFLSOJ 版主命令 |
| `nflsoj-archive` | 同一入口的别名（便于与项目名对应） |
| `hydrooj` | Hydro 版主命令 |
| `hydrooj-archive` | Hydro 版的别名 |

也可以直接激活虚拟环境后使用：

```bash
uv run python -m nflsoj_archive --help   # 模块入口，等价于 nflsoj
uv run python -m hydrooj_archive --help  # 模块入口，等价于 hydrooj
```

### 方式二：直接用 python 运行（不安装）

两个脚本都自带可执行头，零依赖，克隆下来就能跑：

```bash
python src/nflsoj_archive/nflsoj.py --help
python src/hydrooj_archive/hydrooj.py --help

# Linux / macOS 还可以：
chmod +x src/nflsoj_archive/nflsoj.py src/hydrooj_archive/hydrooj.py
./src/hydrooj_archive/hydrooj.py --help
```

> 下文所有示例都用 `nflsoj` / `hydrooj` 代指 `uv run ...` 或 `python src/.../xxx.py`，
> 两者行为完全一致。

可选：为方便日常使用，可以建别名或软链接：

```bash
# Linux / macOS
alias nflsoj='python /path/to/nflsoj-archive/src/nflsoj_archive/nflsoj.py'
alias hydrooj='python /path/to/nflsoj-archive/src/hydrooj_archive/hydrooj.py'

# Windows PowerShell
Set-Alias nflsoj "python D:\path\to\nflsoj-archive\src\nflsoj_archive\nflsoj.py"
Set-Alias hydrooj "python D:\path\to\nflsoj-archive\src\hydrooj_archive\hydrooj.py"
```

---

## 快速开始

```bash
nflsoj login                 # 1. 登录（交互式，推荐粘贴浏览器 Cookie / session-swr）
nflsoj sync                  # 2. 增量同步比赛与题目（首次全量，之后很快）
nflsoj contests              # 3. 查看比赛列表与完成进度
nflsoj problems 2643         # 4. 查看 2643 号比赛的题目
nflsoj mark 2643 3           # 5. 把第 3 题标记为「已完成」（省略状态即 done）
nflsoj stats                 # 6. 查看做题统计（纯本地，秒出）
nflsoj archive 2643          # 7. 把该场比赛与全部题面归档成本地 Markdown
```

不带任何子命令直接运行（`nflsoj`）会显示**概览面板**：登录账号、站点、数据目录、
总完成度进度条、比赛状态分布、最近 7 天标记活跃度，以及常用命令表和建议下一步。
概览面板默认完全本地、不联网；加 `--online` 才会联网校验登录状态。

退出码：`0` 成功、`1` 出错（含「找不到比赛」「未登录」）、`130` 用户 Ctrl+C 中断。

---

## 登录

支持三种方式，登录成功后凭据写入 `config.json`（见 [数据存储](#数据存储)）。

**1) 交互式（推荐）**

```bash
nflsoj login
```

会出现两个选项：

1. **粘贴浏览器 Cookie / Token / `session-swr`**（默认，最快）——粘贴任意文本即可，
   工具会尝试从中提取 token；提取不到时把整段内容当作 Cookie 保存。
2. **用户名 + 密码**——密码输入时不回显（`getpass`），走 `/api/auth/login` 换取 token。

> 获取凭据的方法：网页登录后按 `F12` → `Application` → `Local Storage` → 复制
> `session-swr` 的整段值；或从 `Network` 面板复制请求头里的 `Cookie` / `Authorization`。

**2) 命令行参数**

```bash
nflsoj login --token "eyJhbGciOi..."
nflsoj login --cookie "session-swr=..."
nflsoj login -u 用户名 -p 密码
```

**3) 环境变量**

```bash
# 等价于 --token（仅在 login 命令中读取）
export NFLSOJ_TOKEN="eyJhbGciOi..."       # Windows: $env:NFLSOJ_TOKEN = "..."
nflsoj login
```

无论用哪种方式，登录成功前都会调用 `/api/auth/getSessionInfo` **校验一次**，
校验通过才写盘；用户名以服务端返回的为准。登录后：

```bash
nflsoj whoami     # 显示当前登录用户、站点信息、服务端版本、本地进度
nflsoj logout     # 清除本地登录凭据（做题标记与归档文件都保留）
```

已登录状态下重复执行 `login` 可用来切换账号（会提示当前账号）。

---

## 全局选项

以下选项对所有子命令都有效，既可写在子命令之前（`nflsoj --api URL sync`），
也可写在子命令之后（`nflsoj sync --api URL`）。

| 选项 | 说明 |
|---|---|
| `--data-dir PATH` | 数据目录（默认 `~/.nflsoj`，可被环境变量 `NFLSOJ_DATA_DIR` 覆盖） |
| `--api URL` | 站点 API 地址（默认 `https://nflsoi.cc:20035`） |
| `--timeout N` | HTTP 超时秒数（默认 `30`） |
| `--insecure` | 跳过 HTTPS 证书校验（代理 / 自签名证书环境用） |
| `--debug` | 打印 HTTP 请求 / 响应摘要到 stderr |
| `--offline` | 离线模式：只读本地数据，任何需要联网的操作都直接报错 |
| `--online` | 强制联网校验登录状态（用于无子命令时的概览面板） |
| `--no-color` | 禁用彩色输出 |
| `--ascii` | 使用纯 ASCII 边框与符号（兼容老终端 / 不支持 Unicode 的编码） |
| `--no-progress` | 不显示实时进度条 |
| `--width N` | 输出宽度（默认跟随终端，范围 40–200） |
| `--version` | 显示版本号并退出（只能写在最顶层：`nflsoj --version`） |
| `-h, --help` | 显示帮助（每个子命令都有自己的 `--help`） |

环境变量：

| 变量 | 作用 |
|---|---|
| `NFLSOJ_TOKEN` | `login` 时的默认 token（等价于 `--token`） |
| `NFLSOJ_DATA_DIR` | 默认数据目录（优先级低于 `--data-dir`，高于 `~/.nflsoj`） |
| `NO_COLOR` | 任意非空值即禁用彩色（遵循 [no-color.org](https://no-color.org) 约定） |
| `NFLSOJ_WIDTH` / `COLUMNS` | 覆盖输出宽度（优先级低于 `--width`） |

补充说明：

- 输出重定向到文件 / 管道（非 TTY）时，彩色与进度条会自动关闭，避免污染日志。
- 终端的 `stdout` 编码无法表示 `─│✔○◐█░…` 等字符时，会自动退化为 ASCII 模式。
- `--api` 只覆盖**本次运行**使用的地址；只有 `login` 和 `config --set-api` 会把它写进配置文件。
- 需要写盘的命令（`login` / `logout` / `config` / `sync` / `mark` / `archive`）启动时会先试写数据目录；
  不可写时立刻给出可操作提示并退出（不会先联网再在最后一步崩掉）。

---

## 命令总览

| 命令 | 别名 | 联网 | 作用 |
|---|---|---|---|
| `login` | | 是 | 登录（Cookie / Token / 用户名密码） |
| `logout` | | 否 | 清除本地登录凭据 |
| `whoami` | | 是 | 显示当前登录状态 |
| `config` | | 否 | 查看 / 修改本地配置 |
| `sync` | | 是 | 同步比赛列表与题目（增量） |
| `contests` | `ls` | 视情况 | 列出比赛（含完成进度） |
| `problems` | `ps` | 视情况 | 列出某场比赛的题目 |
| `mark` | `m` | 视情况 | 标记题目状态（默认 `done`） |
| `stats` | `st` | 否 | 做题情况统计 |
| `open` | `o` | 否 | 在浏览器中打开比赛 / 题目 |
| `export` | | 否 | 导出做题记录（md / csv / json） |
| `archive` | `arc` | 是 | 把比赛首页与题面归档成本地 Markdown（顺带抓讲解视频元信息） |
| `videos` | `vid` | 是 | 查看 / 保存比赛说明里的讲解视频 |
| `origin` | `orig` / `src` | 是 | 查询原题（CPRet），按 VJudge 题号缓存 |

「视情况」表示：优先使用本地缓存，只有缓存缺失或显式要求刷新时才联网。

---

## 命令参考

### `login` — 登录

```bash
nflsoj login                              # 交互式（选择粘贴 Cookie 或输入账号密码）
nflsoj login --token "eyJhbGciOi..."      # 直接给 token
nflsoj login --cookie "session-swr=..."    # 直接给 Cookie 串
nflsoj login -u 用户名 -p 密码             # 用户名密码登录
nflsoj login --api https://nflsoi.cc:20035 # 顺便把站点地址写入配置
```

| 选项 | 说明 |
|---|---|
| `--token TEXT` | 直接提供 token（也支持粘贴整段 Cookie / JSON，工具会尝试提取） |
| `--cookie TEXT` | 直接提供浏览器 Cookie 串 |
| `-u, --username TEXT` | 用户名 |
| `-p, --password TEXT` | 密码（与 `-u` 同时给出时才走账号密码登录） |

说明：

- 同时给出 `--token` / `--cookie` 时优先用它们，不会再去请求 `/api/auth/login`。
- `-u` 与 `-p` 请成对给出；只给 `-u` 时会拿空密码去请求 `/api/auth/login`，通常会被站点拒绝。
- 环境变量 `NFLSOJ_TOKEN` 可作为 `--token` 的备用来源。
- 登录成功会打印站点地址、数据目录，以及三条「接下来可以」的建议命令。

### `logout` — 退出登录

```bash
nflsoj logout
```

清除 `config.json` 中的 `token` / `cookie` / `username`。**不会**删除做题标记
（`progress.json`）与已经归档的 Markdown 文件。

| 选项 | 说明 |
|---|---|
| （无） | 除[全局选项](#全局选项)外不接受任何参数 |

### `whoami` — 当前登录状态

```bash
nflsoj whoami
```

联网调用 `/api/auth/getSessionInfo`，输出：

| 字段 | 说明 |
|---|---|
| 用户名 / 用户 ID | 服务端识别的当前账号 |
| 管理员 | `isManager` |
| 已加入小组 | `userMeta.joinedGroupsCount`（存在时） |
| 站点 | `serverPreference.siteName` |
| 服务端版本 | `serverVersion.hash` (+ 日期) |
| 数据目录 / 归档目录 | 当前生效的路径 |
| 本地进度 | `N 场 / M 题 · 已完成 K` |

| 选项 | 说明 |
|---|---|
| （无） | 除[全局选项](#全局选项)外不接受任何参数（`--debug` / `--timeout` 在这里最常用） |

未登录时提示「未登录（匿名访问）」并返回 0。

### `config` — 本地配置

```bash
nflsoj config                                     # 查看当前配置
nflsoj config --set-api https://nflsoi.cc:20035   # 修改站点 API 地址（写入配置）
nflsoj config --set-archive-dir D:\notes\nflsoj   # 修改默认归档目录
nflsoj config --clear                             # 清除登录凭据
```

| 选项 | 说明 |
|---|---|
| `--set-api URL` | 设置站点 API 地址（会去掉末尾 `/` 后写入 `baseUrl`） |
| `--set-archive-dir DIR` | 设置默认归档目录（自动展开 `~` 并转为绝对路径） |
| `--clear` | 清除登录凭据（等价于 `logout` 的清理动作） |

查看视图会显示：站点地址、配置文件路径、记录文件路径、归档目录、Token（摘要）、
Cookie（是否已保存）、记录账号。

### `sync` — 同步比赛与题目

```bash
nflsoj sync                         # 增量同步（推荐，日常就用它）
nflsoj sync --full                  # 关闭增量，完整遍历所有分页
nflsoj sync --keyword 周赛           # 只同步名称含「周赛」的比赛
nflsoj sync --contest 2643 2645     # 只同步指定 ID（直接按 ID 拉详情，不翻页）
nflsoj sync --workers 8             # 并发线程数（默认 8）
nflsoj sync --limit 50              # 最多处理 50 场
nflsoj sync --no-problems           # 只刷新比赛列表，不拉题目
nflsoj sync --force                 # 忽略本地缓存，强制重新拉题目
```

| 选项 | 说明 |
|---|---|
| `--keyword TEXT` | 只同步名称包含该关键词的比赛 |
| `--contest ID [ID ...]` | 只同步指定 ID 的比赛 |
| `--workers N` | 并发线程数（默认 `8`，建议 4–12；内部上限 16） |
| `--limit N` | 最多处理多少场比赛 |
| `--no-problems` | 只同步比赛列表，不拉取题目 |
| `--force` | 强制重新拉取已有比赛的题目 |
| `--full` | 关闭增量模式（完整遍历所有分页） |

行为说明：

- 默认**增量**：本地已有 `syncedAt` 的比赛只更新基本信息、跳过详情请求；
  **连续两页全部已同步**就认为已经追上进度，停止继续翻页。
- 增量只在「没有 `--force`、没有 `--contest`、没有 `--keyword`」时生效，
  其余情况自动按全量/定向同步处理，并在标题下打印「同步模式」。
- 列表分页与详情抓取是**流水线**并行的：列表还没拉完，详情请求已经在跑。
- `--no-problems` 配合 `--contest` 时直接逐个 ID 取元信息，省掉所有列表分页请求。
- 0 题的比赛（讲座 / 未开放题单）是合法的，`syncedAt` 照样记录，不会导致增量永远停不下来。
- 同步结束会打印「N 场比赛 / M 道题目，用时 X」，列出失败的比赛（最多 10 行），
  并提示可用 `nflsoj sync --contest <ID>` 单独重试。
- 中途 Ctrl+C 会先把已拿到的进度写盘再退出。

### `contests` — 比赛列表

```bash
nflsoj contests                     # 读本地缓存
nflsoj contests --live              # 直接请求服务器并写回缓存
nflsoj contests --keyword 寒假      # 按名称过滤
nflsoj contests --phase running     # 只看进行中：not_started / running / ended
nflsoj contests --ids 2643 2645     # 只显示指定 ID
nflsoj contests --limit 100         # 最多显示多少场（默认 50）
```

| 选项 | 说明 |
|---|---|
| `--live` | 直接请求服务器（并写回缓存），数据来源显示为「实时请求」 |
| `--keyword TEXT` | 按名称过滤（不区分大小写的子串；也匹配完整 ID） |
| `--phase {...}` | 按状态过滤：`not_started` / `running` / `ended` |
| `--ids ID [ID ...]` | 只显示指定 ID |
| `--limit N` | 最多显示多少场（默认 `50`） |

表格列：**状态 / ID / 比赛名称 / 模式 / 开始 / 结束 / 已完成**。
终端宽度小于 96 列时自动精简为 **状态 / ID / 比赛名称 / 结束 / 已完成** 五列。
底部汇总「未开始 / 进行中 / 已结束」计数与总完成度进度条。
本地完全没有数据时会自动同步一次（并提示）；超过 `--limit` 会提示当前总数。

### `problems` — 题目列表

```bash
nflsoj problems 2643                # 2643 号比赛（也支持名称关键词）
nflsoj problems "线段树"            # 按名称（不区分大小写子串）匹配
nflsoj problems 2643 --refresh      # 强制从服务器刷新题目列表
nflsoj problems 2643 --state todo   # 只看未完成（可写多个：--state todo doing）
nflsoj problems 2643 --tags         # 显示题目标签
nflsoj problems 2643 --json         # 以 JSON 输出
nflsoj problems 2643 --open         # 打开该比赛的网页
```

| 参数 / 选项 | 说明 |
|---|---|
| `contest`（位置参数） | 比赛 ID 或名称关键词 |
| `--refresh` | 强制从服务器刷新题目列表（失败时降级用缓存并警告） |
| `--state STATE [STATE ...]` | 按状态过滤，接受 `todo` / `doing` / `done` 及其别名 |
| `--tags` | 额外显示一列标签（`#标签` 形式） |
| `--json` | 以 JSON 输出 `{"contest": ..., "problems": [...]}` |
| `--open` | 在浏览器中打开该比赛 |

表格列：**状态 / 题号 / 题目名称 / 题目 ID / 备注 / 更新**（加 `--tags` 时在「题目名称」后
插入「标签」列；终端宽度小于 96 列时省略「备注」「更新」）。「备注」显示服务端的最好提交状态（如 `Accepted`）或 `已提交`；
「更新」是本地标记的相对时间（`刚刚` / `12 分钟前` / `昨天` …）。
底部显示本场进度条、三态图例，以及 `mark` / `archive` / 网页地址的提示。

### `mark` — 标记做题状态

```bash
nflsoj mark 2643 3                  # 把第 3 题标记为「已完成」（省略状态 = done）
nflsoj mark 2643 1 2 3              # 一次标记三题（默认都是 done）
nflsoj mark 2643 3 doing            # 末尾写状态：标记为「正在做」
nflsoj mark 2643 3 todo             # 标记为「未完成」
nflsoj mark 2643 3 --state done     # 用选项指定状态（等价于结尾写 done）
nflsoj mark 2643 3 --clear          # 清除标记
nflsoj mark 2643 3 --next           # 在 未完成 → 正在做 → 已完成 间循环切换
nflsoj mark 2643 3 --refresh        # 先刷新题目列表再标记
```

| 参数 / 选项 | 说明 |
|---|---|
| `contest`（位置参数） | 比赛 ID 或名称关键词 |
| `题号 ...`（位置参数，可多个） | 题号（`displayOrder`）或题目 ID |
| `--state STATE` | `todo` / `doing` / `done`，等价于在结尾写状态 |
| `--clear` | 清除标记（回到「未标记」，统计时按未完成计） |
| `--next` | 在三种状态间循环（覆盖「默认 done」与显式状态） |
| `--refresh` | 先刷新题目列表再标记 |

规则与细节：

- **省略状态时默认标记为「已完成」**（`DEFAULT_MARK_STATE = done`），这是 1.1 的行为；
  想要 1.0 那种「循环切换」请显式加 `--next`。
- 结尾的状态词只有在**不是纯数字**且属于别名表时才会被识别为状态，因此
  `mark 2643 1 2` 表示「第 1、2 题」而不是「第 1 题标记为 2」。状态一律用
  `todo` / `doing` / `done` / `已完成` 等别名表达。
- 题号优先按 `displayOrder` 匹配，再按题目 ID 匹配。
- 输出一张「题号 / 题目 / 原状态 / 新状态」对照表，并刷新本场进度条；
  找不到的题号会集中报错并给出 `nflsoj problems <ID>` 的提示。

**状态别名**（大小写不敏感）：

| 状态 | 可用别名 |
|---|---|
| 未完成 `todo` | `todo` `0` `未完成` `未做` `没做` `new` `undone` |
| 正在做 `doing` | `doing` `1` `正在做` `在做` `进行中` `inprogress` `wip` `ing` |
| 已完成 `done` | `done` `2` `已完成` `完成` `已做` `ac` `ok` `过了` |
| 清除（无状态） | `none` `clear` `reset` `取消` `rm` |

### `stats` — 做题统计

```bash
nflsoj stats                        # 整体统计（纯本地，秒出）
nflsoj stats --contest 2643 2645    # 只统计指定比赛（支持多个）
nflsoj stats --keyword 周赛          # 按比赛名称过滤
nflsoj stats --phase ended          # 按状态过滤
nflsoj stats --limit 30             # 表格最多显示多少行（默认 20）
nflsoj stats --recent 15            # 最近标记显示条数（默认 8）
nflsoj stats --no-recent            # 不显示最近标记
nflsoj stats --json                 # 以 JSON 输出
```

| 选项 | 说明 |
|---|---|
| `--contest SPEC [SPEC ...]` | 只统计指定比赛（ID 或名称；无法匹配的会警告并忽略） |
| `--keyword TEXT` | 按比赛名称过滤 |
| `--phase {...}` | 按状态过滤：`not_started` / `running` / `ended` |
| `--limit N` | 表格最多显示行数（默认 `20`，按题目数降序取前 N 场） |
| `--recent N` | 最近标记条数（默认 `8`） |
| `--no-recent` | 不显示最近标记 |
| `--json` | 以 JSON 输出 `{overall, per_contest, recent}` |

输出包含：比赛数量（其中已同步题目的场次）、题目总数、总完成度进度条与百分比、
`已完成 / 正在做 / 未完成` 分项计数与占比、最近 7 天标记活跃度 sparkline、
服务端记录的「已提交过多少题」（仅供参考），以及按比赛统计表与最近标记表。
终端宽度小于 104 列时，按比赛统计表会省略「正在做 / 未完成」两列，进度列改为百分比。

### `open` — 浏览器打开

```bash
nflsoj open 2643                    # 打开 2643 号比赛页面
nflsoj open 2643 3                  # 打开 2643 号比赛第 3 题
nflsoj open 2643 --print-only       # 只打印链接，不打开浏览器
```

| 参数 / 选项 | 说明 |
|---|---|
| `contest`（位置参数） | 比赛 ID 或名称关键词 |
| `problem`（可选位置参数） | 题号（`displayOrder`）或题目 ID |
| `--print-only` | 只打印链接，不调用浏览器 |

链接格式：`<base>/c/<比赛ID>`，带题号时为 `<base>/c/<比赛ID>/p/<题号>`。
该命令完全本地解析，不联网（浏览器自己会去访问站点）。

### `export` — 导出做题记录

```bash
nflsoj export                                   # Markdown（默认，打印到屏幕）
nflsoj export -o 做题记录.md                     # 写入文件
nflsoj export --format csv -o out.csv
nflsoj export --format json -o out.json
nflsoj export --contest 2643 2645 -o partial.md
nflsoj export --keyword 周赛 --phase ended -o 周赛.md
```

| 选项 | 说明 |
|---|---|
| `--format {md,csv,json}` | 导出格式（默认 `md`） |
| `-o, --output FILE` | 输出文件（默认打印到屏幕；`-` 也表示屏幕） |
| `--contest SPEC [SPEC ...]` | 只导出指定比赛（ID 或名称） |
| `--keyword TEXT` | 按比赛名称过滤 |
| `--phase {...}` | 按状态过滤：`not_started` / `running` / `ended` |

三种格式：

- **Markdown**：每场比赛一个二级标题（含 `已完成/总数 · 状态`），下面是
  `题号 / 题目 / 状态 / 标签 / 题目 ID` 表格。样式示例：

  ```markdown
  # NFLSOJ 做题记录 · <用户名>

  导出时间：2026-11-14 21:49:38
  统计口径：本地手动标记（未完成 / 正在做 / 已完成）

  ## S+线段树 (#2643) — 2/8 · 已结束

  | 题号 | 题目 | 状态 | 标签 | 题目 ID |
  |---:|---|---|---|---:|
  | 1 | 第K值 | 已完成 | 线段树 | 31766 |
  | 3 | 最长连续严格递增段LCIS | 正在做 | 线段树 | 1990 |
  ```

- **CSV**：表头固定为
  `contest_id,contest_name,display_order,problem_id,problem_title,tags,state,state_label,updated_at`，
  一题一行，`tags` 用空格连接。
- **JSON**：`{"user", "exportedAt", "contests": [{id, name, mode, startTime, endTime, problems}]}`。

导出完全基于本地数据，不联网；写文件前会自动创建父目录。

### `archive` — Markdown 归档

```bash
nflsoj archive 2643                     # 归档单场（比赛首页 + 全部题面）
nflsoj archive 2643 2645                # 归档多场
nflsoj archive --all                    # 归档本地全部已同步的比赛
nflsoj archive --all --keyword 线段树    # 按名称过滤
nflsoj archive --all --phase ended      # 只归档已结束的
nflsoj archive --all --limit 10         # 最多归档 10 场
nflsoj archive 2643 --dir D:\notes\oj   # 指定归档目录
nflsoj archive 2643 --no-statements     # 只存比赛首页，不抓题面
nflsoj archive 2643 --force             # 忽略题面缓存，强制重新抓取
nflsoj archive --all --workers 4        # 并发抓题面的线程数（默认 8）
nflsoj archive 2643 --open              # 归档完成后打开总目录
nflsoj archive 2643 --json              # 以 JSON 输出结果（stdout 只有 JSON）
nflsoj archive 2643 --no-videos         # 不抓讲解视频
nflsoj archive 2643 --video-limit 2     # 每场最多抓 2 个视频（默认 3）
nflsoj archive 2643 --video-download    # 连媒体文件一起下载（几十 MB，默认关闭）
```

| 参数 / 选项 | 说明 |
|---|---|
| `SPEC ...`（位置参数，可多个） | 比赛 ID 或名称关键词 |
| `--all` | 归档本地全部已同步的比赛 |
| `--keyword TEXT` | 按比赛名称过滤（可单独使用，等价于「归档全部匹配项」） |
| `--phase {...}` | 按状态过滤：`not_started` / `running` / `ended` |
| `--limit N` | 最多归档多少场比赛 |
| `--dir DIR` | 归档目录（默认 `<数据目录>/archive`，或配置里的 `archiveDir`） |
| `--no-statements` | 只存比赛首页，不抓题目正文（已存在的题面文件原样保留，不会被覆盖） |
| `--force` | 忽略本地题面缓存，强制重新抓取 |
| `--workers N` | 并发抓取题面的线程数（默认 `8`，内部上限 12） |
| `--open` | 归档完成后打开总目录 `index.md` |
| `--json` | 以 JSON 输出归档结果（自动关闭进度条，保证 stdout 干净） |
| `--no-videos` | 不抓比赛说明里的讲解视频 |
| `--video-limit N` | 每场最多抓几个讲解视频（默认 `3`，`0` 等同 `--no-videos`） |
| `--video-download` | 连音视频原始文件一起下载（CENC 加密、单个数十 MB，默认关闭） |
| `--video-max-mb N` | 单个媒体文件的下载上限（默认 `100` MB，超过则只存元信息） |
| `--origin` | 归档时顺带查询原题（结果写进题面与 `progress.json`） |
| `--threshold F` | 配合 `--origin` 的相似度阈值（默认 `0.70`） |

不带位置参数时必须给出 `--all`、`--keyword`、`--phase`、`--limit` 之一，否则报错并提示用法。
运行时会打印每场比赛的「ID / 比赛 / 题目 / 题面 / 失败 / 归档目录」结果表，
以及「新增/更新 N 个文件，M 个文件内容未变（已跳过写入）」。
生成结构与细节见下一节。

---

### `videos` — 讲解视频

站点把每次讲解的视频挂在**「比赛说明」**里，形如 `[20261121讲解](/videoshow/<uuid>)`。
`videos` 解析这些链接并查询视频元信息；为了避免一次拉全站，**默认只看前 3 个**。

```bash
nflsoj videos 2647                      # 列出这场比赛说明里的全部视频
nflsoj videos 2647 --save               # 存元信息 + DASH 清单到归档目录
nflsoj videos 2647 --download           # 保存并下载媒体（隐含 --save）
nflsoj videos 2647 --download --max-mb 40   # 只下载小于 40MB 的文件
nflsoj videos --all                     # 查看本地全部比赛的视频
nflsoj videos --all --save              # 全部保存元信息（一次性）
nflsoj videos --all --download          # 一次性下载全部视频
nflsoj videos --all --download --max-mb 200 --keyword 数位   # 只处理匹配的比赛
nflsoj videos 2647 --json               # 以 JSON 输出（stdout 只有 JSON）
```

| 参数 / 选项 | 说明 |
|---|---|
| `SPEC`（位置参数） | 比赛 ID（或唯一前缀 / 名称关键词）；用 `--all` 时省略 |
| `--all` | 一次性处理本地**全部已同步比赛**的视频（配合 `--download` 即"下载全部"） |
| `--keyword TEXT` | 配合 `--all`：只处理名称含该关键词的比赛 |
| `--phase {...}` | 配合 `--all`：只处理该状态的比赛 |
| `--limit N` | 每场最多处理几个视频（默认 `0` = 全部） |
| `--save` | 把元信息（JSON）与 DASH 清单（`.mpd`）保存到 `<归档目录>/<比赛>/videos/` |
| `--download` | 保存并下载音视频原始文件（隐含 `--save`） |
| `--max-mb N` | 单个媒体文件下载上限（默认 `100` MB，超过跳过并记录原因） |
| `--dir DIR` | 归档目录（默认 `<数据目录>/archive`） |
| `--refresh` / `--force` | 强制重新抓取（默认内容未变时不重写） |
| `--json` | 以 JSON 输出视频元信息 |

批量模式最后会汇总「处理了多少场 / 多少个视频 / 实际下载了多少个（总大小）」，
下载被上限挡掉时会给出「N 个文件超过 --max-mb=X」的提示。

输出示例：

```text
┌─ 讲解视频 ─────────────────────────────────────────────── S+数位DP · #2647 ┐
  描述里的视频 1 个
  本次抓取     前 3 个（--limit 可调）

┌───┬──────────────────┬─────────┬────────────────────┬───────┬──────┬──────┐
│ # │ 视频             │    时长 │ 分辨率             │ 状态  │ 加密 │ 备注 │
├───┼──────────────────┼─────────┼────────────────────┼───────┼──────┼──────┤
│ 1 │ S+数位DP20261117 │ 1:25:22 │ 1080p（2160x1080） │ ready │  是  │      │
└───┴──────────────────┴─────────┴────────────────────┴───────┴──────┴──────┘
```

`archive` 默认也会为每场比赛顺带抓前 3 个视频的元信息（表格里多一列「视频」），
用 `--no-videos` 关掉。

---

### `origin` — 原题查询

把题目题面送进 [CPRet](https://cpret.online/) 做相似度检索：**最高相似度严格大于阈值**
（默认 `0.70`）即判为命中原题，否则视为「无原题」，并保留最高相似度供人工复核。

命中的原题按 **VJudge 题号**缓存（`洛谷-P1000`、`CodeForces-1234A`、`AtCoder-abc154_e`
这种 `OJ-题号` 形式），写进 `progress.json` 的题目记录里，重复查询直接命中缓存。

```bash
nflsoj origin 2647                     # 整场 7 题
nflsoj origin 2647 1 3                 # 只查第 1、3 题（也可用题目 ID）
nflsoj origin --all                    # 一次性查询本地全部题目并缓存题号（已缓存的跳过）
nflsoj origin --all --keyword 数位      # 只查名称匹配的比赛
nflsoj origin --all --refresh          # 全部重查（忽略缓存）
nflsoj origin --all --limit 50         # 本次最多查 50 题（分批跑）
nflsoj origin 2647 --threshold 0.85    # 提高判定门槛
nflsoj origin 2647 --json              # 纯 JSON 输出（含候选与相似度）
nflsoj archive 2647 --origin           # 归档时顺带查，结果写进题面 Markdown
```

| 参数 / 选项 | 说明 |
|---|---|
| `SPEC`（位置参数） | 比赛 ID（或唯一前缀 / 名称关键词） |
| `题号 ...` | 只查询这些题（题号或题目 ID，可多个；省略则整场） |
| `--threshold F` | 相似度阈值，**严格大于**才算命中（默认 `0.70`） |
| `--all` | 一次性查询本地**全部已同步比赛**的题目；已有结果的题默认跳过（增量） |
| `--keyword TEXT` | 配合 `--all`：只查名称含该关键词的比赛 |
| `--phase {...}` | 配合 `--all`：只查该状态的比赛 |
| `--limit N` | 本次最多查询多少题（默认 `0` = 全部，可分批跑） |
| `--workers N` | 并发查询数（默认 `2`，内部上限 8；服务端有限流，开太大只会更慢） |
| `--refresh` | 忽略缓存，重新抓题面并重新查询 |
| `--json` | 以 JSON 输出（stdout 只有 JSON） |

输出示例（NFLSOJ 比赛 2647 实测，阈值 0.70）：

```text
┌──────┬───────────────┬────────┬──────┬──────────────────┬────────────┐
│ 题号 │ 题目          │ 相似度 │ 判定 │ 原题题号         │ 来源       │
├──────┼───────────────┼────────┼──────┼──────────────────┼────────────┤
│    1 │ 非零数位      │ 0.8847 │ 原题 │ AtCoder-abc154_e │ AtCoder    │
│    2 │ A类B类数      │ 0.8979 │ 原题 │ 洛谷-B2140       │ Luogu      │
│    3 │ 奶牛的叫唤2.0 │ 0.9120 │ 原题 │ QOJ-21095        │ QOJ        │
│    4 │ 除九的余数    │ 0.7141 │ 原题 │ QOJ-12975        │ QOJ        │
│    5 │ 数位LIS       │ 0.7307 │ 原题 │ 无题号           │ BZOJ       │
│    6 │ 排序数        │ 0.9223 │ 原题 │ CodeForces-908G  │ Codeforces │
│    7 │ tickets       │ 0.8435 │ 原题 │ 无题号           │ BZOJ       │
└──────┴───────────────┴────────┴──────┴──────────────────┴────────────┘
      3. 奶牛的叫唤2.0：最高分题目（BZOJ）在 VJudge 不可用，改用同一道题的 QOJ 版本：QOJ-21095
      5. 数位LIS：VJudge 的 BZOJ 已停止服务
```

阈值提高到 `0.95` 时同样几题都会变成「未命中」——判定是**严格大于**，
所以 `0.9223 > 0.95` 不成立。想把这些也算原题，把阈值调到 `0.9` 即可。

---

## Markdown 归档详解

`archive` 把一场比赛整理成一个可以直接用 Obsidian / Typora / VS Code 浏览的目录：
**一份比赛首页 `index.md` + 每道题一份 Markdown**，并在归档根目录维护一份总目录。

### 目录结构

```text
~/.nflsoj/archive/                     # 归档根目录（--dir / config --set-archive-dir 可改）
├── index.md                           # 总目录：本归档目录下所有比赛的索引
├── 2643-S+线段树/
│   ├── index.md                       # 比赛首页：说明 / 公告 / 题目列表 / 相关链接
│   └── problems/
│       ├── 01-第K值.md
│       ├── 02-集装箱堆场.md
│       └── ...
└── 2645-S+20261114周赛/
    └── index.md                       # 0 题的比赛（讲座类）也会生成首页
```

命名规则：

| 对象 | 规则 | 示例 |
|---|---|---|
| 比赛目录 | `<比赛ID>-<标题 slug>` | `2643-S+线段树` |
| 题目文件 | `<两位题号>-<标题 slug>.md` | `01-第K值.md` |
| 同名冲突 | 追加 `-2`、`-3`… | `01-第K值-2.md` |

`slug` 会保留中文，把 `\ / : * ? " < > |` 与控制字符替换为 `-`，空白折叠为 `-`，
截断到 44 个字符；空标题回退为 `contest` / `problem`；Windows 保留名（`CON`、`NUL`、`COM1`…）
前面加 `_`。

### front matter 字段

**比赛首页**（`<比赛目录>/index.md`）：

| 字段 | 含义 |
|---|---|
| `title` | 比赛名称 |
| `contest_id` | 比赛 ID |
| `mode` | 赛制（`IOI` / `CUSTOM` / `LECTURE`…） |
| `phase` | `not_started` / `running` / `ended` |
| `start_time` / `end_time` | `YYYY-MM-DD HH:MM`（本地时区） |
| `creator` | 创建者（无则空串） |
| `groups` | 分组，YAML 行内序列，如 `["2026 集训"]` |
| `problems` | 题目数 |
| `local_done` / `local_doing` | 本地已标记的已完成 / 正在做数量 |
| `archived_at` | 归档时间（ISO 8601，带本地偏移） |
| `source` | 原站比赛链接 |
| `generator` | `nflsoj <版本>` |

**题目文件**（`problems/NN-xxx.md`）：

| 字段 | 含义 |
|---|---|
| `title` | 题目标题 |
| `problem_id` | 站点题目 ID |
| `contest_id` / `contest_name` | 所属比赛 |
| `display_order` | 题号 |
| `tags` | 标签，YAML 行内序列 |
| `score_weight` | 分值（服务端给出时） |
| `local_state` | 本地标记：`todo` / `doing` / `done` |
| `submitted` | 服务端是否已有提交记录 |
| `accepted_users` / `submitted_users` | 通过人数 / 提交人数 |
| `archived_at` / `source` / `generator` | 同比赛首页 |

**总目录**（归档根目录 `index.md`）：`title` / `user` / `contests` / `problems` /
`statements` / `updated_at` / `generator`。

### 比赛首页示例

```markdown
---
title: "S+线段树"
contest_id: 2643
mode: "LECTURE"
phase: "ended"
start_time: "2026-11-07 17:00"
end_time: "2026-11-11 17:00"
creator: ""
groups: []
problems: 8
local_done: 2
local_doing: 1
archived_at: "2026-11-14T21:49:38+08:00"
source: "https://nflsoi.cc:20035/c/2643"
generator: "nflsoj 1.1.0"
---

# S+线段树

> 比赛 #2643 · LECTURE · 已结束 · 2026-11-07 17:00 ~ 2026-11-11 17:00

**本地进度**：2/8 已完成，另有 1 题正在做

## 比赛说明

[seg](https://nflsoi.cc:20035/judge/segment-tree-lab.html)

## 题目列表

| # | 题目 | 本地标记 | 标签 | 分数 | 通过 / 提交 | 题面 |
|---:|---|---|---|---:|---:|---|
| 1 | [第K值](problems/01-第K值.md) | 已完成 | `线段树` | 100 | 97 / 99 | 已保存 |
| 3 | [最长连续严格递增段LCIS](problems/03-最长连续严格递增段LCIS.md) | 正在做 | `线段树` | 100 | 60 / 62 | 已保存 |

## 相关链接

- 原站比赛页：https://nflsoi.cc:20035/c/2643
- 原站题目页：https://nflsoi.cc:20035/c/2643/p/1
- 本地索引：[返回总目录](../index.md)

---
*归档于 2026-11-14T21:49:38+08:00 · nflsoj 1.1.0 · 内容以原站为准*
```

题目文件正文以 `# 1. 第K值` 开头，接一行 `> 比赛 [S+线段树](../index.md) · 题目 #31766 · 本地标记：已完成`，
再用两空格换行渲染「**标签** / **通过 / 提交** / **分值** / **你的最好提交**」，
`---` 之后是站点的原始题面（原样保留 Markdown、公式与代码块），末尾附原始链接与归档时间。

生成时的内容处理：

- **链接绝对化**：题面/说明里的站内链接（`](/xxx)` 这种以 `/` 开头的地址）会补成完整网址，离线阅读也能点回原站。
- **Markdown 规范化**：统一换行、去掉行尾空白、压缩连续空行、代码块未闭合时自动补上收尾围栏。
- **表格转义**：标题与标签里的 `|` 会被转义为 `\|`，不会撑破表格。
- **题面列状态**：比赛首页「题面」列会显示 `已保存` / `缺失`（本次抓了但没拿到）/ `未抓取`
  （用了 `--no-statements`）/ `未归档`（没有对应文件），一眼能看出缺了什么。
- 题目权限不足或站点没返回正文时，会写入 `*（未能获取题目正文：<原因>）*`，并在结果表里记为失败。
- 比赛未开放题目列表（`canViewProblems = false`）时，题目列表处会写明原因。

### 题面缓存

题面按题目 ID 单独缓存在 `<数据目录>/cache/statements/<题目ID>.json`：

```jsonc
{
  "id": 31766,
  "title": "第K值",
  "statement": "……站点返回的 Markdown 原文……",
  "statistics": { "...": "站点统计字段" },
  "fetchedAt": 1794664043
}
```

- 缓存有效期 **30 天**（`STATEMENT_CACHE_TTL = 30 * 86400`）；过期、损坏或缺 `statement` 都视为未命中。
- 命中缓存时不会发任何题面请求，因此反复归档同一场比赛几乎不产生流量。
- `--force` 忽略缓存强制重新抓取，并刷新缓存文件。
- 缓存只用于 `archive`；`--no-statements` 完全不读也不写缓存。

### 增量写入

所有 Markdown 都经 `write_if_changed` 落盘：先读旧内容比较，**内容完全相同就不写文件**
（保持 mtime 稳定，避免同步盘/版本控制里全是噪音），有变化才「临时文件 + 原子替换」。
因此：

- 归档结果里的「新增/更新 N 个文件，M 个文件内容未变」就是这一机制的统计。
- 只有内容真的变了（题面更新、标记变化、归档时间跨秒导致 `archived_at` 变化等）才会重写。
- 同一场比赛的题面抓取在内部用线程池并发（`--workers`），比赛之间逐场串行，每场结束就保存一次进度。
- 用 `--no-statements` 时**不会覆盖**已经存在的题目文件（避免把之前存好的题面换成
  「未能获取正文」的占位文字），`archivedStatements` 按现存的有效题面文件数统计；
  首次归档时仍会为每题生成一份只有元信息、没有正文的占位文件。

根目录的总目录只汇总「位于当前归档目录下」的存档：换一个 `--dir` 归档不会把别的目录里的
比赛写进本目录 `index.md`，多个存档目录互不串台。

---

## 讲解视频

站点的讲解视频**不在题目里，而在「比赛说明」里**，通常是一行 Markdown 链接：

```markdown
[S+数位DP20261117](/videoshow/a9ba0323-bbc2-43c6-a299-6f7a7f9baa2c)
```

`nflsoj` 会把这类链接解析出来，查 `/api/video/getVideo?displayUuid=<uuid>` 拿到元信息。
**默认只抓前 3 个**（21 场比赛里 39 场都有视频，全抓既慢又占空间），
`--limit` 可调，`--no-videos` 可关。

### 站点视频的实际形态

站点用 [Shaka Packager](https://github.com/shaka-project/shaka-packager) 把每次讲解转成
**DASH + ClearKey（CENC）加密**的流：

| 字段 | 含义 |
|---|---|
| `displayName` | 视频标题（如 `20261121讲解`） |
| `status` / `transcodeProgress` | 转码状态，`ready` + 100 表示可播放 |
| `mpdUrl` | DASH 清单（几分钟内有效？不是——签名有效期约 **20 小时**） |
| `videoUrl` / `audioUrl` | 视频轨 / 音频轨的**签名直链**（MinIO，AES 加密） |
| `keyIdHex` / `keyHex` | ClearKey 的 KID 与**解密密钥**（php 端直接下发） |

也就是说：一堂 43 分钟的课被拆成「视频轨 + 音频轨」两个 mp4 容器，
两者都用同一个 `keyHex` 做 AES-CTR 加密。**只下载原始文件是打不开的**，
必须先解密、再合并。

### 保存的内容

`videos --save`（或 `archive`）会在 `<归档目录>/<比赛>/videos/` 下生成：

```text
2647-S+数位DP/videos/
├── 01-S+数位DP20261117.json      # 元信息：标题、时长、分辨率、清单/直链、keyHex、体积
├── 01-S+数位DP20261117.mpd       # DASH 清单（2 KB，离线也能看清流结构）
├── 01-….video.mp4                # 可选：--download 才有（CENC 加密，几十 MB）
├── 01-….audio.mp4                # 可选：同上
├── decrypt.ps1                   # 可选：Windows 解密/合并助手（需 ffmpeg）
└── decrypt.sh                    # 可选：POSIX 版
```

JSON 里除了 URL，还有 **`keyHex`**——这是把视频变成可播放文件的关键。
签名直链约 20 小时后失效，但 `keyHex` 与 `.mpd` 长期有效，
失效后重新跑一次 `nflsoj videos <id> --save` 即可换取新地址。

### 一次性下载全部视频

```bash
nflsoj videos --all --download                    # 全部比赛、全部视频
nflsoj videos --all --download --max-mb 200       # 只下载小于 200MB 的文件
nflsoj videos --all --save                        # 只存元信息（秒级）
nflsoj videos --all --keyword 数位 --download      # 只处理匹配的比赛
nflsoj videos 2647 --download                     # 单场（--download 隐含 --save）
```

批量模式会逐场处理、显示"处理比赛"进度，最后汇总：

```text
  ✔ 已处理 20 场比赛、20 个视频到 ~/.nflsoj/archive
      已下载 20 个媒体文件（共 1.9 GB）
      解密：进入各比赛的 videos/ 目录运行 decrypt.ps1（需要 ffmpeg）
```

被 `--max-mb` 挡掉的文件不会静默失败——结尾会提示「N 个文件超过 --max-mb=X 已跳过」，
每个视频的 JSON 里也记录了 `downloadErrors`。**建议先用 `--max-mb` 卡一个上限试跑**，
确认网络与磁盘后去掉上限再全量下载。

### 让下载到的视频可播放（需要 ffmpeg）

本项目**零依赖、不内置解密**（纯 Python 做 CENC 解密 + mp4 重封装不现实）。
下载媒体后，工具会生成 `decrypt.ps1` / `decrypt.sh`，装好
[ffmpeg](https://ffmpeg.org/download.html) 后直接运行即可：

```powershell
cd ~\.nflsoj\archive\2647-S+数位DP\videos
.\decrypt.ps1
```

脚本里实际执行的命令（每个视频一行）：

```bash
ffmpeg -y -decryption_key <keyHex> -i "01-x.video.mp4" \
       -decryption_key <keyHex> -i "01-x.audio.mp4" \
       -map 0:v:0 -map 1:a:0 -c copy "x.decrypted.mp4"
```

没有 ffmpeg 时，也可以把 `.mpd` + `keyHex` 交给支持 ClearKey 的播放器
（如 Shaka Player Demo、带插件的 VLC）。

### 体积与时间参考

| 项目 | 实测 |
|---|---|
| 单个视频（1080p，43 分钟） | 视频轨 ~112 MB + 音频轨 ~77 MB，共 ~190 MB |
| 下载速度 | 约 1 MB/s（取决于到 MinIO 的网络），单个视频 1.5~3 分钟 |
| 只存元信息 + 清单 | 每个视频 ~4 KB，1 秒内完成 |

因此：**默认只存元信息**；确实要离线看再用 `--download`，并用 `--max-mb` 卡住上限
（默认 100 MB，超过就跳过并说明原因，不会静默下半个文件）。

### 常见问题

- **`描述里的视频 0 个`**：这场比赛说明里没有视频链接，或者视频写在了题目正文里
  （目前只从比赛说明解析）。
- **`status` 不是 `ready`**：站点还在转码，过几小时再试。
- **下载报 `HTTP 403`**：签名过期了，重新 `videos --save --download` 换新地址。
- **`video 文件 111.5 MB，超过 --video-max-mb=100，已跳过`**：按提示调大上限即可。

---

## 原题查询

`origin` 把题面送进 **CPRet**（<https://cpret.online/>，一个按文本相似度的 OI 题库检索服务），
用最高相似度判定这道题是不是「别的 OJ 上已有的题」。

### 判定规则

| 情况 | 判定 |
|---|---|
| 最高相似度 **> threshold**（默认 `0.70`） | 命中原题，取相似度最高的那条 |
| 最高相似度 **≤ threshold** | 视为「无原题」，但保留最高相似度供人工复核 |

注意是**严格大于**：`0.7000` 不算命中，`0.7001` 才算。

`threshold` 的取值顺序：`--threshold` > `config.json` 的 `originThreshold` > 默认 `0.7`。

### 缓存格式（VJudge 题号）

命中的原题统一缓存成 **VJudge 题号**，即 `<OJ键>-<题号>`：

| CPRet 来源 | VJudge 题号示例 | 说明 |
|---|---|---|
| Luogu | `洛谷-P1000` | 洛谷在 VJudge 的键是中文「洛谷」 |
| Codeforces | `CodeForces-1234A` | 注意大写 F |
| AtCoder | `AtCoder-abc154_e` | 直接取 task 名 |
| Nowcoder | `牛客-51117` | |
| UOJ | `UniversalOJ-21` | |
| QOJ / LibreOJ / SPOJ / Kattis … | `QOJ-18512` / `LibreOJ-104` | |

缓存写进 `progress.json`，每题一条：

```jsonc
{
  "problemId": 15276,
  "title": "非零数位",
  "origin": {
    "matched": true,
    "score": 0.8847,
    "threshold": 0.7,
    "vjudgeId": "AtCoder-abc154_e",
    "source": "AtCoder",
    "title": "[ABC154E] Almost Everywhere Zero",
    "url": "https://atcoder.jp/contests/abc154/tasks/abc154_e",
    "queriedAt": 1795435000,
    "top": [{"source": "AtCoder", "score": 0.8847, "vjudgeId": "AtCoder-abc154_e"}, ...]
  }
}
```

VJudge 不支持或已停服的来源（`BZOJ`、`Hydro`、`LeetCode` 等）仍会判为命中，
但题号记为「无题号」并在备注里说明原因；若同一道题在别的 OJ 上有可映射的版本
（CPRet 的「合并重复题」分组），会自动改用那个版本并注明。

### 两级缓存

1. **CPRet 结果缓存**：`<数据目录>/cache/cpret/<题面哈希>.json`，有效期 14 天。
   同一份题面重复查询不再访问网络；换阈值只是本地重算，不重新搜索。
2. **每题判定缓存**：`progress.json` 里的 `origin` 字段，随 `sync` 一起保留。

`--refresh` 同时跳过这两级缓存（重新抓题面 + 重新查询）。

### 性能

| 阶段 | 实测 |
|---|---|
| 单题查询（CPRet） | 0.6 ~ 1.2 秒（服务端 20 万+ 题库里检索） |
| 整场 7 题（2 并发） | 3 ~ 4 秒（含题面抓取） |
| 全部命中缓存 | < 0.2 秒 |

服务端有 QPS 限制，默认 `--workers 2` + 每次请求间隔 0.4 秒；开太大只会变慢。

### 批量：一次性查完全部题目

`origin --all` 会把本地所有已同步比赛的题目跑一遍，并把命中的题号写进 `progress.json`：

```bash
nflsoj origin --all                 # 首次：全部题目查一遍（可分批，见 --limit）
nflsoj origin --all --limit 50      # 每次最多 50 题，跑几轮即可覆盖全部
nflsoj origin --all --refresh       # 已有结果也重查
nflsoj origin --all --keyword 数位   # 只查匹配的比赛
nflsoj origin --all --json          # 机器可读的完整结果
```

批量模式的特点：

- **增量**：已有 `score` 结果（也就是真的查过）的题默认跳过，第二次跑只补新题；
- **拿不到题面的题不缓存**（未开始 / 无权限的题会在下次运行时自动重试），
  结尾会提示「其中 N 题拿不到题面」；
- 进度条分两段：获取题面 / 查询 CPRet；每 20 题落盘一次，中断后可继续；
- 结尾汇总「查询 N 题、命中 M 题、跳过 K 题、用时 X」。

### Hydro 版

`hydrooj` 也有同样的 `origin` 命令（短 ID 形态，例如 `hydrooj origin 6a323c90`，
批量用 `hydrooj origin --all`），题面来自 Hydro 的 `pdoc.content`，其余行为完全一致。

---

## 离线模式

`--offline` 表示「只用本地数据」：任何需要访问站点的操作都会立刻失败，而不是静默联网。

```bash
nflsoj --offline contests           # 只读缓存
nflsoj --offline problems 2643
nflsoj --offline mark 2643 3
nflsoj --offline stats
nflsoj --offline export -o 记录.md
```

被禁止的操作会报：`已启用离线模式（--offline），该操作需要访问站点`，并提示「去掉 --offline 后重试」。
典型会触发的情况：`sync`、`whoami`、`contests --live`、`problems --refresh`、
本地没有题目缓存的 `problems` / `mark`，以及 `archive`。
（`login` 同样受 `--offline` 拦截：它本来就需要联网，离线时应当去掉这个参数。）

与 `--offline` 相对的是 `--online`：它只影响**不带子命令的概览面板**，让面板联网校验登录状态。

### 哪些命令本来就不联网

| 命令 | 是否联网 | 说明 |
|---|---|---|
| `stats` / `export` / `open` / `config` / `logout` | 否 | 纯本地读写 |
| `contests` | 否（除非 `--live` 或本地为空） | 本地为空时会自动同步一次 |
| `problems` / `mark` | 否（除非 `--refresh` 或本地缺题单） | 有缓存就直接用 |
| 概览面板（无子命令） | 否（除非 `--online`） | 默认只读本地 |
| `login` / `whoami` / `sync` / `archive` | 是 | 必须访问站点 |

换句话说：**同步一次之后，日常的查看、标记、统计、导出全都可以离线完成**。

---

## 性能

这个工具在「日常使用」路径上刻意避免了网络：

| 优化 | 实现 | 效果 |
|---|---|---|
| 本地命令不联网 | 账号取本地 `config.json` / `progress.json` 缓存，不调 `getSessionInfo` | `stats` / `export` / 缓存命中时的 `contests` `problems` 毫秒级返回 |
| HTTP 长连接复用 | 每线程一条 `http.client` keep-alive 连接，`Connection: keep-alive` + `gzip` | 省掉每次请求的 TLS 握手（代码注释实测约 1.2–1.8 秒/次） |
| 并发抓取 | `ThreadPoolExecutor`，默认 8 线程（同步详情上限 16、题面上限 12） | 详情请求几乎线性加速；实测 8 并发最稳，16 并发反而被站点拖慢 |
| 增量同步 | 已有 `syncedAt` 的比赛跳过详情；连续两页全已同步即停止翻页 | 日常同步通常只发几页列表请求，而不是全量 |
| 流水线 | 列表分页与详情抓取并行推进 | 总耗时 ≈ max(列表请求, 详情请求)，而不是两者相加 |
| 题面缓存 | `cache/statements/<题目ID>.json`，30 天 | 重复归档不再请求题面 |
| 内容未变不写盘 | `write_if_changed` + 沿用已有 `archived_at` | 重复归档 0 写入、0 题面请求，mtime 不抖动 |
| 避免多余会话校验 | 本地已有账号名时不再联网确认身份 | 每次命令省掉约 2–3 秒的 `getSessionInfo` |

**首次全量同步 vs 日常增量同步**（量级示意，实际取决于比赛总数与网络）：

| 场景 | 列表请求 | 详情请求 | 主观感受 |
|---|---|---|---|
| 首次 `sync`（N 场，每页 20） | ⌈N/20⌉ 次（如 ~11 次） | 约 N 次（8 并发） | 通常几十秒到 1 分钟量级 |
| 日常 `sync`（增量） | 前 2 页左右即停止 | 只抓新出现的比赛 | 明显更快，通常几秒 |
| `sync --contest 2643` | 0 次（直接按 ID 取） | 1 次 | 一两个请求的时间 |
| `sync --force` | 全部分页 | 全部 N 次 | 与首次相当 |
| 再次 `archive 2643`（缓存命中） | 1 次（比赛详情） | 0 次题面 | 只写有变化的文件 |

调参建议：

- 站点卡顿时先用 `--workers 4`，不要盲目加大并发。
- 网络慢 / 代理环境用 `--timeout 60`。
- 只想看本地数据时加 `--offline`，可以确保不会因为一个手误触发整轮同步。

**`hydrooj`（Hydro 站）实测**（本机 + 默认 6 并发，站点在本机附近、单请求约 10–50 ms）：

| 场景 | 耗时 |
|---|---|
| 本地命令：`stats` / `export` / 缓存的 `contests`、`problems` | 约 0.3–0.5 s（含 Python 启动） |
| `contests --live --limit 5` | 约 0.5 s |
| `sync --limit 60`（60 场 / 252 题） | 约 1 s（另有 2.5 s 进程与首次握手） |
| 增量 `sync`（前两页已同步即停） | 约 0.5 s |
| `archive` 5 场 15 题（含抓 15 份题面） | 约 3.5 s |
| 重复 `archive`（题面缓存命中、内容未变） | 约 1 s，**0 个文件被重写** |

> Hydro 站的网关会随机返回 503 拦截页，工具会自动退避重试，因此偶发的额外耗时属正常现象。

---

## 从 1.0 升级

### 2.1 → 2.2

2.2.0 把「批量」做成一等公民，并精简了一批参数：

| 变更 | 2.1 | 2.2 |
|---|---|---|
| 原题查询 | 只能按单场比赛查 | 新增 `origin --all`：一次性查完全部题目并缓存题号（增量、可分批） |
| 讲解视频 | 只能按单场处理，默认前 3 个 | 新增 `videos --all`：一次性处理/下载全部视频；单场默认全部 |
| `videos --download` | 必须配合 `--save` | `--download` 隐含保存，少打一个参数 |
| `origin` 参数 | `--top`、`--refresh-problems` | 已移除（候选数固定、题目列表缓存自动维护） |
| `videos` 的 `--force` | 单独存在 | 统一为 `--refresh`（`--force` 仍可用） |
| 失败重试 | 拿不到题面也会写入缓存 | 只有真正查过（有相似度）的题才缓存，无题面的题下次自动重试 |

### 2.0 → 2.1

2.1.0 新增**原题查询**（两个工具都有）：

| 变更 | 1.3 | 1.4 |
|---|---|---|
| 原题查询 | 无 | 新增 `origin` 命令（别名 `orig` / `src`）：送入 CPRet 查相似度，严格大于阈值即命中 |
| 判定缓存 | 无 | 命中结果按 VJudge 题号（`洛谷-P1000`、`CodeForces-1234A`）写进 `progress.json` |
| 结果缓存 | 无 | 题面哈希为键的结果缓存（14 天），换阈值不必联网 |
| 归档 | 只有题面 | `archive --origin` 把「## 原题」区块写进题目 Markdown 与 front matter |
| 命令 | `nflsoj` / `hydrooj` 各 12 个 | 各 13 个（新增 `origin`） |

### 1.2 → 1.3

1.3.0 新增**讲解视频**抓取，并修掉一次站点 API 变更导致的失效：

| 变更 | 1.2 | 1.3 |
|---|---|---|
| 讲解视频 | 只有题面，视频链接留在说明里没处理 | 新增 `videos` 命令；`archive` 默认顺带抓前 3 个视频的元信息与 DASH 清单 |
| 媒体下载 | 无 | `--video-download` / `--max-mb`（默认只存元信息，媒体按需下载） |
| 解密 | 无 | 下载后自动生成 `decrypt.ps1` / `decrypt.sh`（需 ffmpeg） |
| `getContestList` | 带 `returnCount` 参数 | 站点 2026-11 起**拒绝该参数（400）**，已移除；响应中的 `count` 仍然可用 |

> ⚠️ 如果你在用旧版本，`sync` / `contests --live` 会因 `property returnCount should not exist`
> 失败——那是站点 API 变更，升级到 1.3.0 即可。

### 1.2.0 稳定性加固（同一版本内的修补）

在 1.2.0 上做了一轮全面体检（异常/边界输入、错误处理、启动退出、资源释放、日志诊断、
兼容性），修掉了 30 余个问题。**这些都是修复，不改变正常用法**；只有下面几处行为更严格/更友好：

| 场景 | 之前 | 现在 |
|---|---|---|
| `--timeout 0` / 负数 | socket 抛 `ValueError`，打整段 traceback | 用法错误：`--timeout 不能小于 1`，退出码 2 |
| `--timeout` 超大 | 直接传给 socket | 钳到 3600 秒 |
| `--width 0` / 负数 | 被当成"未指定"，或钳成 40 | 明确表示「跟随终端」 |
| `--limit` 负数 | 静默切掉列表末尾几行 | 用法错误；`--limit 0` 表示「全部」 |
| `--limit` 超大 | — | 正常接受（不限制） |
| `--workers` 超大 | 真的开上千线程 | 钳到 16 并在标题下方标注「999 → 16（已钳位）」 |
| `--state doen`（拼错） | 静默显示空表格 | 明确报「无法识别的状态」并列出可用值 |
| `config.json` / `progress.json` 损坏 | 静默当成空数据，**下次写入会把原数据冲掉** | 先备份为 `*.corrupt-<时间戳>` 并告警，再以空数据继续（原文件可恢复） |
| `progress.json` 结构异常（`users` 不是对象等） | `AttributeError` traceback | 逐层校验并降级为可用结构，只提示一次 |
| 比赛/题目时间是对象、数组或乱码 | `TypeError: unhashable type` 崩溃 | 显示 `-`，阶段显示「时间未知」 |
| `--dir` 指向已存在的文件 | `FileExistsError` traceback | 提示「用 --dir 指定一个目录」，退出码 1 |
| `--api` 写成非法 URL | `ValueError` traceback | 提示地址无效，退出码 1 |
| 站点拒绝连接 / DNS 失败 | 每个请求重试到超时（`archive` 要等几分钟） | 立即失败并提示检查 `--api` |
| 站点 3xx 跳转 | 跳转消耗重试预算，`retries=0` 时直接返回空响应 | 跳转单独计数（最多 5 跳），跨站跳转直接拒绝并说明原因 |
| 响应 gzip 损坏 | 压缩字节被当正文，报「非 JSON」 | 明确报「gzip 解码失败，可能被代理改写」 |
| 输出重定向到非 UTF-8 文件 | `UnicodeEncodeError` traceback | 自动降级为可打印字符 |
| 下游管道提前关闭（`\| head`） | 退出码 120 + `Exception ignored` | 静默退出，退出码 0 |
| Ctrl+C | 可能卡在等待线程池 | 立即取消并退出，退出码 130；已完成的进度先落盘 |
| 工具自身未预期的异常 | 整段 traceback | 一行中文提示；`--debug` 或设置 `NFLSOJ_TRACEBACK=1` 才显示堆栈 |
| 归档路径超过 Windows 260 字符 | 含糊的「无法写入文件」 | 明确提示路径过长并给出三种解法 |
| 归档文件被编辑器占用 | 直接失败 | 自动重试 3 次，仍失败时提示「关闭占用的程序」 |
| `--limit 0` 的列表命令 | 显示 0 行 | 显示全部（`--limit 0` = 不限制） |

另外顺手做的改进：

- **性能**：`contests --live` / `sync --no-problems` 的列表阶段改为并发翻页（11 页从 ~5.7s 降到 ~1s）；
  归档复用同一个线程池且线程退出时回收 keep-alive 连接，长时间 `archive --all` 不再堆积 socket。
- **诊断**：`--debug` 现在会打印站点地址/超时/重试配置、每次请求的耗时、重试与跳转次数，
  并在命令结束时汇总「N 次请求 / R 次重试 / D 次跳转」；凭据一律掩码，不会出现在日志里。
- **交互**：`sync` 显示实际生效的并发数；`problems --state` 过滤为空时说明是"过滤掉了"而不是"没有题目"；
  脏数据文件的告警只出现一次，并附带备份路径与恢复方法。
- **测试**：从 160 个增加到 225 个（新增 65 个健壮性用例，含本机回环 HTTP 服务模拟跳转/gzip/429）。

### 1.1 → 1.2

1.2.0 是**纯增量**版本，`nflsoj` 的命令与行为不变（旧数据、旧脚本可继续用）：

| 变更 | 1.1 | 1.2 |
|---|---|---|
| Hydro 版 | 无 | 新增 `hydrooj`（别名 `hydrooj-archive`），对接 Hydro v4.x，前端与 `nflsoj` 完全一致 |
| 控制台命令 | `nflsoj` / `nflsoj-archive` | 增加 `hydrooj` / `hydrooj-archive` |
| 共享层 | `nflsoj.py` 自带全部逻辑 | `render_*` / `print_contest_header` 增加 `link_builder` / `generator` / `source_url` 可选参数，供 Hydro 复用 |
| 归档时间戳 | 每次归档刷新 `archived_at`，导致重复归档总是重写文件 | **沿用已有文件的 `archived_at`**，重复归档真正 0 写入；`--force` 才刷新 |
| 测试 | 96 个 | 160 个（新增 64 个 Hydro 用例）→ 后续加固补齐至 225 个 |

### 1.0 → 1.1

1.1.0 与 1.0.x 的主要差异（**行为变更请重点看第一条**）：

| 变更 | 1.0 | 1.1 |
|---|---|---|
| `mark` 省略状态 | 在 `未完成 → 正在做 → 已完成` 间**循环切换** | **默认标记为「已完成」**；循环请显式加 `--next` |
| 多题标记 | 一次一题 | `mark 2643 1 2 3` 一次多题 |
| 归档 | 无 | 新增 `archive`（别名 `arc`），比赛首页 + 题面 → 本地 Markdown |
| 离线 | 无 | 新增 `--offline`（以及只影响概览面板的 `--online`） |
| 进度条 | 无开关 | 新增 `--no-progress`；`--width` 可手动指定宽度 |
| 同步 | 只有全量 | 默认**增量**；新增 `--full`、`--limit` |
| 并发默认值 | 6 | 8（建议 4–12） |
| 配置 | `--set-api` / `--clear` | 新增 `--set-archive-dir` |
| 导出 | 只有 `--format` / `-o` / `--contest` | 新增 `--keyword` / `--phase` |
| 数据目录 | `config.json` + `progress.json` | 新增 `cache/`（题面缓存）与 `archive/`（归档输出） |
| `progress.json` | 只有比赛与题目 | 比赛上新增 `archivedAt` / `archivePath` / `archivedProblems` / `archivedStatements`（可选 `archiveFailed`），题目上可带 `archivedAt` |
| 分发 | 单脚本 + `requirements.txt` | `uv` 管理的包，入口 `src/nflsoj_archive/`，Python `>=3.14` |
| 版本号 | `1.0.1` | `1.1.0` |

升级操作：

```bash
git pull
uv sync                      # 或继续用 python src/nflsoj_archive/nflsoj.py
nflsoj --version             # 确认输出 nflsoj 1.1.0
nflsoj archive --all         # 首次建立本地 Markdown 存档
```

旧数据**无需迁移**：`progress.json` 里缺 `archivedAt` 等字段就按「未归档」处理，
`config.json` 缺 `archiveDir` 就回退到 `<数据目录>/archive`。
唯一需要改习惯的是上面第一条：想循环请用 `--next`，否则 `mark 2643 3` 会直接变成「已完成」。

---

## 数据存储

默认全部存放在 `~/.nflsoj/`（Windows 为 `C:\Users\<你>\.nflsoj\`），可用 `--data-dir`
或环境变量 `NFLSOJ_DATA_DIR` 更改；`--data-dir` 优先级最高。
所有 JSON 都采用「临时文件 + `os.replace` 原子替换」写入；在类 Unix 系统上还会把文件权限设为 `0600`。
读取时兼容 UTF-8 BOM（`utf-8-sig`），手动用记事本改过 JSON 也不会解析失败。

```text
~/.nflsoj/
├── config.json          # 站点地址与登录凭据
├── progress.json        # 账号 → 比赛 → 题目 的缓存与做题标记
├── cache/
│   ├── statements/      # 题面缓存 <题目ID>.json，30 天有效
│   │   └── 31766.json
│   └── cpret/           # 原题查询结果缓存（题面哈希为键），14 天有效
└── archive/             # archive 命令的默认输出目录
    ├── index.md
    └── 2643-S+线段树/…
        └── videos/      # 讲解视频元信息 / DASH 清单 /（可选）媒体文件
```

| 路径 | 职责 |
|---|---|
| `config.json` | 站点 API 地址（`baseUrl`）、`token`、`cookie`、记录账号（`username`）、可选 `archiveDir` |
| `progress.json` | 按用户名分区的比赛缓存、题目列表、本地标记（`state` / `stateUpdatedAt`）、同步时间、归档元数据 |
| `cache/statements/` | 按题目 ID 缓存的题面 JSON，供 `archive` 复用，避免重复抓取 |
| `archive/` | 归档生成的 Markdown（可用 `--dir` 或 `config --set-archive-dir` 指向别处） |
| `archive/<比赛>/videos/` | 讲解视频的元信息 JSON（含 `keyHex`）、DASH 清单 `.mpd`，以及 `--video-download` 时的媒体文件与解密助手脚本 |

`config.json` 结构示意：

```jsonc
{
  "version": 1,
  "baseUrl": "https://nflsoi.cc:20035",
  "token": "eyJhbGciOi...",
  "cookie": "",
  "username": "gaoshenghan",
  "archiveDir": "D:\\notes\\nflsoj"     // 可选；缺省用 <数据目录>/archive
}
```

`progress.json` 结构示意（含 1.1 新增的归档字段）：

```jsonc
{
  "version": 1,
  "users": {
    "gaoshenghan": {
      "contests": {
        "2643": {
          "id": 2643,
          "name": "S+线段树",
          "mode": "LECTURE",
          "startTime": "2026-11-07T09:00:00.000Z",
          "endTime": "2026-11-11T09:00:00.000Z",
          "phase": "ENDED",
          "groups": ["2026 集训"],        // 可选：站点给了分组才有
          "creator": "teacher",           // 可选：站点给了创建者才有
          "problems": [
            {
              "problemId": 31766,
              "displayOrder": 1,
              "title": "第K值",
              "tags": ["线段树"],
              "submitted": true,
              "scoreWeight": 100,
              "numAcceptedUsers": 97,
              "numSubmittedUsers": 99,
              "numNonZeroUsers": 99,
              "bestStatus": "Accepted",
              "state": "done",              // 本地标记：todo / doing / done（缺省 = 未标记）
              "stateUpdatedAt": 1794664043,  // 本地标记时间
              "archivedAt": 1794664366       // 该题题面被归档的时间（可选）
            }
          ],
          "syncedAt": 1794664366,            // 题目列表最后一次同步时间
          "archivedAt": 1794664366,          // 整场最后一次归档时间
          "archivePath": "D:\\notes\\nflsoj\\2643-S+线段树",
          "archivedProblems": 8,             // 归档时的题目数
          "archivedStatements": 8            // 成功保存的题面数
          // "archiveFailed": 1              // 仅在本次归档有失败时出现
        }
      }
    }
  }
}
```

> `state` 是你**本地手动标记**的状态；`submitted` / `bestStatus` 是服务端返回的提交情况。
> 两者互相独立：`stats` 的完成度只按本地 `state` 统计，「服务端已提交 N 题」仅供参考。

多账号：`progress.json` 按用户名分区，`config.json` 只记当前账号；`logout` 只清凭据，不删数据。
`--data-dir` 可以让不同的数据目录完全隔离（例如工作/个人两套）。

---

## 站点 API 说明

以下接口来自对站点前端 bundle 的分析，全部是 JSON REST API，基地址默认
`https://nflsoi.cc:20035`（`--api` / `config --set-api` 可改）：

| 方法 | 路径 | 关键参数 | 返回 |
|---|---|---|---|
| `GET` | `/api/auth/getSessionInfo` | — | `{userMeta, isManager, serverPreference, serverVersion, ...}` |
| `POST` | `/api/auth/login` | body `{username, password}` | `{token, username, userId}` 或 `{error}` |
| `GET` | `/api/contest/getContestList` | `skipCount`, `takeCount`, `keyword`（另支持 `createdBy`；**`returnCount` 已被站点移除，带上会 400**） | `{contests: [...], count}` |
| `GET` | `/api/contest/getContest` | `contestId`, `description`, `problemInfos`, `announcements` | `{meta, problemInfos, description, announcements, permissions, ...}` |
| `GET` | `/api/problem/getProblem` | `id`, `statement` | `{meta: {id, title}, statement, statistics, ...}` |
| `GET` | `/api/video/getVideo` | `displayUuid`（注意不是 `videoId`） | `{displayName, status, transcodeProgress, mpdUrl, videoUrl, audioUrl, keyHex, keyIdHex}` |
| `POST` | `https://cpret.online/api/search` | body `{q, page:1, count:false, dedup:true, mmr:false, mode:"text"}` | `{html, total, elapsed}`，结果卡片在 `html` 里 |

视频接口返回的 `mpdUrl` / `videoUrl` / `audioUrl` 是 **MinIO 的签名直链**（有效期约 20 小时），
与站点不同源，工具会为它们单独建连接且**不发送站点凭据**；`keyHex` 是 ClearKey 解密密钥。
媒体文件是 CENC 加密的 DASH 分轨，播放前需要解密 + 合并（详见「讲解视频」一章）。

> CPRet 是**第三方服务**（不是本站点）：`origin` 会把题面文本发过去。
> 若不想外发，别用 `origin` / `archive --origin` 即可，其它命令不受影响。

要点：

- **鉴权**：`Authorization: Bearer <token>`；网页端把 token 存在 `localStorage` 的
  `session-swr` 里，所以复制那段值即可。也支持直接用 `Cookie`。
- 每个请求都会带 `User-Agent: nflsoj-cli/<版本> (+python-http.client)`、`Origin`、`Referer`。
- `getContest` 的三个可选参数都有实际作用：`problemInfos=true` 才有题目列表；
  `description=true` 才有比赛说明（归档首页需要）；`announcements=true` 才有公告。
  日常同步只请求 `problemInfos`，归档时才把三者都打开。
- `getProblem` 的 `statement=true` 会返回 **Markdown 格式的题面**，这是归档正文的来源。
- 比赛列表分页大小固定为 `20`（与站点 `serverPreference.pagination.contestList` 一致）；
  代码不因「某页不足 20 条」就停止，而是尽量按响应里的 `count` 继续翻页。
- 对 `500/502/503/504/429` 自动重试（默认 2 次，指数退避 `0.6 × 2^n` 秒，上限 4 秒）；
  连接类错误重试间隔 `0.35 × (n+1)` 秒；最多跟随 3 次同域重定向。
- 常见错误码会翻译成中文提示：`401` 提示重新登录、`403 not activated` 提示账号未激活、
  `429` 提示请求过于频繁、`400` 提示站点 API 可能已升级。
- 响应支持 `gzip`，会自动解压。

> ⚠️ 站点 API 若升级，字段与接口可能变化。工具对字段名做了多路兼容
> （`normalize_contest` / `extract_problems` / `extract_announcements`），但仍以实际站点为准；
> 遇到结构异常时用 `--debug` 查看原始请求，并考虑升级工具版本。

---

## 常见问题（FAQ）

**Q1：`problems` / `mark` / `archive` 提示「找不到比赛」？**

本地还没有这场比赛的数据。先执行 `nflsoj sync`（或 `nflsoj contests --live`）把比赛同步下来。
名称支持不区分大小写的子串匹配；匹配到多场时会列出候选表，请改用比赛 ID。
如果站点上较早的比赛有变动（增量同步只回访前几页），用 `nflsoj sync --full` 完整遍历一次。

**Q2：提示「尚未登录或登录状态已失效」/「登录状态无效或已过期」？**

Token 过期了。重新执行 `nflsoj login`，粘贴最新的 `session-swr` 或 Cookie。
`whoami` 可以立刻确认当前凭据是否有效；`logout` 后再 `login` 可以彻底换账号。

**Q3：终端里中文错位、表格歪掉或出现乱码？**

- 先加 `--ascii`：边框与符号退化为 `+ - |`，几乎在所有终端都不会错位。
- 仍不对就加 `--no-color`，排除颜色转义被截断的可能。
- Windows 老控制台（非 Windows Terminal）建议换用 Windows Terminal；工具会自动尝试开启
  ANSI 支持并切到 UTF-8 代码页。
- 输出被重定向到文件时颜色与进度条本来就会关闭；需要固定宽度时用 `--width 100`。

**Q4：HTTPS 证书报错（`CERTIFICATE_VERIFY_FAILED` 之类）？**

多半是公司代理 / 抓包工具 / 自签名证书。临时用 `--insecure` 跳过校验（**有安全风险**，
不要在不可信网络上长期使用），或把代理的根证书装进系统信任库。
内网镜像站点可以用 `--api` 或 `config --set-api` 指向它。

**Q5：归档时提示题面失败 / 题面是空的？**

常见原因：该题需要报名比赛、题目未公开、账号权限不足，或站点未返回正文。
归档结果表的「失败」列会给出原因，文件里也会写 `*（未能获取题目正文：…）*`。
可以先用浏览器确认能打开该题，再 `nflsoj archive <ID> --force` 重试；
只想先存比赛首页就加 `--no-statements`。
如果比赛本身没开放题目列表（`canViewProblems = false`），首页会写明原因，不会报错。

**Q6：加了 `--offline` 反而报错？**

这正是它的设计：`--offline` 下任何需要联网的操作都会直接失败，并提示
「去掉 `--offline` 后重试」。会触发的包括 `sync`、`whoami`、`contests --live`、
`problems --refresh`、本地缺题单时的 `problems` / `mark`，以及 `archive`。
如果你只是想看看本地数据，用 `nflsoj --offline contests` / `stats` / `export` 即可。

**Q7：为什么 `stats`、`export`、`contests` 这些命令不需要联网？**

因为它们在 `sync` 阶段就已经把比赛、题目、标记全部写进了本地 `progress.json`。
这些命令只读本地文件（账号名也取本地缓存），所以是毫秒级的，断网、离线、
甚至站点挂了都能正常用。也正因如此，它们的统计口径是「**本地标记 + 本地缓存**」，
要和网站保持一致请先 `sync`。

**Q8：统计数字和网站不一致？**

统计完全基于本地标记（`state`）与本地缓存的题目列表，不看站点评测结果。
先 `nflsoj sync` 保证题单最新；「服务端已提交 N 题」只是参考，不计入完成度。

**Q9：想排查请求细节？**

加 `--debug`，每次请求与响应摘要会打印到 stderr（含状态码与字节数）。
配合 `--timeout 60` 可以区分「网络慢」和「站点报错」。

**Q10：归档会不会重复下载、反复改文件？**

不会。题面命中 `cache/statements/`（30 天）就不再请求；Markdown 内容没变就不重写文件，
归档结束会打印「新增/更新 N 个文件，M 个文件内容未变」。

**Q11：登录时报错「数据目录不可写：`C:\Users\<你>\.nflsoj`」（或 `PermissionError: ... config.json.tmp`）？**

说明当前进程对 `~/.nflsoj` 没有写权限。常见的两种原因：

1. **命令是在受限沙箱终端里跑的**（这类终端只允许写工作区目录）；
2. **`~/.nflsoj` 被设成了只读**，或被安全软件锁定。

工具会在启动时先试写一次，直接给出可操作提示而不是抛 traceback。三种解法任选：

```bash
# 1) 换一个可写的数据目录（推荐放在工作区内）
uv run nflsoj --data-dir .nflsoj login

# 2) 用环境变量改默认目录（Windows PowerShell）
$env:NFLSOJ_DATA_DIR = "D:\ojdata"

# 3) 在普通终端（非沙箱）里运行，直接沿用默认的 ~/.nflsoj
```

> 换目录前如果想保留旧的做题标记，把旧的 `config.json` 与 `progress.json`
> 复制到新目录即可（`progress.json` 格式 1.0 → 1.1 无需迁移）。

**Q12：明明在普通终端里，`uv run nflsoj login` 还是写不了 `~/.nflsoj`？**

看进程的**完整性级别**。Windows 有一条规则：从带 Low 完整性标签（Low IL）的
**可执行文件**启动的进程，本身也会是 Low IL，而 Low IL 进程无法写入 Medium IL 的位置
（`C:\Users\<你>\...` 通常就是），所以只有 `%TEMP%` 和工作区能写。

典型触发场景：`.venv` 是在某个沙箱终端里 `uv sync` 出来的，里面的 `python.exe`
被标成了 Low IL。判断方法（在出问题的终端里）：

```powershell
# 列出 python.exe 的完整性标签；显示 Low 就是它
icacls .venv\Scripts\python.exe
# 或者直接看进程的令牌
whoami /groups | Select-String "Mandatory Level"
```

修复（任选其一）：

```powershell
# A. 普通权限：删掉重建。注意要在"没有 Low 标签"的普通终端里执行
Remove-Item .venv -Recurse -Force
uv sync

# B. 管理员终端：直接把工作区和 venv 的标签抬到 Medium
icacls . /setintegritylevel Medium
icacls . /setintegritylevel Medium /T /C      # 递归（需要管理员）

# C. 不想动权限：用工作区外的解释器跑（本项目零依赖，不需要 venv）
C:\Users\<你>\.local\bin\python.exe src\nflsoj_archive\nflsoj.py login
```

> 判断"标签是不是来自目录"的小技巧：把一个正常（Medium）的 `python.exe`
> 复制进工作区再运行，如果它变成 Low，说明是目录带着 Low 标签，从里面启动的任何
> 程序都会被降级——这时用 A/B 修目录，或直接用 C。

**Q13：提示「本地数据文件损坏，已备份：…」怎么办？**

说明 `config.json` / `progress.json` 不是合法 JSON（编辑器写坏、磁盘写一半、手工改错）。
工具**不会覆盖原文件**，而是把它改名成 `<原名>.corrupt-<时间戳>`，再以空数据继续。

恢复步骤：

```powershell
# 1. 用编辑器打开备份，按提示的原因修好坏掉的部分（常见是少括号、多逗号）
notepad "$env:USERPROFILE\.nflsoj\progress.json.corrupt-20261118-213701"
# 2. 修好后改回原名（先删掉/挪走当前的 progress.json）
Move-Item "$env:USERPROFILE\.nflsoj\progress.json" progress.empty.json
Move-Item "$env:USERPROFILE\.nflsoj\progress.json.corrupt-20261118-213701" progress.json
# 3. 验证
nflsoj stats
```

如果只是结构字段类型不对（比如 `users` 被写成了数组），工具会自动降级并提示，
不需要人工修；只有"整个文件不是合法 JSON"才需要按上面步骤恢复。

**Q14：`archive` 报「路径过长」？**

Windows 默认限制单条路径 260 字符，而归档路径 = 归档目录 + `<ID>-<比赛名>` +
`problems` + `<序号>-<题目名>.md`。三种解法：

```powershell
# A. 换一个更短的归档目录（推荐）
nflsoj archive --all --dir D:\oj
# B. 缩短比赛/题目名（工具已把文件名截到 44 字符，通常够用）
# C. 启用 Windows 长路径支持（管理员，修改注册表后重启）
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force
```

**Q15：`--debug` 会泄露 token / 密码吗？**

不会。`--debug` 只打印请求方法、URL、状态码、字节数、耗时、重试与跳转次数；
令牌、Cookie、请求体一律不打印，URL 里若带凭据也会被掩码成 `***`。
需要完整堆栈排查时用 `NFLSOJ_TRACEBACK=1`（hydrooj 用 `HYDROOJ_TRACEBACK=1`），
它只影响异常打印，同样不会输出凭据。

---

## Hydro 版：hydrooj

`hydrooj` 是同一项目里的第二个工具，对接**基于 Hydro v4.x 搭建**的 OJ
（默认 `http://nflsoi.cc:10611`，可用 `--api` 或 `config --set-api` 指向别的 Hydro 站点）。

**前端与功能同 `nflsoj` 完全一致**：同一套配色、表格、进度条、徽标、命令名、选项、
提示语、归档目录结构与 Markdown 模板。只有下面这些地方不同。

### 与 nflsoj 的差异

| 项目 | `nflsoj` | `hydrooj` |
|---|---|---|
| 站点 | `https://nflsoi.cc:20035`（私有 API） | `http://nflsoi.cc:10611`（Hydro JSON 视图） |
| 数据获取 | `<base>/api/...` + Bearer Token | `GET <path>` 加 `Accept: application/json` + 会话 Cookie |
| 登录 | Token / Cookie / 用户名密码 | **用户名密码**（自建 Cookie 会话）或直接粘贴 Cookie |
| 凭据形式 | `token` + `cookie` | `sid` / `sid.sig` 会话 Cookie |
| 比赛 ID | 纯数字（如 `2643`） | 24 位十六进制（如 `6a323c90c9f0ca7cf29b5b0c`） |
| 表格里的 ID | 完整 ID | **前 8 位短 ID**（输入时用完整 ID 或唯一前缀） |
| 题目链接 | `/c/<cid>/p/<题号>` | `/p/<pid>?tid=<cid>` |
| 赛制显示 | `mode` 原值 | `rule` 映射成 `IOI` / `ACM` / `OI` / `Ledo`… |
| 题面来源 | `statement` 字段（Markdown） | `pdoc.content` → `{"zh": "..."}`（Markdown） |
| 默认数据目录 | `~/.nflsoj` | `~/.hydrooj` |
| 默认环境变量 | `NFLSOJ_DATA_DIR` | `HYDROOJ_DATA_DIR` |
| 默认并发 | 8 | 6（站点有随机网关拦截，不宜开太高） |

### 快速开始

```bash
hydrooj login                      # 交互式：用户名密码（推荐）或粘贴 Cookie
hydrooj sync                       # 同步比赛与题目（增量）
hydrooj contests                   # 比赛列表（显示短 ID 与完成进度）
hydrooj problems 6a323c90          # 用短 ID 前缀直接打开某场比赛
hydrooj mark 6a323c90 1 2 3        # 三道题一次标记为已完成
hydrooj archive --all              # 比赛首页 + 题面 → 本地 Markdown
hydrooj stats                      # 做题统计（纯本地）
```

### 登录

```bash
hydrooj login                                  # 交互式
hydrooj login -u yangjinhua -p 密码             # 直接用户名密码
hydrooj login --cookie "sid=...; sid.sig=..."  # 用浏览器里的会话
```

> Hydro 的登录是**服务端表单**：必须先建立访客会话（`GET /login` 拿到 `sid`），
> 再 `POST` 用户名密码。工具内部用一个独立 `CookieJar` 完成这两步，成功后把
> `sid` / `sid.sig` 存进 `config.json`，之后的请求直接复用这套 Cookie。
>
> 用户名密码错误时站点返回 `403` 且页面含「密码错误」，工具会明确提示；
> 若被风控拦住则提示改用 Cookie 登录。

### 网关随机拦截（重要）

该站点的网关会**随机**返回 `503` 并附带一个英文的 “Web Access blocked” 页面
（实测约每 3 次请求就有 1 次）。这不是登录失效，也不是站点挂了。工具的处理：

- 识别响应体里的 `Web Access blocked` / `Blocked Type` / `Web Filtering` 标记；
- 自动按 1s、2s、3s… 退避重试（默认 4 次，`--timeout` 只影响单次超时）；
- 重试成功后继续，`sync` 结束时会汇总「期间站点网关拦截了 N 次请求，已自动重试成功」；
- 连续被拦时降低并发（`--workers 4`），或稍等几十秒再来。

### 常用命令对照

命令名、选项、别名与 `nflsoj` **完全一致**，把 `nflsoj` 换成 `hydrooj` 即可：

| 命令 | 说明 | Hydro 下的注意点 |
|---|---|---|
| `login` / `logout` / `whoami` / `config` | 登录与配置 | `config` 显示已保存的 Cookie 名称 |
| `sync` | 同步比赛与题目 | 默认增量；`--full` 全量（69 页 / 约 1380 场） |
| `contests` / `ls` | 比赛列表 | `--keyword` 是**客户端过滤**（站点 `?q=` 不生效） |
| `problems` / `ps` | 题目列表 | 支持唯一 ID 前缀；未报名时题目会被隐藏 |
| `mark` / `m` | 标记状态 | 省略状态 = 已完成；`--next` 循环 |
| `stats` / `st` | 做题统计 | 纯本地，秒出 |
| `open` / `o` | 浏览器打开 | 题目链接带 `?tid=` |
| `export` | 导出记录 | `md` / `csv` / `json` |
| `archive` / `arc` | Markdown 归档 | 见下 |

### 归档

```bash
hydrooj archive 6a323c90                    # 单场
hydrooj archive --all --limit 10            # 批量
hydrooj archive --keyword 模拟赛             # 按名称
hydrooj archive 6a323c90 --json             # 机器可读结果
hydrooj archive 6a323c90 --dir D:\notes\oj   # 指定目录
```

目录结构与 `nflsoj` 一致，只是目录名里带 24 位 ID：

```text
~/.hydrooj/archive/
├── index.md
└── 6a323c90c9f0ca7cf29b5b0c-NOI2026模拟赛63/
    ├── index.md                      # 比赛首页：赛制 / 时间 / 分组 / 题目表
    └── problems/
        ├── 01-Don't-Look-Back-in-Anger.md
        ├── 02-献给空白的无败冠冕（T2）.md
        └── 03-Median（T3）中位数.md
```

题面文件在通用字段之外还会带上 Hydro 特有的**时限**与**内存限制**（如
`**时限**：1000 ms`、`**内存**：256 MB`），front matter 里的 `source` 是
`/p/<pid>?tid=<cid>` 形式的原始链接。

### 离线与数据目录

与 `nflsoj` 相同：`--offline` 下 `contests` / `problems` / `stats` / `export` / `open`
照常可用，而 `login` / `sync` / `archive` 等需要联网的命令会直接报错并提示去掉该参数。

```bash
hydrooj --offline contests                 # 只用本地缓存
hydrooj --data-dir D:\ojdata login         # 临时指定数据目录
$env:HYDROOJ_DATA_DIR = "D:\ojdata"        # PowerShell 设置默认数据目录
```

### 站点 API 说明（Hydro JSON 视图）

所有页面加上 `Accept: application/json` 就返回原始数据，工具据此省掉了 HTML 解析：

| 方法 | 路径 | 返回要点 |
|---|---|---|
| `GET` | `/` | `UserContext`（JSON 字符串：`_id` / `uname` / `priv`）、`domain`、`UiContext` |
| `GET` | `/login` → `POST /login` | 表单 `uname` / `password` / `rememberme`，成功 200 + `Set-Cookie: sid`，失败 403 |
| `GET` | `/contest?page=N` | `tdocs[]`（`title` / `rule` / `beginAt` / `endAt` / `pids` / `assign`）、`tpcount`（总页数） |
| `GET` | `/contest/<cid>/problems` | `tdoc`、`pdict`（题目标题 / `config` 时限内存 / `tag`）、`tsdoc.endPsdict`（我的提交状态） |
| `GET` | `/p/<pid>?tid=<cid>` | `pdoc.content` 为 `{"zh": "<Markdown>"}"`，另有 `pdoc.data`（测试数据文件列表） |

未登录时这些接口会返回 `200` 且正文形如 `{"url": "/login?redirect=..."}`，工具会把它
识别为「登录状态失效」而不是当成数据解析失败。

---

## 项目结构

```text
nflsoj-archive/
├── pyproject.toml              # 项目元数据、依赖（空）、四个控制台入口
├── uv.lock                     # uv 的锁文件
├── .python-version             # 3.14
├── README.md                   # 本文档
├── LICENSE                     # GPL-3.0
├── src/
│   ├── nflsoj_archive/         # 共享前端 + NFLSOJ 后端
│   │   ├── __init__.py         # 导出 main / __version__
│   │   ├── __main__.py         # python -m nflsoj_archive 入口
│   │   └── nflsoj.py           # HTTP 传输、Console/Progress 渲染、Store、归档渲染、CLI
│   └── hydrooj_archive/        # Hydro 后端（复用上面的前端）
│       ├── __init__.py         # 导出 main / __version__
│       ├── __main__.py         # python -m hydrooj_archive 入口
│       └── hydrooj.py          # Hydro JSON 客户端、数据规整、归档、CLI
└── tests/
    ├── test_nflsoj.py          # 离线单元测试（不访问网络）
    └── test_hydrooj.py         # Hydro 版离线单元测试
```

运行测试（纯离线，`unittest` 标准库即可，共 225 个用例）：

```bash
python -m unittest discover -s tests -v
```

`nflsoj.py` 的代码组织：

| 区块 | 内容 |
|---|---|
| 文本宽度与渲染 | `char_width` / `truncate` / `wrap_display`、`Console`（表格 / 进度条 / 徽标）、`Progress` |
| 传输层 | `HttpTransport`（keep-alive、重试、重定向、gzip）、`Client`（五个 API） |
| 数据规整 | `normalize_contest` / `extract_problems` / `extract_announcements` |
| 存储 | `Store`（config / progress / 题面缓存 / 归档元数据）、`atomic_write_text` |
| 业务 | `sync_contests`、`archive_contests`、`collect_stats`、`render_*_markdown` |
| CLI | `build_parser`（argparse 定义）、`cmd_*` 各命令、`main` |

`hydrooj.py` 的代码组织（**前端部分全部复用 `nflsoj_archive.nflsoj`**）：

| 区块 | 内容 |
|---|---|
| 站点客户端 | `HydroClient`（JSON 视图、Cookie 会话、网关拦截重试、JSON 表单登录） |
| 数据规整 | `_localized`（`{"zh": ...}` 拆包）、`contest_from_tdoc`、`extract_problems`、`problem_statement` |
| 业务 | `fetch_contest_pages`、`sync_contests`、`archive_contests` / `archive_one_contest` |
| 复用 | `Console` / `Progress` / `Store` / `render_*_markdown` / `print_contest_header` / 状态与统计逻辑 |

---

## 贡献

欢迎提交 Issue / Pull Request。请保持以下原则：

- **零第三方依赖**，只用标准库；
- 与现有风格一致：中文注释、`Console` 渲染、`ApiError` 统一错误处理；
- 不要为了「更快」盲目提高并发上限，站点侧压力优先；
- 涉及行为变更（尤其是默认值）时，请同步更新本 README 的「从 1.0 升级」一节。

---

## 许可

本项目基于 **GNU General Public License v3.0（GPL-3.0）** 发布。
你可以自由使用、修改、分发，但衍生作品需以相同许可证开源。

> 本工具为个人学习与效率工具。请合理控制请求频率、遵守 NFLSOJ 站点的使用条款，
> 不要用于批量抓取或给站点造成压力。因使用本工具产生的任何后果由使用者自行承担。
