# 部署说明（技术文档）

本目录是本项目的**本地部署配置目录**，用于在 Windows 上把 `workbuddy2api`
（把 WorkBuddy / CodeBuddy 桌面端登录态转成 OpenAI / Anthropic 兼容 API）跑起来。

- 仓库源码：`D:\item\codebuddy2api`
- 部署配置：`D:\item\codebuddy2api\deploy-local`（本目录）
- 监听地址：`http://127.0.0.1:8787`

> **只想用起来？** 看 [`使用说明.md`](使用说明.md)，那里是操作手册。
> 本文件是技术文档：部署细节、设计取舍、排障。

---

## 一、当前部署状态

| 项目 | 值 |
|------|-----|
| Python | 3.12.10（虚拟环境 `.venv`） |
| 依赖 | fastapi / uvicorn / httpx（已安装） |
| 单元测试 | 99 passed |
| 运行模式 | **管理后台 + API（单进程）** |
| 服务地址 | `http://127.0.0.1:8787` |
| 管理界面 | `http://127.0.0.1:8787/admin/` |
| 登录态目录 | `deploy-local\auth` |
| 管理数据 | `deploy-local\management` |
| 已导入账号 | `13557879180`（.cn，约 4927 积分）、`国际版 zeraphyms`（.ai，约 348 积分） |
| 管理密钥 / 客户端 Key | 见 `deploy-local\.env` |
| 源码补丁 | `deploy-local\intl-support.patch`（国际版支持 + 模型倍率） |

已验证可用：`/health`、`/v1/models`、`/v1/chat/completions`（含流式）、
`/v1/responses`、`/v1/messages`、管理后台登录与 `/admin/api/overview`、
`/admin/api/models`（模型倍率），以及**国内 + 国际两种账号**的真实调用、
积分查询与倍率统计。

---

## 二、两种运行模式（先看这里）

项目有**两个不同的启动入口**，功能不同，**同一端口只能跑其中一个**：

| 模式 | 启动方式 | 管理后台 | 多账号池 | `/v1/*` 鉴权 |
|------|----------|----------|----------|--------------|
| **独立转换器** | `start.bat` / `start.ps1` | ❌ 无（访问 `/admin/` 会 404） | ❌ 单账号 | 默认不校验 |
| **管理后台 + API** | `start-admin.bat` / `start-admin.ps1` | ✅ 有 | ✅ 轮转/冷却/签到 | **必须带客户端 Key** |

> 源仓库 README 里说“访问 `http://127.0.0.1:8787/admin/`”指的是**管理后台模式**。
> 如果之前用 `start.bat` 启动，`/admin/` 必然 404 —— 这不是故障，是入口选错了。
>
> 管理后台模式会挂载同一个转换器，所以 `/v1/*` 接口在两种模式下都可用；
> 区别是后台模式**强制**校验客户端 Key。

### 启动 / 停止 / 自检

```powershell
# 【推荐】管理后台 + API
D:\item\codebuddy2api\deploy-local\start-admin.bat
powershell -ExecutionPolicy Bypass -File D:\item\codebuddy2api\deploy-local\start-admin.ps1

# 仅独立转换器（无管理后台，不校验 API Key）
D:\item\codebuddy2api\deploy-local\start.bat

# 停止（按端口结束进程，两种模式通用）
powershell -ExecutionPolicy Bypass -File D:\item\codebuddy2api\deploy-local\stop.ps1

# 自检（自动识别当前模式）
powershell -ExecutionPolicy Bypass -File D:\item\codebuddy2api\deploy-local\check.ps1
```

常用参数：

```powershell
...\start-admin.ps1 -Port 9000     # 换端口
...\start-admin.ps1 -Force         # 端口被占用时先停旧进程再启动
```

`start.bat` / `start-admin.ps1` 都会先检查端口占用；被占用时打印占用进程 PID
并给出处理办法，不会静默失败。

---

## 三、登录管理后台

1. 用 `start-admin.bat` 启动（启动日志里会打印管理密钥和管理界面地址）。
2. 浏览器打开 <http://127.0.0.1:8787/admin/>。
3. 输入**管理密钥**（`deploy-local\.env` 里的 `ADMIN_KEY`）。

