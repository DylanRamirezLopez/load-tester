<#
.SYNOPSIS
  Load Tester — Instalador y ejecutor automático
.DESCRIPTION
  Verifica requisitos, instala dependencias, valida el proyecto y arranca el servidor.
  Compatible con Windows, Linux y macOS (PowerShell 7+ o PowerShell Core).
#>

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Host.UI.RawUI.WindowTitle = "Load Tester - Setup"

function Write-Step($Message) {
    Write-Host "`n[LOAD TESTER] $Message" -ForegroundColor Cyan
}

function Write-OK($Message) {
    Write-Host "  ✔ $Message" -ForegroundColor Green
}

function Write-Warn($Message) {
    Write-Host "  ⚠ $Message" -ForegroundColor Yellow
}

function Write-Fail($Message) {
    Write-Host "  ✘ $Message" -ForegroundColor Red
}

function Test-Command($Command) {
    try {
        if ($IsWindows -or $PSVersionTable.PSEdition -eq 'Desktop') {
            $null = Get-Command $Command -ErrorAction Stop
        } else {
            $null = Get-Command $Command -ErrorAction Stop
        }
        return $true
    } catch {
        return $false
    }
}

# ============================================================
# PASO 1: BANNER
# ============================================================
Clear-Host
Write-Host @"

   ╔═══════════════════════════════════════════════════╗
   ║              ⚡  LOAD TESTER  ⚡                  ║
   ║   Herramienta de pruebas de carga con k6 + Flask  ║
   ╚═══════════════════════════════════════════════════╝

"@ -ForegroundColor Magenta

# ============================================================
# PASO 2: VERIFICAR PYTHON
# ============================================================
Write-Step "Paso 1/6: Verificando Python..."

$python = $null
foreach ($cmd in @("python3", "python")) {
    if (Test-Command $cmd) {
        $python = $cmd
        break
    }
}

if (-not $python) {
    Write-Fail "Python no instalado"
    Write-Host "  Descárgalo desde: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  Asegúrate de marcar 'Add Python to PATH' durante la instalación." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Presiona Enter después de instalar Python para reintentar..."
    & $PSCommandPath
    exit
}

$pyVersion = & $python --version 2>&1
Write-OK "$pyVersion detectado"

# ============================================================
# PASO 3: VERIFICAR / INSTALAR K6
# ============================================================
Write-Step "Paso 2/6: Verificando k6..."

$k6Paths = @(
    $env:K6_PATH,
    "C:\Program Files\k6\k6.exe",
    "$env:LOCALAPPDATA\Programs\k6\k6.exe",
    "$env:USERPROFILE\k6\k6.exe"
)

$k6Found = $false
foreach ($kp in $k6Paths) {
    if ($kp -and (Test-Path $kp -PathType Leaf)) {
        $k6Found = $true
        $k6Exe = $kp
        break
    }
}

if (-not $k6Found) {
    $k6Exe = (Get-Command "k6" -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($k6Exe) { $k6Found = $true }
}

if ($k6Found) {
    $k6Ver = & $k6Exe version 2>&1
    Write-OK "k6 detectado: $k6Ver"
    $env:K6_PATH = $k6Exe
} else {
    Write-Warn "k6 no está instalado"
    Write-Host "  ¿Quieres que lo instale automáticamente?" -ForegroundColor Yellow

    $choice = Read-Host "  [S]í / [N]o (default: S)"
    if ($choice -eq "" -or $choice -eq "S" -or $choice -eq "s") {
        Write-Host "  Descargando k6..." -ForegroundColor Yellow
        try {
            if (Test-Command "winget") {
                winget install k6 --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
                $k6Exe = "k6"
                $env:K6_PATH = "k6"
                Write-OK "k6 instalado via winget"
            } elseif (Test-Command "choco") {
                choco install k6 -y 2>&1 | Out-Null
                $k6Exe = "k6"
                $env:K6_PATH = "k6"
                Write-OK "k6 instalado via chocolatey"
            } else {
                # Download manually
                $k6Dir = "$env:LOCALAPPDATA\Programs\k6"
                New-Item -ItemType Directory -Path $k6Dir -Force | Out-Null
                $k6Zip = "$env:TEMP\k6.zip"
                Write-Host "  Descargando desde dl.k6.io..." -ForegroundColor Yellow
                Invoke-WebRequest -Uri "https://dl.k6.io/msi/k6-latest-amd64.msi" -OutFile "$env:TEMP\k6.msi" -UseBasicParsing
                Start-Process msiexec -ArgumentList "/i $env:TEMP\k6.msi /quiet /norestart" -Wait
                $k6Exe = "C:\Program Files\k6\k6.exe"
                $env:K6_PATH = $k6Exe
                Write-OK "k6 instalado manualmente"
            }
        } catch {
            Write-Fail "No se pudo instalar k6 automáticamente"
            Write-Host "  Descárgalo manualmente de: https://k6.io/docs/getting-started/installation/" -ForegroundColor Yellow
            Write-Host "  Luego ejecuta este script de nuevo." -ForegroundColor Yellow
            Read-Host "Presiona Enter para continuar de todas formas..."
        }
    } else {
        Write-Warn "Continuando sin k6 (no podrás ejecutar pruebas)"
    }
}

# ============================================================
# PASO 4: CREAR ENTORNO VIRTUAL E INSTALAR DEPENDENCIAS
# ============================================================
Write-Step "Paso 3/6: Instalando dependencias Python..."

$venvDir = Join-Path $ProjectRoot ".venv"
$requirementsPath = Join-Path $ProjectRoot "requirements.txt"

if (-not (Test-Path $requirementsPath)) {
    Write-Fail "requirements.txt no encontrado en $requirementsPath"
    exit 1
}

# Create venv if not exists
if (-not (Test-Path $venvDir)) {
    Write-Host "  Creando entorno virtual..." -ForegroundColor Yellow
    & $python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Error al crear el entorno virtual"
        exit 1
    }
    Write-OK "Entorno virtual creado en .venv"
}

