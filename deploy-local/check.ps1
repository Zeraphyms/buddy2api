# workbuddy2api 一键自检脚本
# 用法:  powershell -ExecutionPolicy Bypass -File check.ps1

param([int]$Port = 8787)

$base = "http://127.0.0.1:$Port"
$ok = $true

# ---------- 读取 .env 中的客户端 Key（管理后台模式下 /v1/* 需要鉴权） ----------
$apiKey = $env:CODEBUDDY2OPENAI_KEY
$envFile = Join-Path $PSScriptRoot ".env"
if (-not $apiKey -and (Test-Path $envFile)) {
    foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
        $t = $line.Trim()
        if ($t -match '^CODEBUDDY2OPENAI_KEY=(.+)$') { $apiKey = $Matches[1].Trim(); break }
    }
}
$authHeaders = @{}
if ($apiKey) { $authHeaders["Authorization"] = "Bearer $apiKey" }

function Test-Endpoint {
    param([string]$Name, [string]$Method, [string]$Url, [string]$Body, [int]$Retries = 2)
    $lastErr = $null
    for ($i = 0; $i -le $Retries; $i++) {
        try {
            $p = @{ Uri = $Url; Method = $Method; UseBasicParsing = $true; TimeoutSec = 120 }
            if ($Body) { $p.ContentType = "application/json"; $p.Body = $Body }
            if ($authHeaders.Count -gt 0) { $p.Headers = $authHeaders }
            $r = Invoke-WebRequest @p
            $note = if ($i -gt 0) { "  (第 $($i+1) 次重试成功)" } else { "" }
            Write-Host ("  [OK]   {0,-22} HTTP {1}{2}" -f $Name, $r.StatusCode, $note) -ForegroundColor Green
            return $r
        } catch {
            $lastErr = $_
            if ($i -lt $Retries) { Start-Sleep -Seconds 2 }
        }
    }
    Write-Host ("  [FAIL] {0,-22} {1}" -f $Name, $lastErr.Exception.Message) -ForegroundColor Red
    $script:ok = $false
    return $null
}

Write-Host "`n=== workbuddy2api 自检 ($base) ===`n" -ForegroundColor Cyan

# 1. 健康检查
$h = Test-Endpoint "GET /health" "GET" "$base/health"
if ($h) {
    $j = $h.Content | ConvertFrom-Json
    if ($j.credential) {
        # 独立转换器模式：/health 直接带账号信息
        Write-Host ("         账号: {0} | token 过期: {1}" -f $j.credential.nickname, $j.credential.token_expired)
    } else {
        # 管理后台模式：/health 精简，账号详情在 /admin/
        Write-Host ("         模式: 管理后台（账号详情见 http://127.0.0.1:$Port/admin/）")
    }
}

# 2. 模型列表
$m = Test-Endpoint "GET /v1/models" "GET" "$base/v1/models"
if ($m) {
    $jm = $m.Content | ConvertFrom-Json
    Write-Host ("         模型数: {0}" -f $jm.data.Count)
}

$model = "deepseek-v4.1-flash"

# 3. Chat Completions（非流式）
$c = Test-Endpoint "POST /v1/chat/completions" "POST" "$base/v1/chat/completions" `
    ("{`"model`":`"$model`",`"messages`":[{`"role`":`"user`",`"content`":`"Reply with exactly: OK`"}]}")
if ($c) {
    $jc = $c.Content | ConvertFrom-Json
    Write-Host ("         回复: {0}" -f $jc.choices[0].message.content)
}

# 4. Chat Completions（流式）
$s = Test-Endpoint "POST (stream) chat" "POST" "$base/v1/chat/completions" `
    ("{`"model`":`"$model`",`"stream`":true,`"messages`":[{`"role`":`"user`",`"content`":`"hi`"}]}")
if ($s) {
    $n = ($s.Content -split "`n" | Where-Object { $_ -match '^data:' }).Count
    Write-Host ("         SSE 事件行数: {0}" -f $n)
}

# 5. Responses API
$rp = Test-Endpoint "POST /v1/responses" "POST" "$base/v1/responses" `
    ("{`"model`":`"$model`",`"input`":[{`"role`":`"user`",`"content`":[{`"type`":`"input_text`",`"text`":`"Reply with exactly: OK`"}]}]}")
