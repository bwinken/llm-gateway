@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Claude Code - Enterprise Install Script (.bat)
REM ============================================================
REM  Rename to install-claude-code.bat after editing the values
REM  below. Run by double-clicking or from a normal cmd window;
REM  no admin rights are required.
REM ============================================================

REM ============================================================
REM  >>> EDIT ME — paths to your prebuilt portable folders <<<
REM ============================================================

REM  Node.js standalone (the unzipped node-v20.x-win-x64\ folder).
REM  Anything that contains node.exe at its root works.
set "NODE_SOURCE=\\your-fileserver\public\tools\node-v20.11.1-win-x64"

REM  Git for Windows x64 Portable ("thumbdrive edition") — the
REM  unzipped PortableGit\ folder. Leave blank to skip Git install.
set "GIT_SOURCE=\\your-fileserver\public\tools\PortableGit"

REM ============================================================
REM  Other configuration (less likely to need editing)
REM ============================================================

REM  Where to copy Node / Git on the user's machine (no admin needed)
set "NODE_TARGET=%USERPROFILE%\tools\nodejs"
set "GIT_TARGET=%USERPROFILE%\tools\git"

REM  Corporate proxy. Leave blank to skip.
set "PROXY_URL=http://proxy.company.local:8080"
set "NO_PROXY=localhost,127.0.0.1,.company.local"

REM  Your LLM Gateway endpoint. The API key is injected per-user
REM  at download time by the gateway. If you copy this file from
REM  disk, replace __USER_API_KEY__ with your key from the dashboard.
set "GATEWAY_URL=https://llm-gateway.company.local"
set "API_KEY=__USER_API_KEY__"

REM  Internal npm registry mirror (Verdaccio / Nexus / Artifactory).
REM  Leave as-is to use the public registry via proxy.
set "NPM_REGISTRY=https://registry.npmjs.org/"

REM  Set to 1 if your corporate proxy does TLS inspection and breaks
REM  npm's TLS chain. Insecure; prefer pointing npm at your CA bundle:
REM    npm config set cafile "C:\path\to\corporate-ca-bundle.pem"
set "NPM_STRICT_SSL_OFF=0"

REM  ── Claude Code runtime settings (written to ~/.claude/settings.json) ──
REM  Empty string / 0 / 0 = skip. Only configured keys are written.

set "DEFAULT_MODEL="

set "CUSTOM_MODEL_ALIAS="
set "CUSTOM_MODEL_DESCRIPTION="
set "CUSTOM_MODEL_CAPABILITIES=tools"

set "MAX_CONTEXT_TOKENS=0"
set "MAX_OUTPUT_TOKENS=0"
set "AUTO_COMPACT_WINDOW=0"

set "DISABLE_1M_CONTEXT=0"
set "DISABLE_TELEMETRY=1"
set "DISABLE_INTERLEAVED_THINKING=0"

set "CLAUDE_SHELL="
set "ENABLE_POWERSHELL_TOOL=1"
set "ENABLE_ACCESSIBILITY=0"

REM  Set to 1 ONLY if you can't install the corporate CA. Insecure.
set "SKIP_TLS_VERIFY=0"

REM ============================================================
REM  1. Detect existing installations
REM ============================================================

echo.
echo ^>^> Checking existing installations

set "NODE_EXISTS=0"
set "GIT_EXISTS=0"
set "CLAUDE_EXISTS=0"

where node >nul 2>&1 && set "NODE_EXISTS=1"
where git  >nul 2>&1 && set "GIT_EXISTS=1"
where claude >nul 2>&1 && set "CLAUDE_EXISTS=1"
where claude.cmd >nul 2>&1 && set "CLAUDE_EXISTS=1"

if "%NODE_EXISTS%"=="1"   ( echo    [OK]   node already installed   ) else ( echo    [WARN] node not found )
if "%GIT_EXISTS%"=="1"    ( echo    [OK]   git  already installed   ) else ( echo    [WARN] git not found - will install from shared drive. )
if "%CLAUDE_EXISTS%"=="1" ( echo    [OK]   claude already installed - will reconfigure. )

REM ============================================================
REM  2. Install Node.js from shared drive (if missing)
REM ============================================================

