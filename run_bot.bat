@echo off
set SCRIPT_DIR=%~dp0

where py >nul 2>&1
if %errorlevel%==0 (
    py "%SCRIPT_DIR%main.py" %*
    exit /b %errorlevel%
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%SCRIPT_DIR%main.py" %*
    exit /b %errorlevel%
)

echo Python 3.11+ не найден. Установите Python и повторите запуск.
exit /b 1
