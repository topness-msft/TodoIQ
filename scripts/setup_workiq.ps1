$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content (Join-Path $PSScriptRoot "workiq-runtime.json") -Raw | ConvertFrom-Json
$runtimeRoot = Join-Path $repoRoot "data\runtime\workiq"
$packageSpec = "$($manifest.package)@$($manifest.packageVersion)"

Write-Host "Work IQ runtime setup"
Write-Host "  Package: $packageSpec"
Write-Host "  Runtime: $runtimeRoot"
$npmProxy = npm config get proxy
$npmHttpsProxy = npm config get https-proxy
Write-Host "  npm registry: inherited from active npm config"
Write-Host "  npm proxy: $(if ($npmProxy -and $npmProxy -ne 'null') { 'configured (value redacted)' } else { 'not set' })"
Write-Host "  npm https-proxy: $(if ($npmHttpsProxy -and $npmHttpsProxy -ne 'null') { 'configured (value redacted)' } else { 'not set' })"

$node = Get-Command node -ErrorAction Stop
$npm = Get-Command npm -ErrorAction Stop
$nodeVersionText = & $node.Source -p "process.versions.node"
if ($LASTEXITCODE -ne 0 -or [version]$nodeVersionText -lt [version]$manifest.nodeMinimum) {
    throw "Node $($manifest.nodeMinimum) or newer is required; found $nodeVersionText."
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

# npm resolves registry, proxy, certificate, and authentication from the user's
# normal npm configuration. Do not replace those values here.
& $npm.Source install --prefix $runtimeRoot --no-save --ignore-scripts $packageSpec
if ($LASTEXITCODE -ne 0) {
    throw "npm install failed with exit code $LASTEXITCODE. Review the configured npm registry/proxy shown above."
}

$packageJson = Join-Path $runtimeRoot "node_modules\@microsoft\workiq\package.json"
$architecture = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win32-arm64" } else { "win32-x64" }
$entryPoint = Join-Path $runtimeRoot $manifest.platformEntries.$architecture
if (-not (Test-Path $packageJson) -or -not (Test-Path $entryPoint)) {
    throw "The pinned Work IQ package did not produce the expected runtime files."
}
$installed = Get-Content $packageJson -Raw | ConvertFrom-Json
if ($installed.version -ne $manifest.packageVersion) {
    throw "Work IQ version mismatch: expected $($manifest.packageVersion), found $($installed.version)."
}

Write-Host "Installed Work IQ $($installed.version) for Riveter."
Write-Host "Managed executable: $entryPoint"
Write-Host "Run the dashboard and use 'Check readiness' to verify MCP/authentication."
