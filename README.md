# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

适用场景：

- 用 **Codex CLI** 走 `/v1/responses`
- 用 **Claude Code / CC Switch** 走 `/v1/messages`
- 用 **Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI** 走 `/v1/chat/completions`

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

`workbuddy2api` 是一个本地协议转换器。它会读取你已经登录好的 WorkBuddy / CodeBuddy 桌面端凭据，转发到腾讯后端 `copilot.tencent.com`，然后在本地暴露这些接口：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

它不负责登录，不模拟桌面端，也不替你执行工具。它只做三件事：

1. 读取本机登录态并注入鉴权头
2. 在 OpenAI / Anthropic 协议和腾讯后端协议之间转换
3. 对 `Codex CLI` 这类长上下文 agent 请求做后端友好的压缩投影

> 命名说明：项目对外名称现在叫 `workbuddy2api`。代码里仍保留部分历史命名，比如 `codebuddy2openai`、`CODEBUDDY2OPENAI_*`，目的是兼容旧配置和环境变量。
> 另外，GitHub 仓库路径当前也可能仍沿用 `codebuddy2openai`，这是仓库路径与项目展示名尚未完全统一，不影响使用。

### 你能用它做什么

- 把 WorkBuddy 订阅复用到 OpenAI 兼容客户端
- 让 Codex CLI 直接接腾讯后端，而不是只接 OpenAI 官方
- 让 Claude Code 通过 CC Switch 复用 WorkBuddy 支持的模型
- 保留原生 `tools` / `tool_calls` / 流式 SSE / 多轮工具调用

### 当前支持

| 客户端 / 协议 | 接口 | 当前状态 |
|------|------|------|
| OpenAI Chat Completions | `/v1/chat/completions` | 已支持 |
| OpenAI Responses | `/v1/responses` | 已支持，适配 Codex CLI |
| Anthropic Messages | `/v1/messages` | 已支持，适配 Claude Code / CC Switch |
| OpenAI Models | `/v1/models` | 已支持 |
| Health Check | `/health` | 已支持 |

### 管理后台能力总览

| 能力 | 说明 |
|---|---|
| 账号池 | 多账号轮转 / 手动指定、冷却与熔断、会话粘性、在途租约限流 |
| 凭据管理 | 浏览器授权添加、凭据刷新、自动签到与余额查询 |
| **积分任务** | 任务查询 / 接受 / 领奖，13 个任务支持一键完成 |
| **任务中心** | 全账号待办扫描 + 执行队列（账号内串行、账号间并发） |
| **用量统计** | 按账号 / 模型 / 域统计 token 与延迟，分钟级时序 |
| **模型与倍率** | 官方倍率、实测扣费、思考档位、上下文与输出上限 |
| **账号可用模型** | 每账号实际可用的模型清单，支持探测确认 |

---

## 3 分钟上手

### 1. 前置条件

你需要先满足这 3 个条件：

1. 本机已经安装并登录 **WorkBuddy / CodeBuddy** 桌面端
2. 本机有 **Python 3.8+**
3. 已安装依赖 `fastapi`、`uvicorn`、`httpx`

默认会在这些位置寻找登录态：

- macOS: `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
- Windows: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
- Linux: `~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info`

### 2. 安装依赖

推荐用 `uv`：

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
```

也可以用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 注意：无论是启动服务，还是执行 `python3 -m core.converter --help`，都必须先装依赖。

### 3. 启动

最常用的启动方式：

```bash
uv run python -m core.converter --desensitize --log converter.log
```

或：

```bash
python3 -m core.converter --desensitize --log converter.log
```

看到监听 `http://127.0.0.1:8787` 就说明已经起来了。

### 4. 快速自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

如果这两条能通，说明本地服务、登录态、基本路由都没问题。

---

## 客户端接入

### Codex CLI

这是当前最推荐的接法。Codex CLI 走的是 `/v1/responses`，而不是 `/v1/chat/completions`。

推荐启动命令：

```bash
uv run python -m core.converter --desensitize --log converter.log
```

把下面配置合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

设置一个占位环境变量：

```bash
export CODEBUDDY2OPENAI_KEY=any-value
```

启动：

```bash
codex --profile workbuddy "你的任务描述"
```

补充说明：

- 推荐保留 `--desensitize`
- 当前 `/v1/responses` 默认已经会做投影压缩
- 如果你想尽量保留原始 system prompt，可试 `--desensitize --no-compact`
- `--desensitize --no-compact` 下若仍命中审核，当前实现会自动退回紧凑模式重试一次

