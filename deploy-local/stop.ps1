# workbuddy2api 停止脚本
# 用法:  powershell -ExecutionPolicy Bypass -File stop.ps1
#        powershell -ExecutionPolicy Bypass -File stop.ps1 -Port 9000

param([int]$Port = 8787)

function Get-ListenerPids {
    param([int]$P)
    $out = netstat -ano
    $pids = @()
    foreach ($line in $out) {
        # 匹配形如:  TCP    127.0.0.1:8787    0.0.0.0:0    LISTENING    13072
        if ($line -match "^\s*TCP\s+\S+:$P\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            $pids += [int]$Matches[1]
        }
    }
    return ($pids | Sort-Object -Unique)
}

$pids = Get-ListenerPids -P $Port

if ($pids.Count -eq 0) {
    Write-Host "[workbuddy2api] 端口 $Port 上没有正在运行的服务。" -ForegroundColor Yellow
    return
}

foreach ($procId in $pids) {
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -eq $p) {
        Write-Host ("[workbuddy2api] PID {0} 已不存在，跳过。" -f $procId) -ForegroundColor DarkGray
        continue
    }
    Write-Host ("[workbuddy2api] 结束进程 PID {0} ({1})" -f $procId, $p.ProcessName)
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2
if ((Get-ListenerPids -P $Port).Count -gt 0) {
    Write-Host "[workbuddy2api] 端口仍被占用，请手动检查。" -ForegroundColor Red
} else {
    Write-Host "[workbuddy2api] 服务已停止，端口 $Port 已释放。" -ForegroundColor Green
}
