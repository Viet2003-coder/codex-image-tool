param(
    [switch]$SkipFfmpegCheck
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "==> Tao virtualenv (.venv)..." -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

Write-Host "==> Cai dependencies tu requirements.txt..." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not $SkipFfmpegCheck) {
    Write-Host "==> Kiem tra ffmpeg..." -ForegroundColor Cyan
    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ffmpeg) {
        Write-Host "    Khong tim thay ffmpeg. Dang tool KHONG dung duoc tinh nang tao video neu thieu ffmpeg." -ForegroundColor Yellow
        Write-Host "    Cai bang: winget install Gyan.FFmpeg" -ForegroundColor Yellow
    } else {
        Write-Host "    OK: $($ffmpeg.Source)" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "==> Xong! Chay GUI bang:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\python.exe gui.py"
Write-Host "    (hoac bam run_gui.ps1)"
Write-Host ""
Write-Host "Ghi chu: tinh nang gen anh qua Codex can cai Codex CLI va chay 'codex login' truoc." -ForegroundColor Yellow