### Claude Code / CC Switch

Claude Code 不走 OpenAI 协议，而是走 Anthropic Messages。

推荐启动命令：

```bash
uv run python -m core.converter --desensitize --log converter.log
```

在 CC Switch 里配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

注意：

- 模型名必须填写腾讯后端支持的真实模型名
- 不做 Anthropic 模型名到腾讯模型名的自动映射
- Claude Code 场景强烈建议开启 `--desensitize`

### 其他 OpenAI 兼容客户端

适用于：

- Cherry Studio
- ZCode
- LobeChat
- NextChat
- Open WebUI
- 自己写的 OpenAI SDK 客户端

配置方式：

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: 留空，或填你启动时设置的 `--api-key`
- 模型名: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

---

## 常用命令

### 基本启动

```bash
python3 -m core.converter
python3 -m core.converter --desensitize
python3 -m core.converter --desensitize --log converter.log
python3 -m core.converter --api-key mysecret
python3 -m core.converter --port 9000
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 给本地客户端加一层鉴权 |
| `--log` | 无 | 记录请求与响应日志 |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize` 使用，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |

### curl 示例

```bash
curl http://127.0.0.1:8787/v1/models

curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

---

## 日志与排障

### 推荐启动方式

```bash
uv run python -m core.converter --desensitize --log converter.log
```

### 日志里能看到什么

每次请求都会带一个唯一 ID，常见日志包括：

- `REQUEST BODY`
- `RESPONSES → CHAT BODY`
- `RESPONSES PROJECTION`
- `RESPONSE BODY`
- `RESPONSE RAW SSE`
- `⚠️内容审核拦截`

其中 `RESPONSES PROJECTION` 会告诉你：

- 投影前后消息数
- 投影前后字符数
- tool schema 压缩量
- 是否丢掉了 harness 消息
- 是否保留了 anchor user

### 最常见问题

#### 找不到登录文件

说明桌面端没登录，或者登录目录不在默认路径。先确认桌面端已经真正完成登录。

#### 401

分两种：

- 本地 401：你启用了 `--api-key`，但客户端没带同一个 key
- 后端 401：腾讯 token 失效，尝试重新打开桌面端登录

#### 响应慢

先换快一点的模型，比如 `deepseek-v4-flash`。

#### 被“敏感内容”拦截

这是腾讯后端的内容审核，不一定是用户问题本身敏感，很多时候是 agent runtime 文本触发的，比如：

- `DoS`
- `exploit`
- `credential`
- `sandbox`
- `escalation`
- 竞争品牌词
- tool description 中的安全术语

建议排查顺序：

1. 开 `--log`
2. 看同一请求 ID 下的 `REQUEST BODY` 或 `RESPONSES → CHAT BODY`
3. 如果是 Codex CLI，再看 `RESPONSES PROJECTION`
4. 开 `--desensitize`
5. 如果还不稳，再尝试 `--desensitize --no-compact`

---

## 管理后台（可选）

除原转换器外，项目附带一个中文 Web 管理后台（`admin/`），适合服务器长期运行：

- 浏览器授权添加国内 CodeBuddy / WorkBuddy 账号，无需桌面端；多账号自动轮转、冷却与积分耗尽避让
- 凭据刷新、积分余额查询、每日自动签到（默认北京时间 09:00，可配置）
- 客户端 API Key 创建与撤销、真实模型连接测试
- 概览页：请求量、完成成功率、HTTP 成功率、平均耗时与最近 100 条请求
- **积分任务**：查询进度、接受、领取奖励，13 个任务支持一键完成
- **任务中心**：全账号待办扫描 + 执行队列（账号内串行、账号间并发）
- **用量统计**：按账号 / 模型 / 域统计 token 与延迟
- **模型与倍率**：官方倍率、实测扣费、思考档位、上下文与输出上限

本地启动（需先安装依赖并设置至少 20 位的管理密钥）：

```bash
export ADMIN_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(36))")
export CODEBUDDY2OPENAI_KEY=sk-wb-$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
python3 -m admin.server
```

然后访问 `http://127.0.0.1:8787/admin/`。一键 Docker 部署见下文；详细说明见 [ADMIN_README.md](ADMIN_README.md)。

> 管理后台为单进程设计，正式对外部署必须放在 HTTPS 反向代理之后（示例：`deploy/admin/nginx.conf.example`）。直接运行 `python3 -m core.converter` 的原有用法不受影响。

---

### 扩展能力

