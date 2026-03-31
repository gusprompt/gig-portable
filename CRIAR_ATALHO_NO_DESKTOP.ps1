param(
    [string]$ShortcutName = "GIG",
    [string]$DestinationDir = [Environment]::GetFolderPath("Desktop"),
    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"

$scriptDir = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$htaLauncher = Join-Path $scriptDir "ABRIR_GIG.hta"
$hiddenLauncher = Join-Path $scriptDir "scripts\launch_gig_portable_hidden.ps1"
$runBat = Join-Path $scriptDir "run_gig.bat"
$exePortable = Join-Path $scriptDir "GIG\GIG.exe"
$customIcon = Join-Path $scriptDir "assets\gig_icon_modern.ico"

if (Test-Path $htaLauncher) {
    $targetPath = $htaLauncher
    $arguments = ""
}
elseif ((Test-Path $hiddenLauncher) -and (Test-Path $runBat)) {
    $targetPath = (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe")
    $arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $hiddenLauncher + '"'
}
elseif (Test-Path $runBat) {
    $targetPath = $runBat
    $arguments = ""
}
else {
    throw "Nao encontrei run_gig.bat."
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

$iconPath = if (Test-Path $customIcon) { $customIcon } elseif (Test-Path $exePortable) { $exePortable } else { $null }

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $scriptDir
if ($iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.Save()

Write-Host "Atalho criado com sucesso: $shortcutPath" -ForegroundColor Green
Write-Host "Destino: $targetPath"
if ($arguments) {
    Write-Host "Argumentos: $arguments"
}
