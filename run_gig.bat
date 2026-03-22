@echo off
setlocal
set "BASE_DIR=%~dp0"
set "APP_DIR=%BASE_DIR%GIG"
if not exist "%APP_DIR%\GIG.exe" (
  set "APP_DIR=%BASE_DIR%dist\GIG"
)
set "APP_EXE=%APP_DIR%\GIG.exe"
set "VENV_PY=%BASE_DIR%\.venv\Scripts\python.exe"

if exist "%APP_EXE%" (
  pushd "%APP_DIR%"
  start "" "%APP_EXE%"
  popd
  endlocal
  exit /b 0
)

echo [AVISO] Nao encontrei o executavel empacotado. Iniciando pelo codigo-fonte...

if exist "%VENV_PY%" (
  pushd "%BASE_DIR%"
  start "" "%VENV_PY%" main.py
  popd
  endlocal
  exit /b 0
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  pushd "%BASE_DIR%"
  start "" py -3 main.py
  popd
  endlocal
  exit /b 0
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  pushd "%BASE_DIR%"
  start "" python main.py
  popd
  endlocal
  exit /b 0
)

echo [ERRO] Nao foi possivel iniciar o GIG.
echo [DICA] Gere o pacote com:
echo        powershell -ExecutionPolicy Bypass -File "%BASE_DIR%scripts\build_portable.ps1"
echo [DICA] Ou crie/ative um Python em "%BASE_DIR%\.venv".
pause
endlocal
exit /b 1
