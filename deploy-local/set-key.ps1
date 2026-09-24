# 修改客户端 API Key
#
# 用法：
#   .\set-key.ps1                          # 交互式，自动生成一个新 key
#   .\set-key.ps1 -NewKey "sk-wb-自定义"    # 指定新 key
#   .\set-key.ps1 -KeepOld                 # 保留旧 key（默认会撤销旧 key）
#   .\set-key.ps1 -List                    # 只列出当前所有 key
#
# 说明：客户端 Key 的服务端只存 SHA-256 摘要，无法从后台还原。
#       本脚本直接更新管理数据文件，改完需重启服务。

param(
    [string]$NewKey = "",
    [switch]$KeepOld,
    [switch]$List
)

$ErrorActionPreference = "Stop"
$stateFile = Join-Path $PSScriptRoot "management\state.json"

if (-not (Test-Path $stateFile)) {
    Write-Host "[错误] 找不到 $stateFile" -ForegroundColor Red
    Write-Host "       服务还没初始化过，请先用 start-admin 启动一次。" -ForegroundColor Red
    exit 1
}

$state = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json

function Show-Keys($s) {
    $rows = @($s.keys.PSObject.Properties)
    if ($rows.Count -eq 0) { Write-Host "  （没有任何客户端 Key）" -ForegroundColor Yellow; return }
    foreach ($p in $rows) {
        Write-Host ("  {0,-16} {1}  创建于 {2}" -f $p.Value.name, $p.Value.hint,
            ([DateTimeOffset]::FromUnixTimeSeconds([int]$p.Value.created).LocalDateTime))
    }
}

if ($List) {
    Write-Host "[workbuddy2api] 当前客户端 Key：" -ForegroundColor Cyan
    Show-Keys $state
    Write-Host ""
    Write-Host "提示：完整 Key 只在创建时显示一次。若需要完整值，用「接入指南」页复制 .env 里的初始 Key。" -ForegroundColor DarkGray
    exit 0
}

Write-Host "[workbuddy2api] 当前客户端 Key：" -ForegroundColor Cyan
Show-Keys $state
Write-Host ""

if (-not $NewKey) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $NewKey = "sk-wb-" + [Convert]::ToBase64String($bytes).Replace("+","").Replace("/","").Replace("=","").Substring(0,43)
    Write-Host "已自动生成新 Key。" -ForegroundColor Green
}

if ($NewKey.Length -lt 12) {
    Write-Host "[错误] Key 太短（至少 12 字符）" -ForegroundColor Red
    exit 1
}

# 计算 SHA-256 摘要（与服务端 digest() 一致：小写十六进制）
$sha = [System.Security.Cryptography.SHA256]::Create()
$hash = ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($NewKey)) |
         ForEach-Object { $_.ToString("x2") }) -join ""

$hint = $NewKey.Substring(0,5) + "…" + $NewKey.Substring($NewKey.Length - 4)
$kid = -join ((1..16) | ForEach-Object { "0123456789abcdef"[(Get-Random -Max 16)] })
$now = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

# 是否已有同名 key
$existing = @($state.keys.PSObject.Properties) | Where-Object { $_.Value.name -eq "原有 API Key" }

if ($existing -and -not $KeepOld) {
    $existing | ForEach-Object { $state.keys.PSObject.Properties.Remove($_.Name) }
    Write-Host "已移除旧的「原有 API Key」。" -ForegroundColor Yellow
}

$state.keys | Add-Member -NotePropertyName $kid -NotePropertyValue ([PSCustomObject]@{
    name = "原有 API Key"; hash = $hash; hint = $hint; created = $now
}) -Force

# 备份后写回
$backup = "$stateFile.bak"
Copy-Item $stateFile $backup -Force
[System.IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "[workbuddy2api] 新的客户端 Key：" -ForegroundColor Green
Write-Host "  $NewKey" -ForegroundColor White
Write-Host ""
Write-Host "请立即保存——服务端只存摘要，之后无法再查看。" -ForegroundColor Yellow
Write-Host "备份已存到: $backup" -ForegroundColor DarkGray
Write-Host ""
Write-Host "接下来：" -ForegroundColor Cyan
Write-Host "  1. 重启服务使其生效：  .\stop.ps1  然后  .\start-admin.bat"
Write-Host "  2. 更新所有客户端里的 API Key"
Write-Host ""
Write-Host "提示：也可以不改这里，直接在管理后台「API 密钥」页新建一个 Key。" -ForegroundColor DarkGray
