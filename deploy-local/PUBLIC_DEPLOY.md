# 对外访问部署说明（Cloudflare Tunnel + ikunling.com）

把本机的管理后台/API 通过 **Cloudflare Tunnel** 暴露到 `https://ikunling.com` 的某个子域，
让其他设备直接填 URL 就能用，同时**不把端口暴露到公网**。

> **默认不启用。** 不加 `-Public` 时，一切行为与原来完全一致。
> 只有显式运行 `start-public.bat` 才会进入对外模式。

---

## 一、对外模式和普通模式的区别

| | 普通模式（`start-admin.bat`） | 对外模式（`start-public.bat`） |
|---|---|---|
| 监听地址 | `127.0.0.1:<端口>` | `127.0.0.1:<端口>`（不变） |
| 管理后台 `/admin/` | 本机可访问 | **仅本机**，外部返回 403 |
| API `/v1/*` | 本机可访问 | 本机 + 隧道外部均可，仍需客户端 Key |
| Cookie Secure | 按请求协议自动判断 | 强制 Secure（隧道对外是 HTTPS） |

服务本身**始终只监听 127.0.0.1**，公网流量全部由 cloudflared 隧道转入，
所以路由器不需要做端口转发，也不需要公网 IP。

## 二、准备

1. 域名 `ikunling.com` 的 DNS 托管在 Cloudflare（NS 已指向 Cloudflare）
2. 本机装有 Python 和项目依赖（首次启动会自动创建 `.venv`）
3. 下载 `cloudflared`：<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>
   Windows 版是一个 `cloudflared.exe`，放到任意目录并加入 PATH，或直接用完整路径

## 三、创建隧道（在 Cloudflare 控制台）

1. 打开 <https://one.dash.cloudflare.com/> → **Networks** → **Tunnels** → **Create a tunnel**
2. 选 **Cloudflared**，起个名字（例如 `buddy2api`）
3. 页面会给出一条安装命令，形如：

   ```
   cloudflared.exe service install <一长串令牌>
   ```

   把令牌复制下来备用（**这是敏感信息，不要外传**）。

4. 在该隧道的 **Public Hostname** 里点 **Add a public hostname**：

   | 字段 | 值 |
   |---|---|
   | Subdomain | `api` |
   | Domain | `ikunling.com` |
   | Type | `HTTP` |
   | URL | `127.0.0.1:8787` |

   保存后，`https://api.ikunling.com` 就指向本机 8787 端口。

> 用主域 `ikunling.com` 也可以，但你的博客已经占用它，**建议用 `api` 子域**，互不干扰。

## 四、启动

**窗口 1：启动服务（对外模式）**

```powershell
deploy-local\start-public.bat
```

看到 `模式 : 对外模式（cloudflared 隧道接入）` 即正确。

**窗口 2：启动隧道**

```powershell
cloudflared tunnel run <隧道名称 或 隧道ID>
```

> 如果第三步用的是 `service install`，cloudflared 会注册成 Windows 服务并自动运行，
> 就不需要窗口 2 了。

## 五、验证

```powershell
# 健康检查（公网）
curl https://api.ikunling.com/health

# 模型列表（需要客户端 Key）
curl https://api.ikunling.com/v1/models -H "Authorization: Bearer sk-wb-你的Key"

# 后台应被拒绝（403）
curl -i https://api.ikunling.com/admin/
```

三条结果依次应为：`ok`、正常 JSON、`403`。

## 六、客户端怎么填

| 字段 | 值 |
|---|---|
| Base URL | `https://api.ikunling.com/v1` |
| API Key | 你在后台新建的客户端 Key |

Claude Code 这类用 Anthropic 协议的客户端：`https://api.ikunling.com`。

## 七、安全注意事项

- **换一把新的客户端 Key**。不要在公网使用曾经外传过的 Key，后台「API 密钥」页新建即可。
- **后台不要暴露**。对外模式下 `/admin/` 已强制只允许本机；需要远程管理时用 Tailscale 之类，而不是把后台放出去。
- **留意上游额度**。任何持有 Key 的人都会消耗你的账号积分，目前还没有按 Key 限流，请只把 Key 给自己信任的人。
- **Cloudflare 免费版有合理使用限制**。个人自用完全够；不要拿它跑大文件或高流量场景。

## 八、常见问题

| 现象 | 原因与处理 |
|---|---|
| 公网访问 502 / 1033 | 隧道没连上，或本机服务没启动。先确认窗口 1 在运行 |
| 公网访问 403 且是 `/admin/` | 正常，对外模式下后台仅本机可访问 |
| 公网访问 `/v1/*` 返回 401 | 没带客户端 Key，或 Key 不对 |
| 后台登录后仍跳回登录页 | 隧道场景 Cookie 为 Secure，必须用 `https://` 访问，不能用 `http://` |
| 改了 `.env` 不生效 | 重启服务；客户端 Key 只在首次启动写入数据文件，之后要用 `set-key.ps1` 或后台新建 |

## 九、每日请求限额

每个客户端 Key 每天最多 **1000** 次对话请求，按东八区自然日计算，跨天自动清零。

- 计数只针对真正的对话接口（`/v1/chat/completions`、`/v1/responses`、`/v1/messages`），
  不包含 `/v1/models`、健康检查和管理后台
- 后台「连接测试」用的临时 Key 不计数，避免调试占额度
- 超限返回 HTTP 429，信息形如：`今日请求已达上限（1000 次），明天再试`
- 计数保存在 `management/state.json` 的 `quota_daily` 字段，重启不丢

调整上限：在 `deploy-local\.env` 里改

```ini
DAILY_REQUEST_LIMIT=1000
```

设为 `0` 表示关闭限额。改完重启服务生效。

## 十、对外模型白名单

默认对外暴露全部可用模型。只给朋友用某一个模型时，在 `deploy-local\.env` 里加：

```ini
PUBLIC_MODELS=deepseek-v4.1-flash
```

多个模型用逗号分隔，例如 `PUBLIC_MODELS=deepseek-v4.1-flash,hy3`。留空表示不限制。

生效后：

- `/v1/models` 只返回白名单里的模型，客户端下拉里看不到别的
- 直接请求白名单外的模型会被拒绝，返回 HTTP 403 与提示
- 这条限制**只在服务端生效**，本地管理后台的「连接测试」不受影响（它会走同一套校验，
  所以测试其它模型也会被拒；需要测试别的模型时先把该模型临时加进白名单）

改完重启服务生效。

## 十一、越狱提示词审核

对外模式（`start-public.bat`）默认在本地审核每一个对话请求。命中以下特征时直接返回
HTTP 403 与 `jailbreak_detected`，**请求不会选账号、不会访问上游，也不消耗积分**：

- 忽略 / 绕过系统规则、进入开发者模式或越狱模式（英文同义写法也会识别）
- 要求泄露系统提示词或隐藏指令
- 要求去掉拒答、道德约束或内容审核
- 解码后执行隐藏指令、加载破甲 skill 模板

普通的安全知识问答、翻译与角色扮演不会被拦截。被拦的请求不占每日 1000 次配额；
同一个 Key 在一小时内被连续拦截 10 次，会限制 10 分钟。**不保存用户提示词**。

审核依赖关键词和正则规则，无法保证挡住所有新写法；它是一层前置防线，不能替代
上游模型自身的安全策略。
