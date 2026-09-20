param([Parameter(Mandatory)][string]$Script)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = $root
Push-Location $root
try {
    python '-B' $Script
    if (-not $?) { exit 1 }
} finally { Pop-Location }
