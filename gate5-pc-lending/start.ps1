$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython -X utf8 (Join-Path $PSScriptRoot 'app.py')
} elseif ($pythonCommand) {
    & $pythonCommand.Source -X utf8 (Join-Path $PSScriptRoot 'app.py')
} else {
    Write-Error 'Python 3.11 or later is required. Install Python, then run: python app.py'
}
