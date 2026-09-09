# NOTE: this file is saved as UTF-8 **with BOM** on purpose.
# Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, which turns
# every em dash below into mojibake and breaks parsing outright.
# Keep the BOM if you re-save this file.
# Build the NWA Bench Console desktop app end to end.
#
#     powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#
# The three steps are ordered, not independent (DEPLOYMENT.md §8): Tauri
# embeds whatever is sitting in frontend/dist at the moment it runs, so
# building the shell without rebuilding the frontend first silently ships
# the previous release's UI. That failure is invisible — the app runs
# perfectly, just with the old dashboard — which is exactly why the steps
# live in one script instead of a README list.
#
#   -SkipFrontend / -SkipBackend  re-use the existing artifacts when you
#                                 are only iterating on the Rust shell.
#   -DebugBuild                   faster, unoptimised, no installer.
#                                 (not -Debug: that name is reserved by
#                                 [CmdletBinding()] as a common parameter)

[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipBackend,
    [switch]$DebugBuild
)

$ErrorActionPreference = "Stop"

$Root       = Split-Path -Parent $PSScriptRoot
$Frontend   = Join-Path $Root "frontend"
$Backend    = Join-Path $Root "backend"
$Venv       = Join-Path $Backend "venv\Scripts\python.exe"
$SidecarOut = Join-Path $Backend "dist_sidecar\thrustbench-backend"
$Staged     = Join-Path $Root "src-tauri\resources\backend"

function Step($n, $msg) { Write-Host "`n[$n/4] $msg" -ForegroundColor Cyan }

# ── 1. Frontend ───────────────────────────────────────────────────
Step 1 "Building the frontend"
if ($SkipFrontend) {
    Write-Host "      skipped (-SkipFrontend)" -ForegroundColor DarkGray
    if (-not (Test-Path (Join-Path $Frontend "dist\index.html"))) {
        throw "frontend/dist is missing — cannot skip the frontend build on a clean tree."
    }
} else {
    Push-Location $Frontend
    try {
        if (-not (Test-Path "node_modules")) { npm install }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
    } finally { Pop-Location }
}

# ── 2. Backend ────────────────────────────────────────────────────
Step 2 "Freezing the backend with PyInstaller"
if ($SkipBackend) {
    Write-Host "      skipped (-SkipBackend)" -ForegroundColor DarkGray
    if (-not (Test-Path $SidecarOut)) {
        throw "backend/dist_sidecar is missing — cannot skip the backend build on a clean tree."
    }
} else {
    if (-not (Test-Path $Venv)) { throw "Backend venv not found at $Venv" }
    Push-Location $Backend
    try {
        & $Venv -m PyInstaller thrustbench-backend.spec --noconfirm `
            --distpath dist_sidecar --workpath build_sidecar
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    } finally { Pop-Location }
}

# ── 3. Stage the frozen backend where Tauri expects it ────────────
# Copied rather than referenced in place: tauri.conf.json's resource
# path has to be inside src-tauri, and a stale copy here would ship an
# old backend with a new UI, so it is wiped first every time.
Step 3 "Staging the backend into src-tauri/resources"
if (Test-Path $Staged) { Remove-Item $Staged -Recurse -Force }
New-Item -ItemType Directory -Path $Staged -Force | Out-Null
Copy-Item (Join-Path $SidecarOut "*") $Staged -Recurse -Force

$exe = Join-Path $Staged "thrustbench-backend.exe"
if (-not (Test-Path $exe)) { throw "Staging failed — $exe is missing" }
$sizeMb = [math]::Round((Get-ChildItem $Staged -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "      staged $sizeMb MB" -ForegroundColor DarkGray

# ── 4. Tauri ──────────────────────────────────────────────────────
Step 4 "Building the Tauri shell and installer"
Push-Location $Root
try {
    # Signing material. Both the key and its password must be present as
    # real environment variables before `tauri build` runs, or the CLI
    # stops on an interactive password prompt and the build hangs forever
    # with no output — it looks like a slow LTO build, not a stall.
    #
    # The password cannot be empty for the same reason: in PowerShell
    # `$env:X = ""` REMOVES the variable rather than setting it empty
    # (unlike an exported empty var in bash), so a passwordless key is
    # indistinguishable from no password at all and prompts. The key is
    # therefore generated *with* a password.
    $keyDir = Join-Path $env:USERPROFILE ".nwa-bench-signing"
    if (-not $env:TAURI_SIGNING_PRIVATE_KEY) {
        $keyFile = Join-Path $keyDir "nwa-bench.key"
        $pwFile  = Join-Path $keyDir "password.txt"
        if (Test-Path $keyFile) {
            # The key *contents*, not its path: the CLI reads the path
            # variant inconsistently and reports "a public key has been
            # found, but no private key", which reads like a missing key.
            $env:TAURI_SIGNING_PRIVATE_KEY = (Get-Content $keyFile -Raw)
            if (Test-Path $pwFile) {
                $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = (Get-Content $pwFile -Raw).Trim()
            }
            Write-Host "      signing with $keyFile" -ForegroundColor DarkGray
        } else {
            Write-Warning "No updater signing key found at $keyFile. The installer will build, but it will NOT be signed and installed copies will refuse it as an update."
        }
    }

    if (-not $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD -and $env:TAURI_SIGNING_PRIVATE_KEY) {
        throw "The signing key is set but its password is not. tauri build would stop on an interactive prompt and hang. Put the password in $keyDir\password.txt or set TAURI_SIGNING_PRIVATE_KEY_PASSWORD."
    }

    if ($DebugBuild) { npx tauri build --debug } else { npx tauri build }
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
} finally { Pop-Location }

Write-Host "`nDone." -ForegroundColor Green
$bundle = Join-Path $Root "src-tauri\target\release\bundle"
if (Test-Path $bundle) {
    Get-ChildItem $bundle -Recurse -Include *.exe, *.sig, *.zip |
        ForEach-Object { Write-Host "  $($_.FullName)" }
}
