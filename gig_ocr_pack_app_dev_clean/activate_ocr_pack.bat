@echo off
setlocal
set "PACK_ROOT=%~dp0"
if "%PACK_ROOT:~-1%"=="\" set "PACK_ROOT=%PACK_ROOT:~0,-1%"
set "GIG_OCR_PACK_ROOT=%PACK_ROOT%"
set "GIG_OCR_PYTHON=%PACK_ROOT%\ocr-runtime\python\python.exe"
set "GIG_OCR_TEMP_ROOT=%PACK_ROOT%\tmp"
echo OCR pack ativo para esta sessao.
echo GIG_OCR_PACK_ROOT=%GIG_OCR_PACK_ROOT%
echo GIG_OCR_PYTHON=%GIG_OCR_PYTHON%
cmd /k
