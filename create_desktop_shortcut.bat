@echo off
setlocal
set "BASE_DIR=%~dp0"
set "PS1=%BASE_DIR%scripts\create_desktop_shortcut.ps1"

if exist "%BASE_DIR%create_desktop_shortcut.ps1" (
  set "PS1=%BASE_DIR%create_desktop_shortcut.ps1"
)

if not exist "%PS1%" (
  echo [ERRO] Nao encontrei create_desktop_shortcut.ps1.
  pause
  exit /b 1
)

powershell -ExecutionPolicy Bypass -File "%PS1%" -Overwrite
if errorlevel 1 (
  echo [ERRO] Falha ao criar atalho.
  pause
  exit /b 1
)

echo Atalho criado.
endlocal
