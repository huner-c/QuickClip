[CmdletBinding()]
param(
    [string]$TargetExe
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($TargetExe)) {
    $TargetExe = Join-Path $RepoRoot "dist\QuickClip\QuickClip.exe"
}

if (-not (Test-Path $TargetExe)) {
    throw "Target executable not found: $TargetExe. Run scripts\build_exe.ps1 first."
}

$ResolvedTarget = (Resolve-Path $TargetExe).Path
$WorkingDir = Split-Path -Parent $ResolvedTarget

$DesktopDir = [Environment]::GetFolderPath("Desktop")
$ProgramsDir = [Environment]::GetFolderPath("Programs")

$DesktopLnk = Join-Path $DesktopDir "QuickClip.lnk"
$ProgramsLnk = Join-Path $ProgramsDir "QuickClip.lnk"

$Shell = New-Object -ComObject WScript.Shell
try {
    foreach ($LnkPath in @($DesktopLnk, $ProgramsLnk)) {
        $ParentDir = Split-Path -Parent $LnkPath
        if (-not (Test-Path $ParentDir)) {
            New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
        }
        $Shortcut = $Shell.CreateShortcut($LnkPath)
        $Shortcut.TargetPath = $ResolvedTarget
        $Shortcut.WorkingDirectory = $WorkingDir
        $Shortcut.IconLocation = "$ResolvedTarget,0"
        $Shortcut.Description = "QuickClip — Instant Screen Recorder & Trimmer"
        $Shortcut.Save()
        Write-Host "Created shortcut: $LnkPath -> $ResolvedTarget"
    }
}
finally {
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($Shell)
}