> `127.0.0.1` 属于浏览器认可的“安全上下文”，因此后台默认的
> `Secure` Cookie 在本地 HTTP 下可正常保存，无需额外配置。

### 管理后台能做什么

- **概览**：账号、总积分、请求量、完成/HTTP 成功率、平均耗时、最近 100 条请求
- **账号管理**：导入桌面端 `.info` 凭据；浏览器授权登录新账号（无需桌面端）
  —— 添加时可选**账号区域**：国内版 / 国际版
- **账号池**：多账号自动轮转；暂停 / 冷却 / 积分耗尽自动避让
- **凭据维护**：手动刷新 token、查询剩余积分、单账号与批量签到
  （每日自动签到，默认北京时间 09:00）
- **客户端 Key**：新建 / 撤销 API Key；`.env` 初始 Key 可在「接入指南」页直接复制
- **接入指南**：一页显示 Base URL（OpenAI / Anthropic）+ API Key，均可一键复制
- **模型与倍率**：查看每个模型的**官方倍率**、**限时免费促销**、上下文/最大输出，
  以及本机**实测扣费**（详见第五节）
- **模型测试**：真实调用上游验证账号可用性

> 管理后台是**单进程**设计，不要用多 worker 启动。
> 若要对外提供服务，必须放在 HTTPS 反向代理之后（见 `deploy/admin/nginx.conf.example`）。

---

## 四、国际版支持（已打补丁）

原项目**只支持国内账号**，把后端网关写死为 `copilot.tencent.com`，国际版
（`workbuddy.ai` / `codebuddy.ai`）凭据打过去一律 401。

本部署已打补丁 `intl-support.patch`，**国内与国际账号现在都能用**，按凭据里的
`domain` 自动选择网关：

| 凭据 domain | 对话/刷新网关 | 积分/签到服务 |
|-------------|---------------|----------------|
| `www.workbuddy.cn` / `www.codebuddy.cn` | `copilot.tencent.com` | `www.codebuddy.cn` |
| `www.workbuddy.ai` / `www.codebuddy.ai` | `www.workbuddy.ai` | `www.workbuddy.ai` |

补丁改动的文件：

| 文件 | 改动 |
|------|------|
| `core/converter.py` | 新增 `is_intl_domain` / `backend_for` / `billing_base_for` / `ensure_system_first`；4 处请求按 `X-Domain` 路由；token 刷新按 domain 选网关 |
| `admin/pool.py` | 积分/签到按 `X-Domain` 选服务地址；移除「仅支持国内账号」限制；请求级区域 ContextVar |
| `admin/browser_login.py` | 新增 `cn` / `intl` 两套授权网关与来源站点；移除国际账号拒绝 |
| `admin/server.py` | `/admin/api/oauth/start` 接受 `region`；新增 `/admin/api/models` 等倍率接口 |
| `admin/rates.py` | **新增**：上游模型目录 + 促销解析 + 实测扣费累计 |
| `admin/metrics.py` | 从 SSE / JSON 响应中提取 model 与 usage，累计 credit |
| `admin/static/index.html` | 添加「账号区域」下拉框与「模型与倍率」页 |
| `admin/static/app.js` | 提交授权带 `region`；渲染模型倍率表 |
| `admin/static/style.css` | 模型页样式 |

### 国际后端的额外要求

国际后端有一条国内没有的硬性校验：

```
code 11128: "first message is not system prompt"
```

即**首条消息必须是 system**。而项目的脱敏逻辑会把「You are a helpful assistant」
这类声明整句删掉，删空后就触发 400。补丁用 `ensure_system_first()` 在处理链
**之后**补一条中性占位 system，避开这个坑。

> 如果你自己改 system prompt，注意别用「You are …」+ 厂商/模型名的句式，
> 那种句子会被 `filter_system_identity` 整句删除。

### 维护与回滚

```powershell
# 查看补丁内容
git -C D:\item\codebuddy2api diff

# 回滚到官方原版
cd D:\item\codebuddy2api
git checkout -- core/converter.py admin/pool.py admin/browser_login.py admin/server.py admin/static/app.js admin/static/index.html

# 上游更新后重新应用
git apply deploy-local\intl-support.patch
```

> 补丁针对 commit `1366be7` 生成。`git pull` 后若冲突，以官方改动为准重新应用。
> 补丁与源码改动是**上游仓库文件**，不在 `deploy-local` 的 git 忽略范围内。

