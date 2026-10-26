[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$pythonCandidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)

foreach ($pythonCandidate in $pythonCandidates) {
    if (Test-Path -LiteralPath $pythonCandidate -PathType Leaf) {
        & $pythonCandidate -m snes_synth gui
        exit $LASTEXITCODE
    }
}

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand -and $pythonCommand.Source -notlike '*\WindowsApps\*') {
    & $pythonCommand.Source -m snes_synth gui
    exit $LASTEXITCODE
}

$pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyCommand) {
    & $pyCommand.Source -3 -m snes_synth gui
    exit $LASTEXITCODE
}

$uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($uvCommand) {
    & $uvCommand.Source run --python 3.12 python -m snes_synth gui
    exit $LASTEXITCODE
}

throw 'Python 3.11 or newer is required. Install Python with Tk support, then run python -m snes_synth gui from this folder.'
