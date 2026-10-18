@echo off
REM ============================================================
REM  workbuddy2api - 对外服务一键启动（服务 + Cloudflare 隧道）
REM
REM  双击本文件即可。会启动：
REM    1. 管理后台 + API（对外模式，后台仅本机可访问）
REM    2. Cloudflare 隧道（如果有隧道令牌，把本机 8787 发布到域名）
REM
REM  首次运行会自动创建 Python 虚拟环境并安装依赖，需要几分钟。
REM  没有 tunnel.token 时仍会启动本地服务，只是不启动隧道。
REM ============================================================
setlocal
set "HERE=%~dp0"
cd /d "%HERE%.."

echo [workbuddy2api] 正在启动服务（新窗口）...
start "workbuddy2api 服务" cmd /k ""%HERE%start-public.bat""

echo [workbuddy2api] 等待服务就绪...
timeout /t 8 /nobreak >nul

if not exist "%HERE%tunnel.token" (
    echo.
    echo [workbuddy2api] 未找到隧道令牌文件，跳过隧道启动。
    echo [workbuddy2api] 服务仅在本机 http://127.0.0.1:8787 可用。
    echo [workbuddy2api] 如需对外访问，把 Cloudflare 令牌保存到：
    echo        %HERE%tunnel.token
    echo    然后重新双击本脚本。
    echo.
    timeout /t 10 /nobreak >nul
    endlocal
    exit /b 0
)

echo [workbuddy2api] 正在启动隧道（新窗口）...
start "workbuddy2api 隧道" cmd /k ""%HERE%start-tunnel.bat""

echo.
echo [workbuddy2api] 已启动。两个窗口都请保持运行。
echo [workbuddy2api] 关闭任一窗口对应的服务就会停止。
echo.
timeout /t 6 /nobreak >nul
endlocal
