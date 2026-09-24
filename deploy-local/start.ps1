# workbuddy2api 本地启动脚本 (PowerShell)
# 用法:  powershell -ExecutionPolicy Bypass -File start.ps1
#        powershell -ExecutionPolicy Bypass -File start.ps1 -Port 9000 -ApiKey mysecret
#        powershell -ExecutionPolicy Bypass -File start.ps1 -Force   # 端口被占用时先结束旧进程

param(
    [int]$Port = 8787,
    [string]$ApiKey = "",
    [switch]$NoDesensitize,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# 指定登录态目录（只放可用账号，避免自动选中被后端 401 拒绝的账号）
$env:CODEBUDDY_AUTH_DIR = Join-Path $PSScriptRoot "auth"

if ($ApiKey) { $env:CODEBUDDY2OPENAI_KEY = $ApiKey }

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
        Write-Host "[workbuddy2api] 错误：端口 $Port 已被占用，服务无法启动。" -ForegroundColor Red
        Write-Host "[workbuddy2api] 占用进程 PID: $($busy -join ', ')"
        Write-Host ""
        Write-Host "处理办法（三选一）："
        Write-Host "  1. 停止旧服务：  powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\stop.ps1`" -Port $Port"
        Write-Host "  2. 强制重启：    powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\start.ps1`" -Force"
        Write-Host "  3. 换一个端口：  powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\start.ps1`" -Port 9000"
        Write-Host ""
        exit 1
    }
}

Write-Host "[workbuddy2api] 仓库目录 : $repo"
Write-Host "[workbuddy2api] 登录态   : $env:CODEBUDDY_AUTH_DIR"
Write-Host "[workbuddy2api] 监听     : http://127.0.0.1:$Port"
Write-Host ""

$convArgs = @("-m", "core.converter", "--port", "$Port", "--log", "converter.log")
if (-not $NoDesensitize) { $convArgs += "--desensitize" }

& ".venv\Scripts\python.exe" @convArgs
exit $LASTEXITCODE
