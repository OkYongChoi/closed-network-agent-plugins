$ErrorActionPreference = "Stop"
& python (Join-Path $PSScriptRoot "create-plugin") @args
exit $LASTEXITCODE