if "%NODE_EXISTS%"=="0" (
    echo.
    echo ^>^> Copying Node.js from %NODE_SOURCE%

    if not exist "%NODE_SOURCE%" (
        echo    [ERR] Shared folder not reachable: %NODE_SOURCE%
        echo    [ERR] Ask your IT admin for the correct Node.js shared drive path.
        pause
        exit /b 1
    )

    if exist "%NODE_TARGET%\node.exe" (
        echo    [WARN] Target already exists, skipping copy: %NODE_TARGET%
    ) else (
        if not exist "%NODE_TARGET%" mkdir "%NODE_TARGET%" >nul 2>&1
        xcopy "%NODE_SOURCE%\*" "%NODE_TARGET%\" /E /I /Y /Q >nul
        if errorlevel 1 (
            echo    [ERR] xcopy failed. Check permissions on %NODE_TARGET%.
            pause
            exit /b 1
        )
        echo    [OK] Node.js copied to %NODE_TARGET%
    )

    set "NODE_DIR=%NODE_TARGET%"
) else (
    for /f "delims=" %%I in ('where node') do set "NODE_DIR=%%~dpI"
    REM strip trailing backslash for cleaner PATH entries
    if defined NODE_DIR set "NODE_DIR=!NODE_DIR:~0,-1!"
)

REM ============================================================
REM  3. Add Node.js to user PATH (persists across reboots)
REM ============================================================

echo.
echo ^>^> Configuring PATH

call :AppendUserPath "%NODE_DIR%"
set "PATH=%NODE_DIR%;%PATH%"

if not exist "%NODE_DIR%\node.exe" (
    echo    [ERR] node.exe not found at %NODE_DIR%
    pause
    exit /b 1
)

"%NODE_DIR%\node.exe" --version
if errorlevel 1 (
    echo    [ERR] node ran but returned an error.
    pause
    exit /b 1
)

REM ============================================================
REM  4. Install Git from shared drive (if missing)
REM ============================================================

if "%GIT_EXISTS%"=="0" if not "%GIT_SOURCE%"=="" (
    echo.
    echo ^>^> Copying Git from %GIT_SOURCE%

    if not exist "%GIT_SOURCE%" (
        echo    [WARN] Shared folder not reachable: %GIT_SOURCE%
        echo    [WARN] Claude Code will still work but worktrees / diffs will be unavailable.
    ) else (
        if exist "%GIT_TARGET%\cmd\git.exe" (
            echo    [WARN] Target already exists, skipping copy: %GIT_TARGET%
        ) else (
            if not exist "%GIT_TARGET%" mkdir "%GIT_TARGET%" >nul 2>&1
            xcopy "%GIT_SOURCE%\*" "%GIT_TARGET%\" /E /I /Y /Q >nul
            if errorlevel 1 (
                echo    [WARN] xcopy failed - continuing without Git.
            ) else (
                echo    [OK] Git copied to %GIT_TARGET%
            )
        )

        if exist "%GIT_TARGET%\cmd\git.exe" (
            call :AppendUserPath "%GIT_TARGET%\cmd"
            set "PATH=%GIT_TARGET%\cmd;%PATH%"
            "%GIT_TARGET%\cmd\git.exe" --version
        ) else (
            echo    [WARN] git.exe not found under %GIT_TARGET%\cmd - did you copy the PortableGit folder?
        )
    )
) else (
    if "%GIT_EXISTS%"=="1" (
        echo    Git already installed - skipping.
    ) else (
        echo    Git install skipped ^(GIT_SOURCE is empty^).
    )
)

REM ── Resolve bash.exe for Claude Code's Bash tool ──
REM PortableGit ships bash.exe under bin\, which is NOT on PATH (only
REM cmd\ is added above). Without CLAUDE_CODE_GIT_BASH_PATH, Claude
REM Code's Bash tool fails to spawn shells on Windows.
set "GIT_BASH_PATH="
if exist "%GIT_TARGET%\bin\bash.exe" set "GIT_BASH_PATH=%GIT_TARGET%\bin\bash.exe"
if defined GIT_BASH_PATH echo    [OK] Git Bash: %GIT_BASH_PATH%

REM ============================================================
REM  5. Configure npm proxy + registry
REM ============================================================

