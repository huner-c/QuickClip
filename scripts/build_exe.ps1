[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SpecPath = Join-Path $RepoRoot "QuickClip.spec"
$IconPath = Join-Path $RepoRoot "assets\quickclip.ico"

if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment Python not found at: $VenvPython"
}

if (-not (Test-Path $IconPath)) {
    Write-Host "Generating icon assets..."
    & $VenvPython (Join-Path $RepoRoot "scripts\generate_icon.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Icon generation failed with exit code $LASTEXITCODE"
    }
}

Push-Location $RepoRoot
try {
    Write-Host "Building QuickClip standalone bundle via PyInstaller..."
    & $VenvPython -m PyInstaller --clean --noconfirm $SpecPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$ExePath = Join-Path $RepoRoot "dist\QuickClip\QuickClip.exe"
$BundledFfmpeg = Join-Path $RepoRoot "dist\QuickClip\_internal\bin\ffmpeg.exe"
$BundledFfprobe = Join-Path $RepoRoot "dist\QuickClip\_internal\bin\ffprobe.exe"
$BundledIco = Join-Path $RepoRoot "dist\QuickClip\_internal\assets\quickclip.ico"

foreach ($RequiredFile in @($ExePath, $BundledFfmpeg, $BundledFfprobe, $BundledIco)) {
    if (-not (Test-Path $RequiredFile)) {
        throw "Expected build artifact missing: $RequiredFile"
    }
}

Write-Host "Build verified:"
Write-Host "  EXE:     $ExePath"
Write-Host "  FFmpeg:  $BundledFfmpeg"
Write-Host "  FFprobe: $BundledFfprobe"
Write-Host "  Icon:    $BundledIco"
