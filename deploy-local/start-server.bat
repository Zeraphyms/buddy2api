@echo off
REM ============================================================
REM  workbuddy2api - 对外服务一键启动（服务 + Cloudflare 隧道）
REM
REM  双击本文件即可。会在两个窗口里分别启动：
REM    1. 管理后台 + API（对外模式，后台仅本机可访问）
REM    2. Cloudflare 隧道（把本机 8787 发布到你的域名）
REM
REM  首次运行会自动创建 Python 虚拟环境并安装依赖，需要几分钟。
REM  需要同目录下有 tunnel.token（隧道令牌），否则不会启动隧道。
REM ============================================================
setlocal
set "HERE=%~dp0"
cd /d "%HERE%.."

if not exist "%HERE%tunnel.token" (
    echo [workbuddy2api] 缺少隧道令牌文件: %HERE%tunnel.token
    echo [workbuddy2api] 请先从 Cloudflare 隧道页面复制令牌保存到该文件。
    echo.
    pause
    exit /b 1
)

echo [workbuddy2api] 正在启动服务（新窗口）...
start "workbuddy2api 服务" cmd /k ""%HERE%start-public.bat""

echo [workbuddy2api] 等待服务就绪...
timeout /t 8 /nobreak >nul

echo [workbuddy2api] 正在启动隧道（新窗口）...
start "workbuddy2api 隧道" cmd /k ""%HERE%start-tunnel.bat""

echo.
echo [workbuddy2api] 已启动。两个窗口都请保持运行。
echo [workbuddy2api] 关闭任一窗口对应的服务就会停止。
echo.
timeout /t 6 /nobreak >nul
endlocal
