param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $ProjectDir "backend"
$Python = Join-Path $BackendDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment missing. Run setup-ai-trader-v2.ps1 first."
}

$Listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($Listener) {
    $Owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($Listener.OwningProcess)" -ErrorAction SilentlyContinue
    $OwnerDescription = if ($Owner) {
        "$($Owner.Name) (PID $($Owner.ProcessId))"
    } else {
        "PID $($Listener.OwningProcess)"
    }
    throw "Port $Port is already in use by $OwnerDescription. Stop that process or choose -Port <number>."
}

Set-Location $BackendDir
$UvicornArgs = @(
    "-m", "uvicorn",
    "app.main:app",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--no-access-log"
)

if ($Reload) {
    # Windows reload uses a multiprocessing child. Restrict watching to the
    # application package, and always stop it with Ctrl+C so the parent can
    # close the child pipe cleanly.
    $UvicornArgs += @("--reload", "--reload-dir", "app")
    Write-Host "Starting local API with reload on http://127.0.0.1:$Port (stop with Ctrl+C)."
} else {
    # Single-process mode is the stable default and mirrors the Docker
    # container lifecycle without Windows multiprocessing shutdown noise.
    Write-Host "Starting local API on http://127.0.0.1:$Port (stop with Ctrl+C)."
}

& $Python @UvicornArgs
if ($LASTEXITCODE -and $LASTEXITCODE -notin @(130, -1073741510)) {
    throw "The local API exited with code $LASTEXITCODE."
}