# Activate and install
if ($IsWindows -or $PSVersionTable.PSEdition -eq 'Desktop') {
    $pip = Join-Path $venvDir "Scripts\pip.exe"
    $pythonVenv = Join-Path $venvDir "Scripts\python.exe"
} else {
    $pip = Join-Path $venvDir "bin\pip"
    $pythonVenv = Join-Path $venvDir "bin\python"
}

if (-not (Test-Path $pip)) {
    Write-Fail "pip no encontrado en el entorno virtual"
    exit 1
}

Write-Host "  Instalando dependencias..." -ForegroundColor Yellow
$pipOutput = & $pip install -q -r $requirementsPath 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Error instalando dependencias"
    Write-Host "  $pipOutput" -ForegroundColor Red
    exit 1
}
Write-OK "Dependencias instaladas correctamente"

# ============================================================
# PASO 5: VALIDAR PROYECTO
# ============================================================
Write-Step "Paso 4/6: Validando proyecto..."

$requiredFiles = @(
    "server.py",
    "templates/index.html",
    "requirements.txt",
    "LICENSE",
    "README.md"
)

$allGood = $true
foreach ($file in $requiredFiles) {
    $path = Join-Path $ProjectRoot $file
    if (Test-Path $path) {
        Write-OK "$file presente"
    } else {
        Write-Fail "$file NO ENCONTRADO"
        $allGood = $false
    }
}

# Validate Python syntax
Write-Host "  Validando sintaxis Python..." -ForegroundColor Yellow
$syntaxOutput = & $pythonVenv -c "
import ast, sys
try:
    ast.parse(open('$ProjectRoot\\server.py', encoding='utf-8').read())
    sys.exit(0)
except SyntaxError as e:
    print(f'Error de sintaxis: {e}')
    sys.exit(1)
" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "Sintaxis Python correcta"
} else {
    Write-Fail "Error de sintaxis en server.py"
    Write-Host "  $syntaxOutput" -ForegroundColor Red
    $allGood = $false
}

# Create required directories
foreach ($dir in @("scripts", "results", "history", "responses")) {
    $d = Join-Path $ProjectRoot $dir
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-OK "Directorio $dir/ creado"
    }
}

if ($allGood) {
    Write-OK "Proyecto validado correctamente"
} else {
    Write-Fail "Hay problemas en el proyecto. Revisa los errores arriba."
    Read-Host "Presiona Enter para continuar de todas formas..."
}

# ============================================================
# PASO 6: PREFERENCIAS
# ============================================================
Write-Step "Paso 5/6: Configuración adicional..."

# Check if port is available
$port = 5000
$inUse = $false
try {
    $conn = [System.Net.Sockets.TcpClient]::new()
    $conn.ConnectAsync("127.0.0.1", $port).Wait(500)
    if ($conn.Connected) {
        $inUse = $true
        $conn.Close()
    }
} catch {}

if ($inUse) {
    Write-Warn "El puerto $port ya está en uso"
    $newPort = Read-Host "  Ingresa otro puerto (default: 5001)"
    if ($newPort -match "^\d+$") { $port = [int]$newPort }
    else { $port = 5001 }
}

$env:PORT = $port

$autoOpen = $true
$openChoice = Read-Host "  ¿Abrir navegador automáticamente? [S]/N"
if ($openChoice -eq "N" -or $openChoice -eq "n") { $autoOpen = $false }

# ============================================================
# EJECUTAR
# ============================================================
Write-Step "Paso 6/6: Iniciando servidor..."

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║     🚀  LOAD TESTER LISTO PARA USAR         ║" -ForegroundColor Green
Write-Host "  ╠══════════════════════════════════════════════╣" -ForegroundColor Green
Write-Host "  ║  URL:    http://127.0.0.1:$port              " -ForegroundColor White
Write-Host "  ║  Puerto: $port                              " -ForegroundColor White
Write-Host "  ║  API:    http://127.0.0.1:$port/api/docs     " -ForegroundColor White
Write-Host "  ║  K6:     $k6Exe " -ForegroundColor White
Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

if ($autoOpen) {
    try {
        Start-Process "http://127.0.0.1:$port"
    } catch {}
}

# Run server with the venv python
& $pythonVenv "$ProjectRoot\server.py"

# ============================================================
# CLEANUP ON EXIT
# ============================================================
Write-Host ""
Write-Host "[LOAD TESTER] Servidor detenido." -ForegroundColor Cyan
Read-Host "Presiona Enter para salir"
