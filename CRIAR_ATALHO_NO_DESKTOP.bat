@echo off
setlocal
set "PS1=%~dp0CRIAR_ATALHO_NO_DESKTOP.ps1"

if not exist "%PS1%" (
  echo [ERRO] Nao encontrei CRIAR_ATALHO_NO_DESKTOP.ps1.
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
exit /b %ERRORLEVEL%
