# ============================================================
# Claude Code — Enterprise Install Script (EXAMPLE TEMPLATE)
# ============================================================
# Rename to install-claude-code.ps1 after customising the values
# marked with CUSTOMISE below.
#
# This script installs Claude Code CLI on a Windows machine that
# is behind a strict corporate firewall (no direct internet, no
# admin privileges, no package managers allowed).
#
# Install flow:
#   1. Detect existing Node.js / git / claude
#   2. Copy Node.js portable from shared drive (if not installed)
#   3. Add Node to the user PATH
#   4. Copy Git PortableGit from shared drive (if not installed)
#   5. Configure npm proxy + registry
#   6. Install @anthropic-ai/claude-code globally
#   7. Configure ~/.claude/settings.json to point at your gateway
#   8. Verify and print next steps
# ============================================================

$ErrorActionPreference = "Stop"

# ── Colours ──
function Write-Step    ($msg) { Write-Host ""; Write-Host ">> $msg" -ForegroundColor Cyan }
function Write-Ok      ($msg) { Write-Host "   [OK] $msg" -ForegroundColor Green }
function Write-Warn    ($msg) { Write-Host "   [WARN] $msg" -ForegroundColor Yellow }
function Write-Err     ($msg) { Write-Host "   [ERR] $msg" -ForegroundColor Red }
function Write-Info    ($msg) { Write-Host "   $msg" -ForegroundColor Gray }

# ╔═══════════════════════════════════════════════════════════╗
# ║  CUSTOMISE — these values must match your environment     ║
# ╚═══════════════════════════════════════════════════════════╝

# Path to Node.js portable (unzipped node-v20-win-x64/ from nodejs.org)
# on your internal shared drive. Replace with your actual share.
$NODE_SOURCE = "\\your-fileserver\public\tools\node-v20.11.1-win-x64"

# Where to place Node.js on the user's machine (no admin required)
$NODE_TARGET = Join-Path $env:USERPROFILE "tools\nodejs"

# Path to Git for Windows PortableGit (unzipped PortableGit-*-64-bit.7z.exe
# from gitforwindows.org) on your internal shared drive. If your users
# already have Git installed company-wide, set $GIT_SOURCE = "" to skip.
$GIT_SOURCE = "\\your-fileserver\public\tools\PortableGit"

# Where to place Git on the user's machine (no admin required)
$GIT_TARGET = Join-Path $env:USERPROFILE "tools\git"

# Corporate proxy for npm + Claude Code (leave blank if none)
$PROXY_URL   = "http://proxy.company.local:8080"
$NO_PROXY    = "localhost,127.0.0.1,.company.local"

# Your LLM Gateway endpoint. The API key is injected per-user at
# download time by the gateway when served from /dashboard/install-claude-code.ps1.
# If you run this script directly (copy from disk) without downloading it,
# replace __USER_API_KEY__ below with your key from the dashboard.
$GATEWAY_URL = "https://llm-gateway.company.local"
$API_KEY     = "__USER_API_KEY__"

# Internal npm registry mirror (optional — use Verdaccio / Nexus / Artifactory
# if your company has one. Leave as-is to use the public registry via proxy.)
$NPM_REGISTRY = "https://registry.npmjs.org/"

# ╔═══════════════════════════════════════════════════════════╗
# ║  1. Detect existing installations                         ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Step "Checking existing installations"

