# Milestone 1 check: tool versions, plus the stl_gis database with PostGIS and pgRouting.
# Run from A:\gis-portfolio:
#   powershell -ExecutionPolicy Bypass -File scripts\m1-verify.ps1
# Output goes to data\m1-verify.txt (git-ignored). The password is never printed.

$repo  = Split-Path -Parent $PSScriptRoot
$out   = Join-Path $repo 'data\m1-verify.txt'
$pgbin = 'C:\Program Files\PostgreSQL\18\bin'
$psql  = Join-Path $pgbin 'psql.exe'
$osm2pgsql   = 'A:\gis-tools\osm2pgsql\osm2pgsql-bin\osm2pgsql.exe'
$pg_tileserv = 'A:\gis-tools\pg_tileserv\pg_tileserv.exe'

# Load PG* settings from .env (repo root first, then A:\gis-portfolio.env).
$envFile = @((Join-Path $repo '.env'), 'A:\gis-portfolio.env') |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $envFile) { Write-Host 'No .env found in A:\gis-portfolio. Copy .env.example to .env first.'; exit 1 }
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') {
        $v = $Matches[2].Trim('"').Trim("'")
        Set-Item -Path "env:$($Matches[1])" -Value $v
    }
}
$db = if ($env:PGDATABASE) { $env:PGDATABASE } else { 'stl_gis' }

$log = New-Object System.Collections.Generic.List[string]
function Add([string]$s) { $log.Add($s); Write-Host $s }
function Run([string]$title, [scriptblock]$cmd) {
    Add ''; Add "## $title"
    try { & $cmd 2>&1 | ForEach-Object { Add "$_" } } catch { Add "ERROR: $_" }
    Add "exit: $LASTEXITCODE"
}

Add "# M1 verify  $(Get-Date -Format s)"
Add "env file: $envFile   host=$env:PGHOST port=$env:PGPORT user=$env:PGUSER db=$db"

Run 'osm2pgsql --version'   { & $osm2pgsql --version }
Run 'pg_tileserv --version' { & $pg_tileserv --version }
Run 'ogr2ogr --version' {
    $o = Get-Command ogr2ogr -ErrorAction SilentlyContinue
    if ($o) { & $o.Source --version }
    elseif (Test-Path "$pgbin\ogr2ogr.exe") { & "$pgbin\ogr2ogr.exe" --version }
    else { 'ogr2ogr not found on PATH or in PostgreSQL bin' }
}
Run 'psql --version' { & $psql --version }

Run "create database $db (if missing)" {
    $exists = & $psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'"
    if ("$exists".Trim() -eq '1') { "database $db already exists" }
    else { & $psql -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $db" }
}
Run 'create extensions' {
    & $psql -d $db -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS postgis;' -c 'CREATE EXTENSION IF NOT EXISTS pgrouting;'
}
Run 'versions in stl_gis' {
    & $psql -d $db -c 'SELECT version();' `
        -c 'SELECT extname, extversion FROM pg_extension ORDER BY extname;' `
        -c 'SELECT postgis_full_version();' `
        -c 'SELECT pgr_version();'
}
Run 'python venv packages' {
    & (Join-Path $repo '.venv\Scripts\python.exe') -m pip list 2>$null |
        Select-String -Pattern '^(psycopg|fastapi|uvicorn|requests|python-dotenv)\s'
}
Run 'git status' { git -C $repo status --short --branch }

$log | Set-Content -Path $out -Encoding UTF8
Write-Host ''
Write-Host "Saved to $out"
