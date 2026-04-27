param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BotArgs
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$main = Join-Path $root "main.py"

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source $main @BotArgs
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $main @BotArgs
    exit $LASTEXITCODE
}

Write-Error "Python 3.11+ не найден. Установите Python и повторите запуск."
exit 1
