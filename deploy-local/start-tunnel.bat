@echo off
REM ============================================================
REM  workbuddy2api - Cloudflare 隧道启动脚本
REM  用法：双击本文件，保持窗口开着即可
REM  说明：把本机 8787 端口通过隧道发布到 api.ikunling.com
REM        隧道令牌存在同目录 tunnel.token 里，不要外传。
REM ============================================================
setlocal
set "HERE=%~dp0"
set "TOKENFILE=%HERE%tunnel.token"
set "CF=%HERE%..\cloudflared.exe"

if not exist "%CF%" (
    echo [workbuddy2api] 找不到 cloudflared.exe，请放在项目根目录
    pause
    exit /b 1
)
if not exist "%TOKENFILE%" (
    echo [workbuddy2api] 找不到隧道令牌文件: %TOKENFILE%
    pause
    exit /b 1
)

set /p TOKEN=<"%TOKENFILE%"
echo [workbuddy2api] 正在启动 Cloudflare 隧道...
echo [workbuddy2api] 按 Ctrl+C 停止
echo.
"%CF%" tunnel --no-autoupdate run --token %TOKEN%

echo.
echo [workbuddy2api] 隧道已退出，错误码 %ERRORLEVEL%
pause
endlocal
