@echo off
REM ============================================================
REM  workbuddy2api - 对外模式启动脚本（配合 cloudflared 隧道）
REM  用法：双击本文件，或命令行执行 start-public.bat
REM  说明：服务仍只监听 127.0.0.1，由 cloudflared 从外部接入。
REM        管理后台仅本机可访问，公网只能调用 /v1/*。
REM ============================================================
setlocal
cd /d "%~dp0.."

powershell -ExecutionPolicy Bypass -File "%~dp0start-admin.ps1" -Public %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [workbuddy2api] 对外模式启动失败，错误码 %ERRORLEVEL%
    pause
)
endlocal
