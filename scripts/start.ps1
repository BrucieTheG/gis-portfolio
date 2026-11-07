<#
.SYNOPSIS
    Start everything the site needs, in order, and stop it all on Ctrl+C.

.DESCRIPTION
    1. Checks that PostgreSQL is running (starts the service if it can).
    2. Refreshes the read-only web roles and their grants (etl/09_web_roles.py).
    3. Starts pg_tileserv  -> http://localhost:7800  (map layers as vector tiles)
    4. Starts the API      -> http://localhost:8000  (isochrones and stats; docs at /docs)
    5. Starts the website  -> http://localhost:5173  (once web/package.json exists, M6)

    Logs go to logs\ (git-ignored). Press Ctrl+C to stop all services.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoWeb
#>
param(
    [switch]$NoWeb,         # skip the Vite dev server
    [int]$TilesPort = 7800,
    [int]$ApiPort = 8000
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Logs = Join-Path $Repo 'logs'
New-Item -ItemType Directory -Force $Logs | Out-Null

function Say($msg) { Write-Host "[$(Get-Date -Format HH:mm:ss)] $msg" }

# --- settings from .env ------------------------------------------------------------------
$EnvFile = Join-Path $Repo '.env'
if (-not (Test-Path $EnvFile)) { throw "No .env file. Copy .env.example to .env first (docs/setup.md)." }
$Cfg = @{}
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $Cfg[$Matches[1]] = $Matches[2] }
}
function CfgOr($name, $default) { if ($Cfg.ContainsKey($name) -and $Cfg[$name]) { $Cfg[$name] } else { $default } }
$PgHost = CfgOr 'PGHOST' 'localhost'
$PgPort = CfgOr 'PGPORT' '5432'
$PgDb = CfgOr 'PGDATABASE' 'stl_gis'
$TileServ = CfgOr 'PG_TILESERV' 'A:\gis-tools\pg_tileserv\pg_tileserv.exe'
if (-not (Test-Path $TileServ)) { throw "pg_tileserv not found at $TileServ (set PG_TILESERV in .env)." }

# --- 1. PostgreSQL -------------------------------------------------------------------------
$svc = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($svc -and $svc.Status -ne 'Running') {
    Say "Starting $($svc.Name)..."
    try { Start-Service $svc.Name } catch {
        throw "PostgreSQL is stopped and this shell can't start it. Start '$($svc.Name)' from Services (or an admin shell), then rerun."
    }
}
$ready = $false
for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
    $tcp = New-Object Net.Sockets.TcpClient
    try { $tcp.Connect($PgHost, [int]$PgPort); $ready = $true } catch { Start-Sleep -Milliseconds 500 } finally { $tcp.Close() }
}
if (-not $ready) { throw "PostgreSQL isn't accepting connections on ${PgHost}:$PgPort." }
Say "PostgreSQL is up on ${PgHost}:$PgPort"

# --- 2. Web roles --------------------------------------------------------------------------
Say 'Refreshing web roles and grants'
& $Python (Join-Path $Repo 'etl\09_web_roles.py')
if ($LASTEXITCODE -ne 0) { throw 'etl/09_web_roles.py failed.' }
# It may have just generated passwords, so read .env again.
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $Cfg[$Matches[1]] = $Matches[2] }
}

function Wait-Http($url, $name, $proc, $seconds = 30) {
    for ($i = 0; $i -lt $seconds * 2; $i++) {
        if ($proc.HasExited) { throw "$name exited early; see the log in logs\." }
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $url | Out-Null; return } catch { Start-Sleep -Milliseconds 500 }
    }
    throw "$name didn't answer at $url within $seconds s; see the log in logs\."
}

$procs = @()
try {
    # --- 3. pg_tileserv --------------------------------------------------------------------
    $pw = [Uri]::EscapeDataString($Cfg['TILES_DB_PASSWORD'])
    $env:DATABASE_URL = "postgresql://tiles_reader:$pw@${PgHost}:$PgPort/$PgDb"
    $tiles = Start-Process -FilePath $TileServ -PassThru -NoNewWindow `
        -ArgumentList @('--config', "`"$(Join-Path $Repo 'serving\pg_tileserv.toml')`"") `
        -WorkingDirectory (Split-Path -Parent $TileServ) `
        -RedirectStandardOutput (Join-Path $Logs 'pg_tileserv.out.log') `
        -RedirectStandardError (Join-Path $Logs 'pg_tileserv.log')
    Remove-Item Env:DATABASE_URL
    $procs += $tiles
    Wait-Http "http://127.0.0.1:$TilesPort/index.json" 'pg_tileserv' $tiles
    Say "pg_tileserv  http://localhost:$TilesPort"

    # --- 4. API ----------------------------------------------------------------------------
    $api = Start-Process -FilePath $Python -PassThru -NoNewWindow -WorkingDirectory $Repo `
        -ArgumentList @('-m', 'uvicorn', 'api.main:app', '--host', '127.0.0.1', '--port', "$ApiPort") `
        -RedirectStandardOutput (Join-Path $Logs 'api.out.log') `
        -RedirectStandardError (Join-Path $Logs 'api.log')
    $procs += $api
    Wait-Http "http://127.0.0.1:$ApiPort/health" 'API' $api
    Say "API          http://localhost:$ApiPort  (docs: /docs)"

    # --- 5. Website (M6) -------------------------------------------------------------------
    $web = Join-Path $Repo 'web'
    if (-not $NoWeb -and (Test-Path (Join-Path $web 'package.json'))) {
        $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
        if (-not $npm) { throw 'npm not found; install Node.js or use -NoWeb.' }
        $site = Start-Process -FilePath $npm -PassThru -NoNewWindow -WorkingDirectory $web `
            -ArgumentList @('run', 'dev', '--', '--port', '5173', '--strictPort') `
            -RedirectStandardOutput (Join-Path $Logs 'web.out.log') `
            -RedirectStandardError (Join-Path $Logs 'web.log')
        $procs += $site
        Wait-Http 'http://localhost:5173/' 'website' $site 60
        Say 'Website      http://localhost:5173'
    }

    Say 'All services running. Press Ctrl+C to stop.'
    while ($true) {
        foreach ($p in $procs) {
            if ($p.HasExited) { throw "A service stopped unexpectedly (exit code $($p.ExitCode)); see logs\." }
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) {
            # taskkill /T also stops child processes (npm -> node, uvicorn workers).
            & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
        }
    }
    Say 'Services stopped.'
}
