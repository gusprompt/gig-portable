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

Write-Host "Preflight GIG (portatil)" -ForegroundColor Cyan
Write-Host ""

$checks = @()

$exeDir = Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)
$parentDir = Resolve-Path (Join-Path $exeDir "..")
$appExeCandidates = @(
    (Join-Path $exeDir "GIG\GIG.exe"),
    (Join-Path $exeDir "dist\GIG\GIG.exe"),
    (Join-Path $parentDir "GIG\GIG.exe"),
    (Join-Path $parentDir "dist\GIG\GIG.exe")
)
$appExe = $null
foreach ($candidate in $appExeCandidates) {
    if (Test-Path $candidate) {
        $appExe = $candidate
        break
    }
}

$checks += @{
    Name = "Build local"
    Ok = -not [string]::IsNullOrWhiteSpace($appExe)
    Detail = "GIG.exe em GIG\\ ou dist\\GIG"
}

$apiKey = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    $apiKey = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "Machine")
}
$checks += @{
    Name = "GEMINI_API_KEY"
    Ok = -not [string]::IsNullOrWhiteSpace($apiKey)
    Detail = "Necessaria para Elvin/Monk"
}

$tesseract = Get-Command tesseract
$checks += @{
    Name = "Tesseract (opcional)"
    Ok = $null -ne $tesseract
    Detail = "Recomendado para OCR robusto no modo PDF-Texto"
}

$gs = Get-Command gswin64c
if ($null -eq $gs) { $gs = Get-Command gswin32c }
$checks += @{
    Name = "Ghostscript (opcional)"
    Ok = $null -ne $gs
    Detail = "Usado por OCRmyPDF"
}

$qpdf = Get-Command qpdf
$checks += @{
    Name = "qpdf (opcional)"
    Ok = $null -ne $qpdf
    Detail = "Usado por OCRmyPDF"
}

$allOk = $true
foreach ($c in $checks) {
    Write-CheckResult -Name $c.Name -Ok $c.Ok -Detail $c.Detail
    if (-not $c.Ok -and $c.Name -in @("Build local", "GEMINI_API_KEY")) {
        $allOk = $false
    }
}

Write-Host ""
if ($allOk) {
    Write-Host "Preflight concluido: pronto para execucao." -ForegroundColor Green
    exit 0
}

Write-Host "Preflight concluido com alertas bloqueantes." -ForegroundColor Yellow
if (-not $Quiet) {
    Write-Host "Ajuste os itens acima e execute novamente."
}
exit 1
