@echo off
setlocal ENABLEDELAYEDEXPANSION
title Configurar GEMINI_API_KEY
echo.
echo ===========================================
echo   Configuracao da chave GEMINI_API_KEY
echo ===========================================
echo.
echo Cole sua chave e pressione ENTER.
echo.
set /p GEMINI_KEY=Chave: 

if "%GEMINI_KEY%"=="" (
  echo.
  echo [ERRO] Chave vazia. Nada foi alterado.
  pause
  exit /b 1
)

setx GEMINI_API_KEY "%GEMINI_KEY%" >nul
if errorlevel 1 (
  echo.
  echo [ERRO] Falha ao salvar a chave.
  pause
  exit /b 1
)

echo.
echo [OK] Chave salva no perfil do usuario.
echo Feche e abra novamente o GIG para aplicar.
pause
exit /b 0
