# 上下文压缩参数开关（预留，默认未启用）

> **状态：已实现但未开启。当前行为与改动前完全一致。**
> 写这份说明是为了以后真需要时能立刻想起来怎么用，不用再翻代码。

---

## 这是什么

`core/responses_projection.py` 负责把 Codex CLI 发来的长对话历史压缩后再转发给上游。
压缩力度由 9 个上限常量控制。这些常量现在可以**用环境变量覆盖**，但默认值
就是原来的数字，所以不设环境变量时行为不变。

也就是说：旋钮装好了，但没人拧过。

## 为什么会有这个改动

背景是 Codex CLI 会把大量运行时提示、完整工具 schema、长历史都塞进请求，
导致请求体膨胀。在此之前遇到过「prompt is too long: 1049986 tokens > 1048576
maximum」这类报错。当时把上限改成可配置，方便随时调，不必改代码。

## 可用参数与默认值

| 环境变量 | 默认值 | 作用 |
|----------|--------|------|
| `WB2A_MAX_SYSTEM_GUIDANCE_CHARS` | `1200` | 系统提示（含运行时指导）的长度上限 |
| `WB2A_MAX_USER_CHARS` | `3200` | 单条用户消息的长度上限 |
| `WB2A_MAX_ASSISTANT_CHARS` | `1800` | 单条助手消息的长度上限 |
| `WB2A_MAX_TOOL_OUTPUT_CHARS` | `1600` | 单条工具输出的长度上限 |
| `WB2A_MAX_TOOL_ARGS_CHARS` | `900` | 工具调用参数的长度上限 |
| `WB2A_MAX_HISTORY_SUMMARY_CHARS` | `2200` | 历史摘要的字符上限 |
| `WB2A_MAX_HISTORY_ITEMS` | `10` | 历史摘要保留的条目数 |
| `WB2A_MAX_TAIL_MESSAGES` | `8` | 尾部保留的完整消息条数 |
| `WB2A_MAX_TAIL_CHARS` | `7000` | 尾部保留内容的总字符上限 |

## 怎么开启

在 `deploy-local\.env` 里按需覆盖（只写想改的那几个即可），然后重启服务：

```ini
# 例：放宽用户消息与尾部保留，缓解长对话被压得太狠
WB2A_MAX_USER_CHARS=8000
WB2A_MAX_TAIL_CHARS=16000
```

```powershell
powershell -ExecutionPolicy Bypass -File deploy-local\start-admin.ps1 -Force
```

改小会让压缩更激进（省 token，但更容易丢上下文）；改大则相反。调完建议用
`/v1/responses` 实跑一轮长对话确认效果。

## 注意事项

- 这些上限只管**压缩投影**，与 `WB2A_MAX_REQUEST_MB`（请求体读取上限，默认
  256 MiB，见 `ADMIN_README.md`）不是一回事，别搞混。
- 参数在模块导入时读取一次，改了必须**重启服务**才生效。
- 数值必须是正整数；写错会导致启动时报错。
- 目前没有任何地方设置过这些变量，所以线上跑的就是默认值。

