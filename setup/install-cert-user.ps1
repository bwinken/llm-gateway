# LLM Gateway — install CA certificate to current user's trust store.
# No administrator privileges required.

$ErrorActionPreference = "Stop"

$certFile = Join-Path $PSScriptRoot "llm-gateway-ca.crt"

if (-not (Test-Path $certFile)) {
    Write-Host ""
    Write-Host "ERROR: Cannot find $certFile" -ForegroundColor Red
    Write-Host "Make sure llm-gateway-ca.crt is in the same folder as this script." -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "Installing certificate to current user's trust store..." -ForegroundColor Cyan

try {
    Import-Certificate -FilePath $certFile -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
    Write-Host ""
    Write-Host "SUCCESS: Certificate installed." -ForegroundColor Green
    Write-Host "Close all browser windows and Office apps, then reopen them." -ForegroundColor Green
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
}

Read-Host "Press Enter to close"
