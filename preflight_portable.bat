@echo off
setlocal
set "BASE_DIR=%~dp0"
set "PS1=%BASE_DIR%preflight_portable.ps1"

if not exist "%PS1%" (
  set "PS1=%BASE_DIR%scripts\preflight_portable.ps1"
)

if not exist "%PS1%" (
  echo [ERRO] Nao encontrei preflight_portable.ps1.
  pause
  exit /b 1
)

powershell -ExecutionPolicy Bypass -File "%PS1%"
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
  echo Preflight concluido com sucesso.
) else (
  echo Preflight concluiu com alertas bloqueantes.
)
pause
exit /b %CODE%