除账号池与基础运维外，管理后台还包含以下几块能力。

#### 积分任务

「账号池」每个账号行有「任务」按钮，打开任务面板：

- 展示全部任务：标题、进度（current/target）、奖励（积分 / 能量 / Buddy）、状态
- **接受**：报名任务。上游对未接受的任务不计数，故执行前先接受
- **领取**：进度达标后领奖。已领取的重复调用会被上游幂等拦下，不会重复发放
- **一键完成**：对单个任务自动执行行为事件（见下）
- **一键完成可自动任务**：跑完全部可自动化任务，再统一领取奖励

支持一键完成的任务：

| 任务 | 说明 |
|---|---|
| `chat_5` | 补足 5 条对话活跃上报 |
| `first_buddy` | 活跃上报 → 同意协议 → 领取第一只 Buddy |
| `Model_chat_GLM5.2` | 按 GLM-5.2 模型上报对话事件 |
| `RichMeow_Chat` | 桌面端完整对话事件链 |
| `Buddy_App` / `Buddy_App_QQ` | 进入 Buddy 应用事件链 |
| `automation_1` | 定时任务创建事件 |
| `Library_read` | 资料库阅读点击事件 |
| `template_5` | 使用模板事件组 ×5 |
| `playbook_prompt` | 灵感案例发送 Prompt 事件组 |
| `create_canvas` | 设计创意画布创建事件组 |
| `Hp_Appearance` | 设置主题 + 皮肤生效事件 |
| `black_cat` | 夜猫子：仅在 23:00–08:00 窗口内计数 |

> 任务进度由上游的行为事件点亮，而非独立接口。事件按三种客户端指纹分别构造
> （CLI / 桌面端 / Web），事件形状与真实客户端一致。
> 动作幂等：已领取或已达标的任务直接跳过，不重复消耗配额。

#### 任务中心

「任务中心」视图解决「不知道哪些账号有哪些任务没做」：

- **扫描待办**：并发拉取全部账号的任务列表，汇总「未完成且可自动化」的待办项（只读）
- **执行队列**：把待办按账号分组排队执行。账号内串行（与单任务、一键完成互斥），
  账号间并发（1–4 可配，默认 1）
- 队列进度实时轮询，逐项显示状态与结果

#### 用量统计

「用量」视图按三个维度统计 token 消耗与请求表现：

- **总量**：请求数、总 token、prompt / completion、失败尝试、平均延迟
- **按账号**：请求 / 失败 / prompt / completion / 合计 / 均延迟 / 均速率
- **按模型**：请求 / 失败 / token / 扣费
- **按域**：国内 / 国际分别统计
- **Token 时序**：分钟级 prompt 与 completion 曲线

统计仅覆盖经过网关的请求，数据随 `state.json` 持久化。

#### 模型与倍率

- **官方倍率**：来自上游真实目录（`credits` 字段），含限时免费促销
- **实测倍率**：本机真实请求累计的 `credit ÷ tokens`，与官方值对照
- **模型档位**：每个模型支持的思考档位（如 `low / high / max`，可关思考时附 `off`）
- **上下文**：上限与默认值（如「1,000,000，300K 默认」）
- **最大输出**、图像 / 工具能力支持情况
- 目录外模型单列标注：可正常调用但上游目录未列出

档位与能力信息取自上游目录；上游缺失时用 models.dev 补齐（按模型名匹配、
取出现最多的档位组合），缓存 24 小时，网络不可用时沿用旧缓存。

#### 各账号可用模型

按账号展示它**实际能用**的模型：该账号所在区域的上游目录 + 实测可用，
再剔除实测不可用的。「探测」按钮可确认未确定的模型（每次消耗极少额度）。

---

## Docker 部署

如果你更习惯用 Docker，可以直接用。

前提是把宿主机登录态目录挂进去，因为容器里拿不到桌面端 auth 文件。

### 一键部署（推荐，含管理后台）

一条命令完成密钥生成、镜像构建与启动：

```bash
bash deploy/one-click/deploy.sh
```

脚本会自动生成 `ADMIN_KEY` 与客户端 Key（保存在 `deploy/one-click/.env`，仅首次显示），随后访问 `http://127.0.0.1:8787/admin/`。改端口：编辑 `deploy/one-click/.env` 里的 `PORT` 后重跑脚本。

### docker compose（独立转换器）

先改 `deploy/standalone/docker-compose.yml` 里的 auth 挂载路径，再执行：

```bash
docker compose -f deploy/standalone/docker-compose.yml up -d --build
```

