<#
.SYNOPSIS
  Lanzador rápido del Load Tester
.DESCRIPTION
  Ejecuta setup.ps1 que instala dependencias, valida y arranca el servidor.
  Si setup.ps1 no existe, arranca directamente server.py con el entorno virtual.
#>

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SetupScript = Join-Path $ProjectRoot "setup.ps1"
$VenvPython = Join-Path $ProjectRoot ".venv" "Scripts" "python.exe"
$ServerScript = Join-Path $ProjectRoot "server.py"

# Preferir PowerShell 7 si está disponible
$pwsh = if (Get-Command "pwsh" -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }

if (Test-Path $SetupScript) {
    Write-Host "=== Load Tester ===" -ForegroundColor Cyan
    Write-Host "Ejecutando setup completo..." -ForegroundColor Yellow
    & $pwsh -NoProfile -ExecutionPolicy Bypass -File $SetupScript
} elseif (Test-Path $VenvPython) {
    Write-Host "=== Load Tester ===" -ForegroundColor Cyan
    Write-Host "Iniciando servidor..." -ForegroundColor Yellow
    Start-Process "http://127.0.0.1:5000"
    & $VenvPython $ServerScript
} else {
    Write-Host "=== Load Tester ===" -ForegroundColor Cyan
    Write-Host "Ejecutando setup inicial..." -ForegroundColor Yellow
    & $pwsh -NoProfile -ExecutionPolicy Bypass -Command "
        python -m venv '$ProjectRoot\.venv'
        & '$VenvPython' -m pip install -q -r '$ProjectRoot\requirements.txt'
        Start-Process 'http://127.0.0.1:5000'
        & '$VenvPython' '$ServerScript'
    "
}
