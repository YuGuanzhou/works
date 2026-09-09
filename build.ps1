$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Run start.bat first to create the Python environment.'
}
& $taskPython -m pip install 'pyinstaller==6.22.2'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
$taskVersion = & $taskPython -c 'from jobs_app import __version__; print(__version__)'
$taskName = "Get-Jobs-Python-$taskVersion"
& $taskPython -m PyInstaller --noconfirm --onefile --name $taskName --add-data 'jobs_app/static;jobs_app/static' --add-data 'LICENSE;.' --add-data 'THIRD_PARTY_NOTICES.md;.' --collect-all playwright --collect-all patchright run.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Get-Item -LiteralPath (Join-Path 'dist' "$taskName.exe") | Select-Object FullName, Length
