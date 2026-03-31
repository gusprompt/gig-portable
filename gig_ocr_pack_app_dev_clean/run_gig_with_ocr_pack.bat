@echo off
setlocal
if "%~1"=="" (
  echo Uso: run_gig_with_ocr_pack.bat ^<CAMINHO_DO_REPO_DO_APP^>
  exit /b 1
)
set "PACK_ROOT=%~dp0"
if "%PACK_ROOT:~-1%"=="\" set "PACK_ROOT=%PACK_ROOT:~0,-1%"
set "REPO_ROOT=%~1"
set "GIG_OCR_PACK_ROOT=%PACK_ROOT%"
set "GIG_OCR_PYTHON=%PACK_ROOT%\ocr-runtime\python\python.exe"
set "GIG_OCR_TEMP_ROOT=%PACK_ROOT%\tmp"
if not exist "%REPO_ROOT%\run_gig.bat" (
  echo [ERRO] Nao encontrei run_gig.bat em "%REPO_ROOT%".
  exit /b 1
)
pushd "%REPO_ROOT%"
call "%REPO_ROOT%\run_gig.bat"
popd
endlocal