if ($rp) {
    $jr = $rp.Content | ConvertFrom-Json
    Write-Host ("         status: {0}" -f $jr.status)
}

# 6. Anthropic Messages（非流式）
$an = Test-Endpoint "POST /v1/messages" "POST" "$base/v1/messages" `
    ("{`"model`":`"$model`",`"max_tokens`":64,`"stream`":false,`"messages`":[{`"role`":`"user`",`"content`":`"Reply with exactly: OK`"}]}")
if ($an) {
    $ja = $an.Content | ConvertFrom-Json
    Write-Host ("         回复: {0}" -f (($ja.content | Where-Object { $_.type -eq 'text' }).text))
}

# 7. 管理后台页面（仅当以 start-admin 启动时存在）
Write-Host ""
try {
    $a = Invoke-WebRequest "$base/admin/" -UseBasicParsing -TimeoutSec 15
    Write-Host ("  [OK]   {0,-22} HTTP {1}" -f "GET /admin/", $a.StatusCode) -ForegroundColor Green
    $title = [regex]::Match($a.Content, '<title>(.*?)</title>').Groups[1].Value
    Write-Host ("         管理界面标题: {0}" -f $title)
} catch {
    Write-Host ("  [--]   {0,-22} 未启用管理后台（用 start-admin.ps1 启动）" -f "GET /admin/") -ForegroundColor Yellow
}

# 8. 模型倍率（仅管理后台模式）
try {
    $s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $adminKey = $null
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
            if ($line.Trim() -match '^ADMIN_KEY=(.+)$') { $adminKey = $Matches[1].Trim(); break }
        }
    }
    if (-not $adminKey) { throw "未找到 ADMIN_KEY" }
    $lb = (@{key=$adminKey} | ConvertTo-Json -Compress)
    $lr = Invoke-WebRequest "$base/admin/api/login" -Method POST -ContentType "application/json" -Body $lb -UseBasicParsing -TimeoutSec 20 -WebSession $s
    $sid = [regex]::Match(($lr.Headers['Set-Cookie'] -join ';'), 'workbuddy_admin=([^;]+)').Groups[1].Value
    # 后台用 Secure Cookie；本地 HTTP 下需手动放进会话容器并清掉 Secure 标志
    $ck = New-Object System.Net.Cookie("workbuddy_admin", $sid, "/admin", "127.0.0.1")
    $ck.Secure = $false
    $s.Cookies.Add($ck)
    $mr = Invoke-WebRequest "$base/admin/api/models" -UseBasicParsing -TimeoutSec 120 -WebSession $s
    $mj = $mr.Content | ConvertFrom-Json
    Write-Host ("  [OK]   {0,-22} HTTP {1}" -f "GET /admin/api/models", $mr.StatusCode) -ForegroundColor Green
    foreach ($rg in @("cn", "intl")) {
        $info = $mj.regions.$rg
        if ($info -and $info.count) {
            Write-Host ("         {0} 模型 {1} 个，促销 {2} 项" -f $rg, $info.count, $info.promotions)
        } elseif ($info -and $info.error) {
            Write-Host ("         {0} 目录获取失败: {1}" -f $rg, $info.error) -ForegroundColor Yellow
        }
    }
    $freeCount = ($mj.models | Where-Object { $_.effective -eq 0 }).Count
    Write-Host ("         免费模型: {0} 个" -f $freeCount)
} catch {
    Write-Host ("  [--]   {0,-22} 跳过（{1}）" -f "GET /admin/api/models", $_.Exception.Message) -ForegroundColor Yellow
}

Write-Host ""
if ($ok) { Write-Host "全部通过 ✅" -ForegroundColor Green }
else     { Write-Host "存在失败项 ❌  请查看 converter.log" -ForegroundColor Red }
Write-Host ""
