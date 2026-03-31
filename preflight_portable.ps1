param(
    [switch]$Quiet
)

$ErrorActionPreference = "SilentlyContinue"

function Write-CheckResult {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )

    if ($Ok) {
        Write-Host "[OK] $Name - $Detail" -ForegroundColor Green
    }
    else {
        Write-Host "[WARN] $Name - $Detail" -ForegroundColor Yellow
    }
}

function Test-VersionCommand {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    if (-not (Test-Path $FilePath)) {
        return $false
    }

    $null = & $FilePath @ArgumentList
    return $LASTEXITCODE -eq 0
}

Write-Host "Preflight GIG (portatil com OCR embutido)" -ForegroundColor Cyan
Write-Host ""

$root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$appExe = Join-Path $root "GIG\GIG.exe"
$packRoot = Join-Path $root "gig_ocr_pack_app_dev_clean"
$ocrPython = Join-Path $packRoot "ocr-runtime\python\python.exe"
$tesseractExe = Join-Path $packRoot "Tesseract-OCR\tesseract.exe"
$qpdfExe = Join-Path $packRoot "qpdf-portable\bin\qpdf.exe"
$ghostscriptExe = Join-Path $packRoot "ghostscript\bin\gswin64c.exe"
if (-not (Test-Path $ghostscriptExe)) {
    $ghostscriptExe = Join-Path $packRoot "ghostscript\bin\gswin32c.exe"
}

$checks = @()
$checks += @{
    Name = "Build local"
    Ok = Test-Path $appExe
    Detail = "GIG\\GIG.exe presente"
    Blocking = $true
}
$checks += @{
    Name = "OCR pack embutido"
    Ok = Test-Path $packRoot
    Detail = "gig_ocr_pack_app_dev_clean presente"
    Blocking = $true
}
$checks += @{
    Name = "Python OCR local"
    Ok = Test-Path $ocrPython
    Detail = "ocr-runtime\\python\\python.exe presente"
    Blocking = $true
}
$checks += @{
    Name = "Tesseract embutido"
    Ok = Test-VersionCommand -FilePath $tesseractExe -ArgumentList @("--version")
    Detail = "Tesseract local responde"
    Blocking = $true
}
$checks += @{
    Name = "qpdf embutido"
    Ok = Test-VersionCommand -FilePath $qpdfExe -ArgumentList @("--version")
    Detail = "qpdf local responde"
    Blocking = $true
}
$checks += @{
    Name = "Ghostscript embutido"
    Ok = Test-VersionCommand -FilePath $ghostscriptExe -ArgumentList @("-v")
    Detail = "Ghostscript local responde"
    Blocking = $true
}
$checks += @{
    Name = "ocrmypdf embutido"
    Ok = (Test-Path $ocrPython) -and ((& $ocrPython -m ocrmypdf --version) -or $LASTEXITCODE -eq 0)
    Detail = "ocrmypdf local responde"
    Blocking = $true
}

foreach ($lang in @("por", "eng", "osd")) {
    $checks += @{
        Name = "Idioma OCR $lang"
        Ok = Test-Path (Join-Path $packRoot "Tesseract-OCR\tessdata\$lang.traineddata")
        Detail = "$lang.traineddata presente"
        Blocking = $true
    }
}

$apiKey = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    $apiKey = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "Machine")
}
$checks += @{
    Name = "GEMINI_API_KEY"
    Ok = -not [string]::IsNullOrWhiteSpace($apiKey)
    Detail = "Necessaria para Elvin/Monk"
    Blocking = $false
}

$allOk = $true
foreach ($check in $checks) {
    Write-CheckResult -Name $check.Name -Ok $check.Ok -Detail $check.Detail
    if (-not $check.Ok -and $check.Blocking) {
        $allOk = $false
    }
}

Write-Host ""
if ($allOk) {
    Write-Host "Preflight concluido: pacote pronto para execucao com OCR embutido." -ForegroundColor Green
    exit 0
}

Write-Host "Preflight concluiu com falhas bloqueantes no pacote." -ForegroundColor Yellow
if (-not $Quiet) {
    Write-Host "Revise os itens acima antes de distribuir este portable."
}
exit 1
