$ErrorActionPreference = "Stop"

$userNodeBins = @(
    (Join-Path $HOME ".local/node-current/bin")
    (Join-Path $HOME ".local/bin")
)
foreach ($userNodeBin in $userNodeBins) {
    if (Test-Path (Join-Path $userNodeBin "node")) {
        $env:PATH = $userNodeBin + [IO.Path]::PathSeparator + $env:PATH
        break
    }
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $HOME ".cache/ms-playwright"

$nodeVersion = (& node --version).TrimStart("v")
$nodeMajor = [int]($nodeVersion.Split(".")[0])
if ($nodeMajor -lt 20) {
    throw "Playwright requires Node.js 20 or newer; found v$nodeVersion"
}

$portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$portProbe.Start()
$env:MEMENTO_E2E_PORT = $portProbe.LocalEndpoint.Port.ToString()
$portProbe.Stop()

Set-Location $PSScriptRoot
$exitCode = 1
try {
    & npx playwright test -c playwright.config.mjs @args
    $exitCode = $LASTEXITCODE
}
finally {
    $nextProcesses = & /usr/bin/pgrep -f "next dev .* -p $env:MEMENTO_E2E_PORT"
    foreach ($processId in $nextProcesses) {
        if ($processId -match "^\d+$") {
            & /usr/bin/kill $processId 2>$null
        }
    }
}
exit $exitCode
