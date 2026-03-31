@echo off
setlocal
set "PACK_ROOT=%~dp0"
if "%PACK_ROOT:~-1%"=="\" set "PACK_ROOT=%PACK_ROOT:~0,-1%"
set "GIG_OCR_PACK_ROOT=%PACK_ROOT%"
set "GIG_OCR_PYTHON=%PACK_ROOT%\ocr-runtime\python\python.exe"
set "GIG_OCR_TEMP_ROOT=%PACK_ROOT%\tmp"
echo [INFO] Verificando OCR pack...
if not exist "%GIG_OCR_PYTHON%" (
  echo [ERRO] Python OCR nao encontrado: %GIG_OCR_PYTHON%
  exit /b 1
)
for %%F in (por eng osd) do (
  if not exist "%PACK_ROOT%\Tesseract-OCR\tessdata\%%F.traineddata" (
    echo [ERRO] traineddata ausente no pack: %%F.traineddata
    exit /b 1
  )
)
"%PACK_ROOT%\Tesseract-OCR\tesseract.exe" --version
"%PACK_ROOT%\qpdf-portable\bin\qpdf.exe" --version
if exist "%PACK_ROOT%\ghostscript\bin\gswin64c.exe" (
  "%PACK_ROOT%\ghostscript\bin\gswin64c.exe" -v
) else (
  "%PACK_ROOT%\ghostscript\bin\gswin32c.exe" -v
)
"%GIG_OCR_PYTHON%" -m ocrmypdf --version
endlocal