---

## 五、模型列表按账号返回（重要）

`/v1/models` 和后台「连接测试」的下拉，返回**当前账号实际能用的模型**：

| 账号池模式 | 返回内容 |
|-----------|---------|
| **手动指定账号** | **只有该账号**能用的模型 |
| 轮转 | 所有参与轮转账号的并集（因为无法预知请求走哪个号） |

> **这是最容易误解的一点**：如果账号池处于**轮转模式**，`/v1/models`
> 会返回并集（例如 43 个），看起来"没有按账号过滤"。
> 想让列表只反映某一个账号，必须切到**手动指定账号**模式
> （后台点账号行的「切换为此账号」会自动切换模式并选中）。

实测效果（手动模式）：

| 账号 | 模型数 | deepseek |
|------|-------|----------|
| 国内 `13557879180` | 30 | ✅ 4 个 |
| 国际 `zeraphyms` | 21 | ✅ 1 个（`deepseek-v4.1-flash`） |

### 每个账号的模型怎么算

```
账号可用模型 = 该账号所在区域的上游目录
             + 实测确认可用的模型（可能在目录外）
             − 实测确认不可用的模型（上游返回 11102）
```

上游用 **`11102 model service info not found`** 表示「这个账号没有这个模型」，
这是一个精确的按账号信号。系统据此自动学习：

- **真实请求成功** → 记为该账号可用（国际账号调 `deepseek-v4.1-flash`
  成功后，它会自动出现在列表里）
- **真实请求返回 11102** → 记为该账号不可用，从列表移除
- **其它错误**（429 限流、超时）→ **不下结论**，保持原状

### 主动探测

「模型与倍率」页新增「各账号可用模型」区块，每个账号旁有
**「探测未确认模型」**按钮。点一下会按 `max_tokens=1` 逐个试探未确认的模型，
每次消耗极少额度（默认每批 8 个，避免触发限流）。

探测结果分三种：**可用** / **不可用**（明确 11102）/ **未判定**（限流等，
下次再探）。这个按钮能让国际账号把 `deepseek-v4.1-flash` 这类
目录外的可用模型找出来。

### 为什么不能只信上游目录

原项目 `get_available_models()` 返回**写死的 18 个模型**，不看账号是谁。

我第一版修复改成「按区域只返回该区域目录」，**那是错的**——上游目录不完整：

| 模型 | 在国际目录里 | 国际账号实测 |
|------|------------|------------|
| `deepseek-v4.1-flash` | ❌ 没有 | ✅ **200 可用** |
| `glm-5.1` | ❌ 没有 | ✅ **200 可用** |
| `kimi-k2.7` | ❌ 没有 | ✅ **200 可用** |
| `deepseek-v4-pro` | ❌ 没有 | ❌ 11102 |
| `deepseek-v4-flash` | ❌ 没有 | ❌ 11102 |

上游还把目录拆成主文件 + `include` 分片（`mergeStrategy: "merge"`），
本工具只读了主文件，所以国际目录只有 18 个。

**现在的做法**：目录只当**候选池**，真实可用性由实测学习 + 主动探测确定。
这样既不会漏掉能用的（国际的 DP），也不会多列不能用的（国际的 DP-Pro）。

### 目录刷新

服务启动时拉一次两个区域的目录，之后每小时刷新。
也可在「模型与倍率」页点「刷新上游目录」立即更新。

---

## 六、模型与倍率（新增功能）

管理后台「模型与倍率」页展示每个模型的计费倍率。数据来自**上游真实接口**：

```
GET {网关}/v2/enterprises/personal/models
```

该接口返回 `data.models[].credits`（如 `"x0.79 credits"`）与
`data.modelPromotions`（限时免费促销，含 `factor: 0` 和有效期）。

### 为什么需要「实际生效」这一列

上游目录里的静态倍率**会滞后于真实计费**。实测对比：

| 区域 | 模型 | 目录标称 | 实际扣费 |
|------|------|---------|---------|
| 国内 | `hy4-preview` | x0.29 | **0（免费）** |
| 国内 | `hy3` | x0.00 | 0（免费） |
| 国际 | `deepseek-v4.1-flash` | 目录中**根本没有** | **0（免费）** |

