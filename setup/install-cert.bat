@echo off
REM LLM Gateway - install CA certificate to current user's trust store.
REM No administrator privileges required.

if not exist "%~dp0llm-gateway-ca.crt" (
    echo.
    echo ERROR: llm-gateway-ca.crt not found in %~dp0
    echo Make sure the certificate file is in the same folder as this script.
    echo.
    pause
    exit /b 1
)

echo.
echo Installing certificate to current user's trust store...
echo.

certutil -user -addstore Root "%~dp0llm-gateway-ca.crt"

if %errorlevel% equ 0 (
    echo.
    echo SUCCESS: Certificate installed.
    echo Close all browser windows and Office apps, then reopen them.
) else (
    echo.
    echo ERROR: Installation failed. See message above.
)

echo.
pause
