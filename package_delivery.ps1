$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root "dist"
$stage = Join-Path $dist "PayForNothingBot"
$zip = Join-Path $dist "PayForNothingBot.zip"

if (Test-Path $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}

if (Test-Path $zip) {
    Remove-Item -LiteralPath $zip -Force
}

New-Item -ItemType Directory -Path $dist -Force | Out-Null
New-Item -ItemType Directory -Path $stage -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage "data") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage "tests") -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $root "main.py") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "config.json") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "config.example.json") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "run_bot.ps1") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "run_bot.bat") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "package_delivery.ps1") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "requirements.txt") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root ".gitignore") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "Dockerfile") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root ".dockerignore") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "render.yaml") -Destination $stage
Copy-Item -LiteralPath (Join-Path $root "tests\\test_storage.py") -Destination (Join-Path $stage "tests")

Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Write-Output "Архив создан: $zip"
