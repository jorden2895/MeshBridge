$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Dist = Join-Path $ProjectRoot "dist"
$Work = Join-Path $ProjectRoot "build"
$VersionSource = Join-Path $ProjectRoot "version.py"
$BridgeVersionInfo = Join-Path $Work "bridge_version_info.txt"
$SettingsVersionInfo = Join-Path $Work "settings_version_info.txt"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup_windows.bat before building a release."
}

$VersionMatch = Select-String -LiteralPath $VersionSource -Pattern '^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"$'
if (-not $VersionMatch) { throw "Could not read version.py." }
$AppVersion = $VersionMatch.Matches[0].Groups[1].Value
$VersionParts = $AppVersion.Split('.')
$FileVersion = "$($VersionParts[0]), $($VersionParts[1]), $($VersionParts[2]), 0"

if ($env:GITHUB_REF_TYPE -eq "tag" -and $env:GITHUB_REF_NAME -ne "v$AppVersion") {
    throw "Git tag $($env:GITHUB_REF_NAME) does not match version.py v$AppVersion."
}

function Write-VersionInfo {
    param(
        [string]$Path,
        [string]$Description,
        [string]$InternalName,
        [string]$OriginalFilename
    )
    @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($FileVersion), prodvers=($FileVersion), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'MeshTelegram Bridge'),
    StringStruct('FileDescription', '$Description'),
    StringStruct('FileVersion', '$AppVersion'),
    StringStruct('InternalName', '$InternalName'),
    StringStruct('OriginalFilename', '$OriginalFilename'),
    StringStruct('ProductName', 'MeshTelegram Bridge'),
    StringStruct('ProductVersion', '$AppVersion')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -LiteralPath $Path -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path $Work | Out-Null
Write-VersionInfo $BridgeVersionInfo "MeshTelegram Bridge" "MeshTelegramBridge" "MeshTelegramBridge.exe"
Write-VersionInfo $SettingsVersionInfo "MeshTelegram Bridge Settings" "MeshTelegramBridgeSettings" "MeshTelegramBridgeSettings.exe"

& $Python -m pip install -r (Join-Path $ProjectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }

& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MeshTelegramBridge `
    --version-file $BridgeVersionInfo `
    --distpath $Dist --workpath $Work `
    (Join-Path $ProjectRoot "main.py")
if ($LASTEXITCODE -ne 0) { throw "Bridge executable build failed." }

& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MeshTelegramBridgeSettings `
    --version-file $SettingsVersionInfo `
    --distpath $Dist --workpath $Work `
    (Join-Path $ProjectRoot "settings_ui.py")
if ($LASTEXITCODE -ne 0) { throw "Settings executable build failed." }

Write-Output "Release executables created in $Dist"
