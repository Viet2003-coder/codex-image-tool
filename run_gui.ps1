$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (Test-Path ".venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" gui.py
} else {
    Write-Host "Chua co .venv - hay chay .\setup.ps1 truoc." -ForegroundColor Yellow
    python gui.py
}
