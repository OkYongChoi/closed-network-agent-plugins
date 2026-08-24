$ErrorActionPreference = "Stop"
& python (Join-Path $PSScriptRoot "plugin-installer") @args
exit $LASTEXITCODE
