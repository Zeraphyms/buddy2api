# workbuddy2api 管理后台启动脚本
# 后台同时提供 API 与 Web 管理界面（同一端口）
#
# 用法:  powershell -ExecutionPolicy Bypass -File start-admin.ps1
#        powershell -ExecutionPolicy Bypass -File start-admin.ps1 -Port 9000
#        powershell -ExecutionPolicy Bypass -File start-admin.ps1 -Force
#        powershell -ExecutionPolicy Bypass -File start-admin.ps1 -InsecureCookie   # 纯 HTTP 下无法登录时使用

param(
    [int]$Port = 0,
    [switch]$Force,
    [switch]$InsecureCookie
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# ---------- 读取 .env ----------
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[workbuddy2api] 找不到配置文件 $envFile" -ForegroundColor Red
    exit 1
}
foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#")) { continue }
    $i = $t.IndexOf("=")
    if ($i -lt 1) { continue }
    $k = $t.Substring(0, $i).Trim()
    $v = $t.Substring($i + 1).Trim()
    if (-not [Environment]::GetEnvironmentVariable($k)) {
        [Environment]::SetEnvironmentVariable($k, $v, "Process")
    }
}
if ($Port -eq 0) {
    $Port = if ($env:PORT) { [int]$env:PORT } else { 8787 }
}

# ---------- 登录态目录（只放可用账号，避免选中被后端 401 拒绝的账号） ----------
$env:CODEBUDDY_AUTH_DIR = Join-Path $PSScriptRoot "auth"
$env:MANAGEMENT_DATA_DIR = Join-Path $PSScriptRoot "management"

if (-not $env:ADMIN_KEY -or $env:ADMIN_KEY.Length -lt 20) {
    Write-Host "[workbuddy2api] ADMIN_KEY 缺失或不足 20 字符，请检查 .env" -ForegroundColor Red
    exit 1
}

# ---------- 端口占用检查 ----------
function Get-ListenerPids {
    param([int]$P)
    $pids = @()
    foreach ($line in (netstat -ano)) {
        if ($line -match "^\s*TCP\s+\S+:$P\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            $pids += [int]$Matches[1]
        }
    }
    return ($pids | Sort-Object -Unique)
}

$busy = Get-ListenerPids -P $Port
if ($busy.Count -gt 0) {
    if ($Force) {
        Write-Host "[workbuddy2api] 端口 $Port 被占用，-Force 已启用，先结束旧进程..." -ForegroundColor Yellow
        & (Join-Path $PSScriptRoot "stop.ps1") -Port $Port
        Start-Sleep -Seconds 1
    } else {
        Write-Host ""
        Write-Host "[workbuddy2api] 错误：端口 $Port 已被占用。" -ForegroundColor Red
        Write-Host "[workbuddy2api] 占用进程 PID: $($busy -join ', ')"
        Write-Host "  1. 停止旧服务：  ...\stop.ps1 -Port $Port"
        Write-Host "  2. 强制重启：    ...\start-admin.ps1 -Force"
        Write-Host "  3. 换一个端口：  ...\start-admin.ps1 -Port 9000"
        Write-Host ""
        exit 1
    }
}

Write-Host "[workbuddy2api] 模式     : 管理后台 + API（单进程）"
Write-Host "[workbuddy2api] 登录态   : $env:CODEBUDDY_AUTH_DIR"
Write-Host "[workbuddy2api] 管理数据 : $env:MANAGEMENT_DATA_DIR"
Write-Host "[workbuddy2api] 管理界面 : http://127.0.0.1:$Port/admin/"
Write-Host "[workbuddy2api] 管理密钥 : $env:ADMIN_KEY"
Write-Host "[workbuddy2api] API Key  : $env:CODEBUDDY2OPENAI_KEY"
if ($InsecureCookie) {
    Write-Host "[workbuddy2api] Cookie   : 已关闭 Secure 标记（仅纯 HTTP 本地调试）" -ForegroundColor Yellow
}
Write-Host ""

# 后台默认 secure_cookie=True（面向 HTTPS 反代）。纯 HTTP 访问时若浏览器
# 拒绝保存 Secure Cookie，可用 -InsecureCookie 关闭该标记。
if ($InsecureCookie) {
    $code = @"
import os, uvicorn
from admin.server import create_app
uvicorn.run(create_app(secure_cookie=False), host="127.0.0.1", port=$Port, log_level="warning")
"@
    $code | & ".venv\Scripts\python.exe" -
} else {
    $code = @"
import os, uvicorn
from admin.server import create_app
uvicorn.run(create_app(), host="127.0.0.1", port=$Port, log_level="warning")
"@
    $code | & ".venv\Scripts\python.exe" -
}
exit $LASTEXITCODE