所以页面同时给出两列：

- **官方倍率** —— 上游目录的标称值
- **实际生效** —— 叠加促销后的值；促销生效时显示「免费」并标注 `Free now`

### 「目录外」模型

有些模型**可以正常调用、且不扣费**，但上游目录里没有列出来
（典型：国际版的 `deepseek` 系列）。这类模型标为「目录外」，没有官方倍率，
只有实测值。这解释了「别的项目获取不到国际服 deepseek 倍率」的现象 ——
不是它读错了，而是上游目录里确实没有。

### 实测倍率

每次真实请求都会累计上游 `usage.credit` 与 token 数，算出
`credit ÷ tokens × 1000` 作为实测倍率，用于和官方值对照。

> **精度说明**：上游 `credit` 只有 **0.01** 的精度。样本 token 太少时，
> 单次量化误差就能让结果偏离数倍。因此累计不足 2000 tokens 时不显示实测值
> （页面上显示为 `—`）。想要可靠的实测值，请用较长输出多跑几次。

统计持久化在 `management/state.json` 的 `model_usage` 字段，重启不丢；
「清零实测统计」按钮可重置。

### 刷新

目录缓存 1 小时。点「刷新上游目录」立即重新拉取两个区域的目录。
上游调价或促销起止时，以刷新后的结果为准。

---

## 七、登录态目录说明

