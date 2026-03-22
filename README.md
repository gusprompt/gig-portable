# gig_portable

Pacote portatil do GIG para copia e execucao sem instalacao do Python.

## Como usar

1. Execute `preflight_portable.bat` para validar requisitos.
2. Execute `run_gig.bat` para abrir o app.
3. Se usar Elvin/Monk, execute `set_gemini_api_key.bat`.
4. Opcionalmente, execute `create_desktop_shortcut.bat` para criar atalho no Desktop.

## Estrutura

- `GIG/`: app compilado
- `assets/gig_icon_modern.ico`: icone do atalho
- `run_gig.bat`
- `preflight_portable.bat`
- `preflight_portable.ps1`
- `set_gemini_api_key.bat`
- `create_desktop_shortcut.bat`
- `create_desktop_shortcut.ps1`

## Observacoes

- `Filtro` funciona offline.
- `Monk` exige `GEMINI_API_KEY`.
- OCR robusto no modo PDF-Texto pode exigir Tesseract, Ghostscript e qpdf no sistema.
