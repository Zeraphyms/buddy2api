@echo off
REM ============================================================
REM  workbuddy2api - 管理后台启动脚本（含 API）
REM  用法：双击本文件，或命令行执行 start-admin.bat
REM ============================================================
setlocal
cd /d "%~dp0.."

powershell -ExecutionPolicy Bypass -File "%~dp0start-admin.ps1" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [workbuddy2api] 启动失败，错误码 %ERRORLEVEL%
    pause
)
endlocal