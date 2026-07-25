param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location "$ProjectDir\backend"

if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    throw "Virtual environment missing. Run setup-ai-trader-v2.ps1 first."
}

. .\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --port $Port
