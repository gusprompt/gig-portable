param()

$ErrorActionPreference = "Stop"

function Show-LauncherError([string]$message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message,
        "GIG",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

$scriptDir = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$baseDir = (Resolve-Path (Join-Path $scriptDir "..")).Path
$packRoot = Join-Path $baseDir "gig_ocr_pack_app_dev_clean"
$appExe = Join-Path $baseDir "GIG\GIG.exe"

if (-not (Test-Path (Join-Path $packRoot "ocr-runtime\python\python.exe"))) {
    Show-LauncherError (
        "Nao encontrei o OCR embutido deste pacote.`n`n" +
        "Esperado em:`n" + $packRoot
    )
    exit 1
}

if (-not (Test-Path $appExe)) {
    Show-LauncherError (
        "Nao encontrei o executavel do GIG.`n`n" +
        "Esperado em:`n" + $appExe
    )
    exit 1
}

$env:GIG_OCR_PACK_ROOT = $packRoot
$env:GIG_OCR_PYTHON = Join-Path $packRoot "ocr-runtime\python\python.exe"
$env:GIG_OCR_TEMP_ROOT = Join-Path $packRoot "tmp"

try {
    Start-Process -FilePath $appExe -WorkingDirectory (Split-Path -Parent $appExe) | Out-Null
    exit 0
}
catch {
    Show-LauncherError ("Nao foi possivel iniciar o GIG.`n`n" + $_.Exception.Message)
    exit 1
}