echo.
echo ^>^> Configuring npm

if not "%PROXY_URL%"=="" (
    call npm config set proxy       "%PROXY_URL%" --location=user
    call npm config set https-proxy "%PROXY_URL%" --location=user
    echo    [OK] npm proxy = %PROXY_URL%

    setx HTTP_PROXY  "%PROXY_URL%" >nul
    setx HTTPS_PROXY "%PROXY_URL%" >nul
    setx NO_PROXY    "%NO_PROXY%"  >nul
    set "HTTP_PROXY=%PROXY_URL%"
    set "HTTPS_PROXY=%PROXY_URL%"
    set "NO_PROXY=%NO_PROXY%"
    echo    [OK] HTTP_PROXY / HTTPS_PROXY / NO_PROXY set
) else (
    echo    No proxy configured - skipping.
)

call npm config set registry "%NPM_REGISTRY%" --location=user
echo    [OK] npm registry = %NPM_REGISTRY%

if "%NPM_STRICT_SSL_OFF%"=="1" (
    call npm config set strict-ssl false --location=user
    echo    [WARN] npm strict-ssl = false ^(TLS verification bypassed^)
)

REM ============================================================
REM  6. Install Claude Code globally
REM ============================================================

echo.
echo ^>^> Installing @anthropic-ai/claude-code

call npm install -g "@anthropic-ai/claude-code"
if errorlevel 1 (
    echo    [ERR] npm install failed.
    echo    [ERR] Common causes:
    echo    [ERR]   - Proxy URL wrong ^(check %PROXY_URL%^)
    echo    [ERR]   - Registry blocked by firewall
    echo    [ERR]   - TLS inspection - configure 'npm config set cafile' with your corporate CA
    pause
    exit /b 1
)
echo    [OK] Claude Code installed.

REM Make sure npm prefix is on PATH
for /f "tokens=*" %%P in ('npm config get prefix 2^>nul') do set "NPM_PREFIX=%%P"
if defined NPM_PREFIX (
    echo    npm global prefix: %NPM_PREFIX%
    call :AppendUserPath "%NPM_PREFIX%"
    set "PATH=%NPM_PREFIX%;%PATH%"
)

where claude.cmd >nul 2>&1 && (echo    [OK] claude on PATH) || echo    [WARN] claude not on PATH - open a new terminal.

REM ============================================================
REM  7. Configure %USERPROFILE%\.claude\settings.json (merge)
REM ============================================================

echo.
echo ^>^> Writing %USERPROFILE%\.claude\settings.json

if not exist "%USERPROFILE%\.claude" mkdir "%USERPROFILE%\.claude" >nul 2>&1

