$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -m PyInstaller --noconfirm --clean --onefile --console --name MemeBot --hidden-import python_socks.async_.asyncio memebot.py
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'telegram.bat') -Destination (Join-Path $PSScriptRoot 'dist\telegram.bat')
Write-Host 'Built dist\MemeBot.exe and dist\telegram.bat. Keep config.ini and state beside the EXE. X API keys are no longer used.'
