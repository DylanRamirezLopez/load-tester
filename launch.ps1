$port = 5000
$folder = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Load Tester ===" -ForegroundColor Cyan
Write-Host "Iniciando servidor en http://127.0.0.1:$port" -ForegroundColor Yellow
Write-Host ""

Start-Process "http://127.0.0.1:$port"

python "$folder\server.py"
