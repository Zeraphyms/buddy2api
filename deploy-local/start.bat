@echo off
REM ============================================================
REM  workbuddy2api - Windows 启动脚本
REM  用法：双击本文件，或在命令行执行 start.bat
REM ============================================================
setlocal
cd /d "%~dp0.."

REM 指定登录态目录（只放可用账号，避免自动选中被后端 401 拒绝的账号）
set "CODEBUDDY_AUTH_DIR=%~dp0auth"
set "PORT=8787"

REM ---------- 端口占用检查 ----------
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL%==0 (
    echo.
    echo [workbuddy2api] 错误：端口 %PORT% 已被占用，服务无法启动。
    echo.
    echo 占用该端口的进程：
    netstat -ano | findstr ":%PORT%" | findstr "LISTENING"
    echo.
    echo 处理办法（二选一）：
    echo   1. 结束占用进程：  taskkill /PID ^<上面的PID^> /F
    echo   2. 换一个端口：    powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Port 9000
    echo.
    pause
    exit /b 1
)

echo [workbuddy2api] 登录态目录: %CODEBUDDY_AUTH_DIR%
echo [workbuddy2api] 启动中，监听 http://127.0.0.1:%PORT% ...
echo.

".venv\Scripts\python.exe" -m core.converter --desensitize --log converter.log

echo.
echo [workbuddy2api] 服务已退出（错误码 %ERRORLEVEL%）。
pause
endlocal