REM Build env hashtable inside PowerShell. We pass every relevant
REM variable through -ArgumentList so the inline script stays the
REM same regardless of which knobs the user enabled.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$f=Join-Path $env:USERPROFILE '.claude\settings.json';" ^
  "$s=@{};" ^
  "if(Test-Path $f){try{$s=Get-Content $f -Raw|ConvertFrom-Json -AsHashtable}catch{Copy-Item $f \"$f.bak\" -Force; $s=@{}}};" ^
  "if(-not $s.ContainsKey('env')){$s['env']=@{}};" ^
  "$e=$s['env'];" ^
  "$e['ANTHROPIC_BASE_URL']='%GATEWAY_URL%';" ^
  "$e['ANTHROPIC_API_KEY']='%API_KEY%';" ^
  "$e['ANTHROPIC_AUTH_TOKEN']='%API_KEY%';" ^
  "$e['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']='1';" ^
  "if('%DISABLE_TELEMETRY%' -eq '1'){$e['DISABLE_TELEMETRY']='1'};" ^
  "if('%DISABLE_INTERLEAVED_THINKING%' -eq '1'){$e['DISABLE_INTERLEAVED_THINKING']='1'};" ^
  "if('%DISABLE_1M_CONTEXT%' -eq '1'){$e['CLAUDE_CODE_DISABLE_1M_CONTEXT']='1'};" ^
  "if([int]'%MAX_CONTEXT_TOKENS%' -gt 0){$e['CLAUDE_CODE_MAX_CONTEXT_TOKENS']='%MAX_CONTEXT_TOKENS%'};" ^
  "if([int]'%MAX_OUTPUT_TOKENS%' -gt 0){$e['CLAUDE_CODE_MAX_OUTPUT_TOKENS']='%MAX_OUTPUT_TOKENS%'};" ^
  "if([int]'%AUTO_COMPACT_WINDOW%' -gt 0){$e['CLAUDE_CODE_AUTO_COMPACT_WINDOW']='%AUTO_COMPACT_WINDOW%'};" ^
  "if('%CLAUDE_SHELL%'){$e['CLAUDE_CODE_SHELL']='%CLAUDE_SHELL%'};" ^
  "if('%GIT_BASH_PATH%'){$e['CLAUDE_CODE_GIT_BASH_PATH']='%GIT_BASH_PATH%'};" ^
  "if('%ENABLE_POWERSHELL_TOOL%' -eq '1'){$e['CLAUDE_CODE_USE_POWERSHELL_TOOL']='1'};" ^
  "if('%ENABLE_ACCESSIBILITY%' -eq '1'){$e['CLAUDE_CODE_ACCESSIBILITY']='1'};" ^
  "if('%CUSTOM_MODEL_ALIAS%'){$e['ANTHROPIC_CUSTOM_MODEL_OPTION']='%CUSTOM_MODEL_ALIAS%';" ^
  "  if('%CUSTOM_MODEL_DESCRIPTION%'){$e['ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION']='%CUSTOM_MODEL_DESCRIPTION%'};" ^
  "  if('%CUSTOM_MODEL_CAPABILITIES%'){$e['ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES']='%CUSTOM_MODEL_CAPABILITIES%'}};" ^
  "if('%SKIP_TLS_VERIFY%' -eq '1'){$e['NODE_TLS_REJECT_UNAUTHORIZED']='0'};" ^
  "if('%PROXY_URL%'){$e['HTTP_PROXY']='%PROXY_URL%';$e['HTTPS_PROXY']='%PROXY_URL%';$e['NO_PROXY']='%NO_PROXY%'};" ^
  "if('%DEFAULT_MODEL%'){$s['model']='%DEFAULT_MODEL%'};" ^
  "$s|ConvertTo-Json -Depth 10|Set-Content -Path $f -Encoding UTF8;" ^
  "Write-Host '   [OK] Wrote' $f"

if errorlevel 1 (
    echo    [ERR] Failed to update settings.json
    pause
    exit /b 1
)

echo      ANTHROPIC_BASE_URL = %GATEWAY_URL%
if not "%DEFAULT_MODEL%"=="" echo      model              = %DEFAULT_MODEL%

REM ============================================================
REM  8. Verify
REM ============================================================

echo.
echo ^>^> Verifying installation

call claude --version 2>nul
if errorlevel 1 (
    echo    [WARN] Couldn't run 'claude --version' in this shell. Open a NEW cmd / Terminal and try again.
)

echo.
echo ============================================================
echo   Claude Code installation complete
echo ============================================================
echo.
echo Next steps:
echo   1. Close this window and open a NEW cmd / Terminal.
echo      ^(So the updated PATH takes effect.^)
echo   2. Run:  claude -p "hello"
echo   3. Run:  claude    ^(to start an interactive session^)
echo.
echo Not working? Check:
echo   - Is your gateway URL reachable? ^(try in browser first^)
echo   - TLS errors? Install the corporate CA, or set SKIP_TLS_VERIFY=1 above.
echo   - Run:  claude /doctor    for built-in diagnostics
echo.

pause
exit /b 0

REM ============================================================
REM  Helper: append a directory to the user's persistent PATH
REM  (HKCU\Environment\Path) without duplicating it.
REM ============================================================
:AppendUserPath
set "_NEW=%~1"
if "%_NEW%"=="" goto :eof

set "_USERPATH="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| find /i "Path"') do set "_USERPATH=%%B"

REM Already present? bail out.
echo ;%_USERPATH%; | find /i ";%_NEW%;" >nul && (
    echo    Already in user PATH: %_NEW%
    goto :eof
)

if defined _USERPATH (
    setx Path "%_USERPATH%;%_NEW%" >nul
) else (
    setx Path "%_NEW%" >nul
)
echo    [OK] Added to user PATH: %_NEW%
goto :eof