### docker run（独立转换器）

```bash
docker build -t workbuddy2api -f deploy/standalone/Dockerfile .

docker run -d --name workbuddy2api -p 8787:8787 \
  -v ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth:/data/auth:ro \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  workbuddy2api
```

### 相关环境变量

| 变量 | 说明 |
|------|------|
| `CODEBUDDY_AUTH_DIR` | 指定登录态目录 |
| `CODEBUDDY2OPENAI_KEY` | 本地 API Key |
| `CODEBUDDY2OPENAI_LOG` | 日志路径 |

---

## 模型列表

当前内置默认模型列表：

`hy3`、`hy4-preview`、`kimi-k3`、`kimi-k2.8-preview`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`deepseek-v4-pro`、`deepseek-v4.1-flash`、`deepseek-v4-flash`、`minimax-m3-pay`、`hy3-preview-agent`、`auto`

> 本机装有 WorkBuddy 时优先使用其动态模型目录，上表为兜底列表。具体能不能用，取决于你的 WorkBuddy / CodeBuddy 订阅与上游状态。

---

## 项目结构

```text
workbuddy2api/
├── core/                      # 协议转换核心
│   ├── converter.py           # 主入口，FastAPI 服务（python3 -m core.converter）
│   ├── responses_adapter.py   # OpenAI Responses ↔ Chat 适配
│   ├── responses_projection.py# Codex / agent 请求投影压缩
│   ├── anthropic_adapter.py   # Anthropic Messages ↔ Chat 适配
│   └── desensitize.py         # 运行时文本压缩与零宽脱敏
├── admin/                     # 中文管理后台（可选，python3 -m admin.server）
│   ├── server.py              # 管理 API、会话与静态页面
│   ├── pool.py                # 账号池轮转、冷却、积分与签到
│   ├── browser_login.py       # 浏览器授权登录
│   ├── metrics.py             # 请求统计
│   ├── rates.py               # 模型倍率、目录与账号可用性
│   ├── modelmeta.py           # models.dev 元数据兜底（档位 / 上下文）
│   ├── usage.py               # 按账号 / 模型 / 域的用量聚合
│   ├── tasks.py               # 积分任务：列表 / 接受 / 领奖
│   ├── events.py              # 行为事件上报（CLI / 桌面 / Web 三指纹）
│   ├── autotask.py            # 任务一键完成动作
│   ├── taskcenter.py          # 全账号扫描 + 执行队列
│   └── static/                # 前端资源
├── deploy/                    # 部署
│   ├── standalone/            # 独立转换器（Dockerfile + compose）
│   ├── admin/                 # 管理后台（Dockerfile + compose + nginx 示例）
│   └── one-click/             # 一键式部署（deploy.sh 自动生成密钥并启动）
├── tests/                     # 单元测试（pytest）
├── codex-codebuddy.example.toml
├── README.md
└── LICENSE
```

运行测试：

```bash
python3 -m pytest tests/
```

---

## 致谢

本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。

## 免责声明

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

## 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
## English

`workbuddy2api` exposes your already logged-in **WorkBuddy / CodeBuddy** desktop session as local **OpenAI- and Anthropic-compatible APIs**.

Supported endpoints:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

Recommended use cases:

- **Codex CLI** via `/v1/responses`
- **Claude Code / CC Switch** via `/v1/messages`
- **Cherry Studio / ZCode / LobeChat / Open WebUI** via `/v1/chat/completions`

### Quick Start

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
uv run python -m core.converter --desensitize --log converter.log
```

Then verify:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### Codex CLI

Use `/v1/responses` and keep `--desensitize` enabled.

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

Run:

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

### Claude Code / CC Switch

Use `/v1/messages`:

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### Notes

- `--desensitize` is recommended for both Codex CLI and Claude Code
- `/v1/responses` already applies backend-facing projection by default
- `--desensitize --no-compact` preserves more of the original system prompt
- if that still gets review-blocked, `/v1/responses` will retry once in compact mode
- an optional Chinese web admin console (`python3 -m admin.server`, see [ADMIN_README.md](ADMIN_README.md)) adds browser-based account authorization, multi-account rotation, client API keys, daily check-in and request metrics; one-command Docker setup: `bash deploy/one-click/deploy.sh`

### CLI Options

```bash
python3 -m core.converter [--host HOST] [--port PORT] [--api-key KEY] [--log PATH] [--desensitize] [--skip-check]
```

### Disclaimer

For personal learning and research only. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic.

---

<sub>
Keywords: codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>
