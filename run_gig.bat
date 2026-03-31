@echo off
setlocal
set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

set "PACK_ROOT=%BASE_DIR%\gig_ocr_pack_app_dev_clean"
set "APP_DIR=%BASE_DIR%\GIG"
set "APP_EXE=%APP_DIR%\GIG.exe"

if not exist "%PACK_ROOT%\ocr-runtime\python\python.exe" (
  echo [ERRO] OCR embutido nao encontrado em:
  echo        "%PACK_ROOT%"
  echo [DICA] Este portable deve conter a pasta gig_ocr_pack_app_dev_clean.
  pause
  exit /b 1
)

if not exist "%APP_EXE%" (
  echo [ERRO] Nao encontrei o executavel do GIG em:
  echo        "%APP_EXE%"
  pause
  exit /b 1
)

set "GIG_OCR_PACK_ROOT=%PACK_ROOT%"
set "GIG_OCR_PYTHON=%PACK_ROOT%\ocr-runtime\python\python.exe"
set "GIG_OCR_TEMP_ROOT=%PACK_ROOT%\tmp"

pushd "%APP_DIR%"
start "" "%APP_EXE%"
popd
endlocal
exit /b 0
