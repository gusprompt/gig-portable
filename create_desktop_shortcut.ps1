param(
    [string]$ShortcutName = "GIG",
    [string]$DestinationDir = [Environment]::GetFolderPath("Desktop"),
    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"

$scriptDir = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$baseCandidates = @(
    $scriptDir,
    (Resolve-Path (Join-Path $scriptDir "..")).Path
)

$baseDir = $null
$targetPath = $null
$iconPath = $null

foreach ($base in $baseCandidates) {
    $runBat = Join-Path $base "run_gig.bat"
    $runTestMode = Join-Path $base "scripts\run_test_mode.cmd"
    $exePortable = Join-Path $base "GIG\GIG.exe"
    $exeProject = Join-Path $base "dist\GIG\GIG.exe"
    $customIconV4 = Join-Path $base "assets\gig_icon_v4.ico"
    $customIcon = Join-Path $base "assets\gig_icon_modern.ico"

    if (Test-Path $runBat) {
        $baseDir = $base
        # Preferencia: abrir a versao atual do codigo-fonte (modo teste), quando disponivel.
        if (Test-Path $runTestMode) {
            $targetPath = $runTestMode
        } else {
            $targetPath = $runBat
        }
        if (Test-Path $customIconV4) { $iconPath = $customIconV4 }
        elseif (Test-Path $customIcon) { $iconPath = $customIcon }
        elseif (Test-Path $exePortable) { $iconPath = $exePortable }
        elseif (Test-Path $exeProject) { $iconPath = $exeProject }
        break
    }
}

if (-not $targetPath) {
    throw "Nao encontrei run_gig.bat (nem no diretorio atual nem no pai)."
}

if (-not (Test-Path $DestinationDir)) {
    New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
}

$shortcutPath = Join-Path $DestinationDir ("{0}.lnk" -f $ShortcutName)
if ((Test-Path $shortcutPath) -and (-not $Overwrite)) {
    Write-Host "Atalho ja existe: $shortcutPath"
    Write-Host "Use -Overwrite para substituir."
    exit 0
}

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.WorkingDirectory = (Split-Path -Parent $targetPath)
if ($iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.Save()

Write-Host "Atalho criado com sucesso: $shortcutPath" -ForegroundColor Green
Write-Host "Destino: $targetPath"
