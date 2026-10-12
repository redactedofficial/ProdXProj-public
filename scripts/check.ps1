$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runnerPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $runnerPython)) { $runnerPython = 'python' }
Push-Location $projectRoot
try {
    & $runnerPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $runnerPython -m compileall -q proxrunner tests
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
