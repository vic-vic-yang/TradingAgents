# TradingAgents Web API — run: .\start-api.ps1   or   .\start-api.ps1 --port 8001
Set-Location $PSScriptRoot
python -m web_api @args
