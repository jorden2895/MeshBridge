$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Dist = Join-Path $ProjectRoot "dist"
$Work = Join-Path $ProjectRoot "build"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup_windows.bat before building a release."
}

& $Python -m pip install -r (Join-Path $ProjectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }

& $Python -m PyInstaller --noconfirm --clean --onefile --console `
    --name MeshTelegramBridge `
    --distpath $Dist --workpath $Work `
    (Join-Path $ProjectRoot "main.py")
if ($LASTEXITCODE -ne 0) { throw "Bridge executable build failed." }

& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MeshTelegramBridgeSettings `
    --distpath $Dist --workpath $Work `
    (Join-Path $ProjectRoot "settings_ui.py")
if ($LASTEXITCODE -ne 0) { throw "Settings executable build failed." }

Write-Output "Release executables created in $Dist"