function Test-Command($name) {
    # Try both the plain name and the .cmd variant (npm/claude on Windows are .cmd)
    foreach ($candidate in @($name, "$name.cmd", "$name.exe")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    return $null
}

$nodePath   = Test-Command "node"
$gitPath    = Test-Command "git"
$npmPath    = Test-Command "npm"
$claudePath = Test-Command "claude"

if ($nodePath)   { Write-Ok   "node  found at $nodePath" } else { Write-Warn "node not found"   }
if ($npmPath)    { Write-Ok   "npm   found at $npmPath"  } else { Write-Warn "npm not found"    }
if ($gitPath)    { Write-Ok   "git   found at $gitPath"  } else { Write-Warn "git not found — will install from shared drive." }
if ($claudePath) { Write-Ok   "claude already installed at $claudePath — this script will reconfigure it." }

# ╔═══════════════════════════════════════════════════════════╗
# ║  2. Install Node.js from shared drive (if missing)        ║
# ╚═══════════════════════════════════════════════════════════╝

if (-not $nodePath) {
    Write-Step "Copying Node.js from $NODE_SOURCE"

    if (-not (Test-Path $NODE_SOURCE)) {
        Write-Err "Shared folder not reachable: $NODE_SOURCE"
        Write-Err "Ask your IT admin for the correct Node.js shared drive path."
        Read-Host "Press Enter to exit"
        exit 1
    }

    if (Test-Path $NODE_TARGET) {
        Write-Warn "Target already exists, skipping copy: $NODE_TARGET"
    } else {
        New-Item -ItemType Directory -Path (Split-Path $NODE_TARGET -Parent) -Force | Out-Null
        Copy-Item -Path $NODE_SOURCE -Destination $NODE_TARGET -Recurse -Force
        Write-Ok "Node.js copied to $NODE_TARGET"
    }
} else {
    Write-Info "Node.js already installed — skipping copy."
}

# ╔═══════════════════════════════════════════════════════════╗
# ║  3. Add Node.js to user PATH (permanently)                ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Step "Configuring PATH"

$nodeDir = if ($nodePath) { Split-Path $nodePath -Parent } else { $NODE_TARGET }

# Persist to user env (no admin needed, survives reboot)
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$nodeDir*") {
    $newPath = if ($userPath) { "$userPath;$nodeDir" } else { $nodeDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Ok "Added to user PATH: $nodeDir"
} else {
    Write-Info "Already in user PATH: $nodeDir"
}

# Also update the CURRENT shell so the rest of this script can see node/npm
$env:Path = "$nodeDir;$env:Path"

# Confirm node is now callable
try {
    $nodeVer = & node --version
    Write-Ok "node --version = $nodeVer"
} catch {
    Write-Err "node still not runnable after PATH update. Check $nodeDir contents."
    Read-Host "Press Enter to exit"
    exit 1
}

# ╔═══════════════════════════════════════════════════════════╗
# ║  4. Install Git from shared drive (if missing)            ║
# ╚═══════════════════════════════════════════════════════════╝
# Git is used by Claude Code for version control features (diffs,
# worktrees, PR tooling). Skipped entirely if $GIT_SOURCE is blank
# or git is already installed.

if (-not $gitPath -and $GIT_SOURCE) {
    Write-Step "Copying Git from $GIT_SOURCE"

    if (-not (Test-Path $GIT_SOURCE)) {
        Write-Warn "Shared folder not reachable: $GIT_SOURCE"
        Write-Warn "Claude Code will still work but some features (worktrees, diffs) will be unavailable."
    } else {
        if (Test-Path $GIT_TARGET) {
            Write-Warn "Target already exists, skipping copy: $GIT_TARGET"
        } else {
            New-Item -ItemType Directory -Path (Split-Path $GIT_TARGET -Parent) -Force | Out-Null
            Copy-Item -Path $GIT_SOURCE -Destination $GIT_TARGET -Recurse -Force
            Write-Ok "Git copied to $GIT_TARGET"
        }

        # PortableGit puts the git binary under cmd/git.exe
        $gitCmdDir = Join-Path $GIT_TARGET "cmd"
        if (-not (Test-Path (Join-Path $gitCmdDir "git.exe"))) {
            Write-Warn "git.exe not found under $gitCmdDir — check that you copied the PortableGit folder (not an installer .exe)."
        } else {
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            if ($userPath -notlike "*$gitCmdDir*") {
                $newPath = if ($userPath) { "$userPath;$gitCmdDir" } else { $gitCmdDir }
                [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
                Write-Ok "Added to user PATH: $gitCmdDir"
            } else {
                Write-Info "Already in user PATH: $gitCmdDir"
            }
            $env:Path = "$gitCmdDir;$env:Path"

            try {
                $gitVer = & git --version
                Write-Ok "git --version = $gitVer"
            } catch {
                Write-Warn "git still not runnable after PATH update — you may need a new terminal."
            }
        }
    }
} elseif (-not $gitPath -and -not $GIT_SOURCE) {
    Write-Info "Git install skipped (\$GIT_SOURCE is empty)."
} else {
    Write-Info "Git already installed — skipping."
}

# ╔═══════════════════════════════════════════════════════════╗
# ║  5. Configure npm proxy + registry                        ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Step "Configuring npm"

if ($PROXY_URL) {
    & npm config set proxy       $PROXY_URL        --location=user
    & npm config set https-proxy $PROXY_URL        --location=user
    Write-Ok "npm proxy = $PROXY_URL"

    # Also set env vars so node itself (and any spawned child) honours proxy
    [Environment]::SetEnvironmentVariable("HTTP_PROXY",  $PROXY_URL, "User")
    [Environment]::SetEnvironmentVariable("HTTPS_PROXY", $PROXY_URL, "User")
    [Environment]::SetEnvironmentVariable("NO_PROXY",    $NO_PROXY,  "User")
    $env:HTTP_PROXY  = $PROXY_URL
    $env:HTTPS_PROXY = $PROXY_URL
    $env:NO_PROXY    = $NO_PROXY
    Write-Ok "HTTP_PROXY / HTTPS_PROXY / NO_PROXY set"
} else {
    Write-Info "No proxy configured — skipping."
}

& npm config set registry $NPM_REGISTRY --location=user
Write-Ok "npm registry = $NPM_REGISTRY"

# Make sure npm trusts the same CA bundle (the gateway cert is a separate
# concern handled by install-cert-user.ps1; this is for npm → registry).
# If your corporate proxy does TLS inspection, point npm at the bundled CA:
#   & npm config set cafile "C:\path\to\corporate-ca-bundle.pem" --location=user

# ╔═══════════════════════════════════════════════════════════╗
# ║  6. Install Claude Code globally                          ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Step "Installing @anthropic-ai/claude-code"

try {
    & npm install -g "@anthropic-ai/claude-code"
    Write-Ok "Claude Code installed."
} catch {
    Write-Err "npm install failed: $($_.Exception.Message)"
    Write-Err "Common causes:"
    Write-Err "  - Proxy URL wrong (check $PROXY_URL)"
    Write-Err "  - Registry blocked by firewall (ask IT to allow registry.npmjs.org)"
    Write-Err "  - TLS inspection — configure 'npm config set cafile' with your corporate CA"
    Read-Host "Press Enter to exit"
    exit 1
}

# Confirm claude is now on PATH (npm -g installs to a prefix that may need
# to be added to PATH separately)
$npmPrefix = (& npm config get prefix).Trim()
Write-Info "npm global prefix: $npmPrefix"
if ($env:Path -notlike "*$npmPrefix*") {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$npmPrefix*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$npmPrefix", "User")
        Write-Ok "Added npm prefix to user PATH: $npmPrefix"
    }
    $env:Path = "$npmPrefix;$env:Path"
}

$claudeBin = Test-Command "claude"
if ($claudeBin) {
    Write-Ok "claude installed at $claudeBin"
} else {
    Write-Warn "claude not found on PATH. You may need to restart your terminal."
}

# ╔═══════════════════════════════════════════════════════════╗
# ║  7. Configure ~/.claude/settings.json                     ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Step "Writing ~/.claude/settings.json"

$claudeDir      = Join-Path $env:USERPROFILE ".claude"
$settingsFile   = Join-Path $claudeDir "settings.json"

if (-not (Test-Path $claudeDir)) {
    New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null
}

# If settings.json already exists, we MERGE instead of overwriting so the
# user's existing preferences (model, theme, etc.) are preserved.
$existing = @{}
if (Test-Path $settingsFile) {
    try {
        $existing = Get-Content $settingsFile -Raw | ConvertFrom-Json -AsHashtable
        Write-Info "Existing settings.json found — merging."
    } catch {
        Write-Warn "Existing settings.json is not valid JSON — backing up and starting fresh."
        Copy-Item $settingsFile "$settingsFile.bak" -Force
        $existing = @{}
    }
}

if (-not $existing.ContainsKey("env")) { $existing["env"] = @{} }

# These three keys route Claude Code to your gateway and suppress all
# non-essential telemetry / update checks (critical for airgapped use).
$existing["env"]["ANTHROPIC_BASE_URL"]                   = $GATEWAY_URL
$existing["env"]["ANTHROPIC_API_KEY"]                    = $API_KEY
$existing["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

# Write back as pretty JSON
$existing | ConvertTo-Json -Depth 10 | Set-Content $settingsFile -Encoding UTF8
Write-Ok "Wrote $settingsFile"
Write-Info "  ANTHROPIC_BASE_URL = $GATEWAY_URL"
Write-Info "  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = 1"

# ╔═══════════════════════════════════════════════════════════╗
# ║  8. Verify                                                ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Step "Verifying installation"

try {
    $cv = & claude --version 2>&1
    Write-Ok "claude --version = $cv"
} catch {
    Write-Warn "Couldn't run 'claude --version' in this shell. Open a NEW PowerShell window and try again."
}

# ╔═══════════════════════════════════════════════════════════╗
# ║  Done                                                     ║
# ╚═══════════════════════════════════════════════════════════╝

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Claude Code installation complete" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Close this window and open a NEW PowerShell / Terminal." -ForegroundColor Gray
Write-Host "     (So the updated PATH takes effect.)" -ForegroundColor Gray
Write-Host "  2. Run:  claude -p `"hello`"" -ForegroundColor Gray
Write-Host "     You should see a reply routed through your gateway." -ForegroundColor Gray
Write-Host "  3. Run:  claude" -ForegroundColor Gray
Write-Host "     To start an interactive session." -ForegroundColor Gray
Write-Host ""
Write-Host "Not working? Check:" -ForegroundColor Yellow
Write-Host "  - Did you install the CA certificate? (run install-cert-user.ps1)" -ForegroundColor Gray
Write-Host "  - Is your gateway URL reachable? (try in browser first)" -ForegroundColor Gray
Write-Host "  - Use: claude /doctor     to run built-in diagnostics" -ForegroundColor Gray
Write-Host ""

Read-Host "Press Enter to close"
