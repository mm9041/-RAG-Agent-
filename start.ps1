param(
    [string]$Python = "D:\anaconda3\envs\time\python.exe",
    [int]$Port = 8000,
    [string]$HostAddr = "127.0.0.1",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipBuild -and -not (Test-Path "web\dist\index.html")) {
    Write-Host "前端产物不存在，开始构建..." -ForegroundColor Cyan
    Push-Location web
    try {
        if (-not (Test-Path "node_modules")) { npm install --no-fund --no-audit }
        npm run build
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path $Python)) {
    Write-Host "找不到 Python：$Python，请用 -Python 指定解释器路径。" -ForegroundColor Red
    exit 1
}

Write-Host "启动服务：http://${HostAddr}:${Port}" -ForegroundColor Green
& $Python -m server --host $HostAddr --port $Port
