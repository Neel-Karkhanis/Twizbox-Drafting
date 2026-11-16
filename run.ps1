# Builds the image and runs the agent in a locked-down container.
# The container can only see the workspace folder; everything else is read-only.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "No .env file found. Copy .env.example to .env and add your key."
    exit 1
}
New-Item -ItemType Directory -Force "workspace" | Out-Null

docker build -q -t twizbox-agent .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker run --rm -it `
    --env-file .env `
    --read-only `
    --tmpfs /tmp `
    --cap-drop ALL `
    --security-opt no-new-privileges `
    --memory 512m `
    --pids-limit 64 `
    -v "${PWD}\workspace:/workspace" `
    twizbox-agent