本机 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\` 下有**两个**账号文件：

| 文件 | 账号 | 域名 | 打补丁后 |
|------|------|------|----------|
| `workbuddy-desktop-ai.info` | zeraphyms | www.workbuddy.ai | **可用**（走 .ai 网关） |
| `workbuddy-desktop.info` | 13557879180 | www.workbuddy.cn | 可用（走 .cn 网关） |

> 打补丁**前**，程序按文件名排序取第一个 `.info`（即 `-ai.info`），导致 401；
> 因此当时用 `auth\` 子目录只放国内账号。打补丁**后**两个账号都可用，
> 这个限制已不再是必需的，保留 `auth\` 只是为了固定选用哪个账号。

`CODEBUDDY_AUTH_DIR` 环境变量指向 `auth\`，启动脚本已自动设置。

> 登录态会随桌面端使用自动刷新；若日后失效，请重新复制文件覆盖。

---

## 八、客户端接入

### API Key 在哪？

管理后台模式下 `/v1/*` **强制校验客户端 Key**。Key 有三个来源，任选其一：

| 来源 | 位置 | 能否再次查看 |
|------|------|-------------|
| **启动配置的初始 Key** | `.env` 的 `CODEBUDDY2OPENAI_KEY` | ✅ 可（本部署已开启，见下） |
| 后台新建的 Key | 「API 密钥」页 → 创建密钥 | ⚠️ 仅创建时显示一次 |
| 脚本替换的 Key | `deploy-local\set-key.ps1` | ⚠️ 脚本会打印一次 |

**最简单的方式**：打开后台 <http://127.0.0.1:8787/admin/> → 左侧「接入指南」，
页面上直接显示 **API Key** 和两个 Base URL，点「复制」即可。

「API 密钥」页也会把可还原的那个 Key 完整显示出来，带复制按钮。

### `CODEBUDDY2OPENAI_KEY` 是必须的吗

**不是。** 实测五种场景：

| 场景 | 结果 |
|------|------|
| 首次启动 + 有该 Key | 该 Key 写入 `state.json`，可用 |
| 首次启动 + 无该 Key | **服务正常启动**，但没有任何客户端 Key，`/v1/*` 全返回 401，需去后台新建 |
| 已有 `state.json` + 改该 Key | **新值被忽略**，旧 Key 仍有效 |
| 已有 `state.json` + 清空该 Key | 旧 Key 仍有效 |
| `ADMIN_KEY` 为空 | **启动失败**（`must contain at least 20 characters`） |

关键代码在 `Store.__init__`：`if initial_key: self.add_key(...)` 位于
`else` 分支内，**只在首次初始化时执行**。

所以：**必须存在的是 `ADMIN_KEY`，不是 `CODEBUDDY2OPENAI_KEY`。**

### 怎么改客户端 Key

- **首次部署**：改 `.env` 的 `CODEBUDDY2OPENAI_KEY`
- **已有 `state.json`**：`.env` 无效，用 `set-key.ps1` 或后台新建

```powershell
deploy-local\set-key.ps1            # 自动生成并替换
deploy-local\set-key.ps1 -List      # 查看现有 Key
deploy-local\set-key.ps1 -KeepOld   # 新增并保留旧的
```

脚本会备份 `state.json`，改完需重启服务。

> **为什么有的 Key 看不到？**
> 这是上游的安全设计：除创建时的一次性响应外，服务端只保存密钥的
> SHA-256 摘要，**数学上无法还原**（`ADMIN_README.md` 明确写了这一点，
> 并有测试守护）。
>
> 本部署新增了开关 `ADMIN_REVEAL_INITIAL_KEY=1`（在 `.env` 里），
> 它**只让 `.env` 里那一个初始 Key** 可在后台显示——那个值本来就以明文
> 存在 `.env` 中，显示它不降低安全性。**后台新建的 Key 依然无法查看。**

### Codex CLI（走 `/v1/responses`）

合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "deepseek-v4.1-flash"
model_provider = "workbuddy"
```

```powershell
# 填后台「接入指南」页显示的 Key（即 .env 里的 CODEBUDDY2OPENAI_KEY）
$env:CODEBUDDY2OPENAI_KEY="sk-wb-..."
codex --profile workbuddy "你的任务"
```

### Claude Code / CC Switch（走 `/v1/messages`）

```json
{
  "WorkBuddy": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "sk-wb-...",
    "model": "deepseek-v4.1-flash"
  }
}
```

### 其他 OpenAI 兼容客户端（Cherry Studio / LobeChat / NextChat 等）

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：后台「接入指南」页显示的 Key（后台模式必填）
- 模型名：见下方模型列表

---

## 九、自检命令

```powershell
# 健康检查
curl.exe http://127.0.0.1:8787/health

# 模型列表（管理后台模式需带 Key）
curl.exe http://127.0.0.1:8787/v1/models -H "Authorization: Bearer sk-wb-..."

# 普通对话
curl.exe http://127.0.0.1:8787/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer sk-wb-..." ^
  -d "{\"model\":\"deepseek-v4.1-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

或直接运行本目录的 `check.ps1` 一键自检（会自动读取 `.env` 里的 Key）。

---

## 十、可用模型

模型列表**动态来自上游**，随账号区域变化，以 `/v1/models` 或后台
「模型与倍率」页显示为准。下面是各区域的实测快照（2026-09）：

模型列表**按账号返回**（见第五节）。下面是上游两个目录的内容
（2026-09 实测快照），实际以 `/v1/models` 或后台「模型与倍率」页为准。

**国内目录（.cn，约 30 个）**

`auto`、`default`、`hy3`、`hy3-x`、`hy4-preview`、`hy4-preview-x`、
`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5.0`、`glm-4.7`、
`glm-4.6`、`glm-4.6v`、`glm-5v-turbo`、`kimi-k3-1`、`kimi-k2.8-preview`、
`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`kimi-k2-thinking`、
`deepseek-v4-pro`、`deepseek-v4.1-flash`、`deepseek-v4-flash`、`deepseek-v3-2-volc`、
`minimax-m3`、`minimax-m2.5`、`hunyuan-2.0-thinking`、`hunyuan-chat`、`hunyuan-image-alpha`

**国际目录（.ai，约 18 个）**

`default-model`、`fast-model`、`balanced-model`、`primary-model`、`deep-model`、
`hy3`、`hy4-preview`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、
`gpt-5.5`、`gpt-5.4`、`gpt-5.3-codex`、`gemini-3.5-flash`、
`glm-5.3`、`glm-5.2`、`kimi-k3`、`kimi-k2.6`

> **注意**：目录不等于可用性。国际账号实测可调用 `deepseek-v4.1-flash`、
> `glm-5.1`、`kimi-k2.7`（都不在国际目录里），而 `deepseek-v4-pro`、
> `deepseek-v4-flash` 会返回 `11102`。以实测为准，上游随时可能调整。

---

## 十一、排障

| 现象 | 原因与处理 |
|------|-----------|
| **`/admin/` 返回 404** | 用的是独立转换器模式。管理后台需用 `start-admin.bat` 启动 |
| **登录后仍跳回登录页** | 浏览器需能保存 Cookie。`127.0.0.1` 属安全上下文，正常可用；若用局域网 IP 访问则需 HTTPS 反代 |
| **`/v1/*` 返回 401 `invalid api key`** | 管理后台模式强制校验客户端 Key，请带上「接入指南」页显示的 Key |
| **看不到完整 Key / 复制不了** | 上游设计只存 SHA-256，仅创建时显示一次。本部署可用 `.env` 的 `ADMIN_REVEAL_INITIAL_KEY=1` 显示初始 Key；后台新建的 Key 仍无法查看，需要时重新建一个 |
| **后台报 `ADMIN_KEY must contain at least 20 characters`** | `.env` 里 `ADMIN_KEY` 缺失或过短 |
| 端口被占用 | `start.bat` / `start-admin.ps1` 会自动检测并打印占用 PID；用 `stop.ps1` 停掉旧服务，或换端口 `-Port 9000` |
| 双击 `start.bat` 闪退 / 报 `'tart.bat' is not recognized` | 本机控制台代码页为 **936(GBK)**，批处理必须存为 GBK 编码。本目录的 `.bat` 已按 GBK 保存，请勿用 UTF-8 另存 |
| 上游 `401 Authorization Required` | 凭据 domain 与后端网关不匹配。打补丁后已自动路由；若仍出现，确认补丁已应用（`git diff` 应看到 `core/converter.py` 改动） |
| 上游 `11128 first message is not system prompt` | 国际后端要求首条消息是 system。补丁已自动补占位；若自己改 system prompt，避开「You are …」+ 厂商名句式（会被脱敏整句删除） |
| 上游 `11101 Non-stream chat request is currently not supported` | 后端只支持流式；本项目内部已强制走流式，直接请求后端才会出现 |
| 国际账号积分显示 `None` | 在后台点「刷新状态」触发一次查询；国际账号走 `.ai` 积分服务 |
| 倍率页「实测倍率」显示 `—` | 该模型累计 token 不足 2000（上游 credit 仅 0.01 精度，样本太少会失真）。用较长输出多跑几次即可 |
| 倍率页某模型标「目录外」 | 可正常调用，但上游目录未列出（如国际版 deepseek 系列），因此没有官方倍率 |
| **国际账号看不到 deepseek** | 在「模型与倍率」→「各账号可用模型」点**「探测未确认模型」**，或直接调用一次该模型（成功即自动记住）。见第五节 |
| **选某个模型报 `11102 model service info not found`** | 该模型当前账号没有。系统会自动记住并把它从该账号列表移除 |
| **列表里模型数比预期少** | 该账号还没探测过目录外的模型。点「探测未确认模型」补齐 |
| **报 429 `too many requests`** | 上游限流。账号池会自动冷却该账号（5 分钟）；也可在账号行点「刷新凭据」立即解除。探测时也会遇到，此时结果标「未判定」 |
| 目录拉取失败 | 该区域没有可用账号，或账号凭据失效。修好账号后点「刷新上游目录」 |
| 被“敏感内容”拦截 | 腾讯后端内容审核；管理后台模式已强制开启脱敏 |
| 响应慢 | 换 `deepseek-v4-flash` 等更快模型 |
| 找不到登录文件 | 桌面端未登录，或登录目录不在默认路径 |
| 后台账号显示积分耗尽 / 冷却 | 账号池自动避让；在后台「账号」处查看状态或手动刷新 |

日志文件：`D:\item\codebuddy2api\converter.log`

---

## 十二、文件说明

| 文件 | 说明 |
|------|------|
| `start-admin.bat` / `start-admin.ps1` | **管理后台 + API** 启动（推荐） |
| `start.bat` / `start.ps1` | 仅独立转换器启动（无后台） |
| `stop.ps1` | 按端口结束进程 |
| `check.ps1` | 一键自检（自动识别当前模式） |
| `.env` | 管理密钥与客户端 Key（**勿外传**） |
| `auth/` | 登录凭据（**勿外传**） |
| `management/` | 后台状态：账号索引、Key 摘要（**勿外传**） |

以上敏感文件已加入 `.gitignore`。

---

## 十三、免责声明

本项目仅用于个人学习与研究，与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic
无官方关联。请仅在你合法拥有订阅的前提下使用。
