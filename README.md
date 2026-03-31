# gig_portable

Pacote portatil do GIG para copia e execucao sem instalacao de Python nem dos componentes de OCR.

## Como usar

1. Execute `preflight_portable.bat` para validar requisitos.
2. Clique em `ABRIR_GIG.hta` para abrir o app.
3. Se usar Elvin/Monk, execute `set_gemini_api_key.bat`.
4. Opcionalmente, execute `CRIAR_ATALHO_NO_DESKTOP.bat` para criar atalho no Desktop.

## Estrutura

- `ABRIR_GIG.hta`: launcher clicavel com icone na raiz
- `GIG/`: app compilado
- `gig_ocr_pack_app_dev_clean/`: OCR embutido com `Tesseract`, `Ghostscript`, `qpdf` e runtime local de `ocrmypdf`
- `assets/gig_icon_modern.ico`: icone do atalho
- `CRIAR_ATALHO_NO_DESKTOP.bat`
- `CRIAR_ATALHO_NO_DESKTOP.ps1`
- `run_gig.bat`
- `preflight_portable.bat`
- `preflight_portable.ps1`
- `set_gemini_api_key.bat`
- `scripts/launch_gig_portable_hidden.ps1`

## Observacoes

- `Filtro` funciona offline.
- `Monk` exige `GEMINI_API_KEY`.
- OCR robusto no modo PDF-Texto ja vem embutido neste pacote.
- O launcher do portable configura automaticamente as variaveis `GIG_OCR_PACK_ROOT`, `GIG_OCR_PYTHON` e `GIG_OCR_TEMP_ROOT`.
