@echo off
title Load Tester - k6
cd /d "%~dp0"

echo === Load Tester ===
echo.
echo [1/5] Buscando PowerShell...
where pwsh >nul 2>&1
if %errorlevel% equ 0 (
    set "PS=pwsh"
) else (
    set "PS=powershell"
)

echo [2/5] Verificando Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python no encontrado. Instalalo desde https://www.python.org/downloads/
    echo Asegurate de marcar "Add Python to PATH"
    pause
    exit /b 1
)

python --version

echo [3/5] Ejecutando setup...
%PS% -NoProfile -ExecutionPolicy Bypass -File "setup.ps1"

if %errorlevel% neq 0 (
    echo.
    echo El servidor se detuvo con codigo %errorlevel%
    pause
)
