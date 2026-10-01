param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendEnv = Join-Path $repoRoot 'backend/.env'
$frontendPackage = Join-Path $repoRoot 'frontend/package.json'
$frontendRoot = Join-Path $repoRoot 'frontend'
$frontendBrowserUtil = Join-Path $repoRoot 'frontend/src/utils/browser.js'
$backendMain = Join-Path $repoRoot 'backend/app/main.py'
$authApi = Join-Path $repoRoot 'backend/app/auth_api.py'
$failures = 0
$warnings = 0

function Add-Result([string]$State, [string]$Message) {
    $color = switch ($State) {
        'PASS' { 'Green' }
        'WARN' { 'Yellow' }
        default { 'Red' }
    }
    Write-Host "[$State] $Message" -ForegroundColor $color
    if ($State -eq 'FAIL') { $script:failures++ }
    if ($State -eq 'WARN') { $script:warnings++ }
}

function Read-EnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $backendEnv)) { return '' }
    $row = Get-Content -LiteralPath $backendEnv | Where-Object {
        $_ -match "^\s*$([regex]::Escape($Name))\s*="
    } | Select-Object -Last 1
    if (-not $row) { return '' }
    $value = ($row -split '=', 2)[1].Trim()
    return $value.Trim('"').Trim("'")
}

function Read-FrontendApiOverride {
    foreach ($fileName in @('.env.development.local', '.env.local', '.env.development', '.env')) {
        $path = Join-Path $frontendRoot $fileName
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $row = Get-Content -LiteralPath $path | Where-Object {
            $_ -match '^\s*VITE_API_BASE\s*='
        } | Select-Object -Last 1
        if ($row) {
            $value = ($row -split '=', 2)[1].Trim().Trim('"').Trim("'")
            if ($value) { return $value }
        }
    }
    return ''
}

function Test-PrivateIPv4([string]$Address) {
    $bytes = [Net.IPAddress]::Parse($Address).GetAddressBytes()
    return $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
}

function Get-Listener([int]$Port) {
    return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Test-CorsPreflight([string]$HostAddress) {
    $origin = "http://${HostAddress}:$FrontendPort"
    $uri = "http://127.0.0.1:$BackendPort/api/v1/auth/login"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -Method Options -TimeoutSec 4 -Headers @{
            Origin = $origin
            'Access-Control-Request-Method' = 'POST'
            'Access-Control-Request-Headers' = 'content-type'
        }
    } catch {
        $response = $_.Exception.Response
    }
    if (-not $response) {
        Add-Result 'FAIL' "CORS preflight for $origin did not receive an API response."
        return
    }
    $allowOrigin = [string]$response.Headers['Access-Control-Allow-Origin']
    $allowCredentials = [string]$response.Headers['Access-Control-Allow-Credentials']
    if ($allowOrigin -eq $origin -and $allowCredentials -eq 'true') {
        Add-Result 'PASS' "CORS credentials preflight accepted $origin."
    } else {
        Add-Result 'FAIL' "CORS preflight rejected $origin (allow-origin='$allowOrigin', allow-credentials='$allowCredentials')."
    }
}

Write-Host 'Bid Intel LAN demo diagnostics (read-only)' -ForegroundColor Cyan

$package = Get-Content -LiteralPath $frontendPackage -Raw | ConvertFrom-Json
$devScript = [string]$package.scripts.dev
if ($devScript -match 'vite\s+--host\s+0\.0\.0\.0') {
    Add-Result 'PASS' 'Vite dev command binds to 0.0.0.0.'
} else {
    Add-Result 'FAIL' 'frontend/package.json dev script must bind Vite to 0.0.0.0.'
}

$browserSource = Get-Content -LiteralPath $frontendBrowserUtil -Raw
if ($browserSource -match 'location\.hostname' -and $browserSource -match 'apiPort') {
    Add-Result 'PASS' 'Frontend derives the API hostname from the page and uses the configured API port.'
} else {
    Add-Result 'FAIL' 'Frontend API base does not appear to derive from the page hostname.'
}

$mainSource = Get-Content -LiteralPath $backendMain -Raw
if ($mainSource -match 'allow_credentials\s*=\s*True' -and $mainSource -match '192\\\.168') {
    Add-Result 'PASS' 'Backend CORS allows credentialed requests from RFC1918 LAN origins.'
} else {
    Add-Result 'FAIL' 'Backend CORS is missing credential support or its private IPv4 origin rule.'
}

$authSource = Get-Content -LiteralPath $authApi -Raw
if ($authSource -match 'httponly\s*=\s*True' -and $authSource -match 'samesite\s*=\s*"strict"') {
    Add-Result 'PASS' 'Session cookie is HttpOnly and SameSite=Strict.'
} else {
    Add-Result 'FAIL' 'Session cookie security attributes are missing.'
}

$authEnabled = (Read-EnvValue 'AUTH_ENABLED') -match '^(?i:true|1|yes)$'
if (-not (Test-Path -LiteralPath $backendEnv)) {
    Add-Result 'WARN' 'backend/.env is missing; runtime settings may not match the reviewer setup.'
} elseif ($authEnabled) {
    $missing = @('AUTH_USERNAME', 'AUTH_PASSWORD', 'AUTH_SECRET_KEY') | Where-Object {
        -not (Read-EnvValue $_)
    }
    if ($missing.Count -eq 0) {
        Add-Result 'PASS' 'Reviewer auth is enabled and required values are present (values were not printed).'
    } else {
        Add-Result 'FAIL' "AUTH_ENABLED=true but required settings are empty: $($missing -join ', ')."
    }
    if ((Read-EnvValue 'AUTH_COOKIE_SECURE') -match '^(?i:true|1|yes)$') {
        Add-Result 'FAIL' 'AUTH_COOKIE_SECURE=true cannot persist cookies over the HTTP LAN demo URL.'
    } else {
        Add-Result 'PASS' 'HTTP demo cookie Secure flag is disabled.'
    }
} else {
    Add-Result 'WARN' 'AUTH_ENABLED is false; the reviewer login screen will not be exercised.'
}

$upInterfaceIndexes = @(Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object Status -eq 'Up' | ForEach-Object { $_.ifIndex })
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred -ErrorAction SilentlyContinue |
    Where-Object {
        $_.InterfaceIndex -in $upInterfaceIndexes -and
        $_.IPAddress -ne '127.0.0.1' -and
        $_.IPAddress -notlike '169.254.*' -and
        (Test-PrivateIPv4 $_.IPAddress)
    } | Sort-Object InterfaceIndex, IPAddress)
if ($addresses.Count -gt 0) {
    Write-Host 'Private IPv4 addresses (use an address on the same LAN as the reviewer):'
    $addresses | ForEach-Object { Write-Host "  $($_.InterfaceAlias): http://$($_.IPAddress):$FrontendPort" }
} else {
    Add-Result 'WARN' 'No active RFC1918 IPv4 address was found; connect the demo host to the intended LAN.'
}

$apiOverride = Read-FrontendApiOverride
if ($apiOverride -match '^https?://(?:localhost|127(?:\.\d{1,3}){3})(?::|/|$)') {
    Add-Result 'FAIL' "VITE_API_BASE points to this client's loopback address ($apiOverride); clear it or use an API address reachable from reviewers."
} elseif ($apiOverride) {
    Add-Result 'WARN' "VITE_API_BASE overrides same-host discovery: $apiOverride. Confirm this address is reachable from the reviewer device."
} else {
    Add-Result 'PASS' 'VITE_API_BASE is blank in Vite env files; LAN clients will use the page hostname for the API.'
}

foreach ($service in @(
    [pscustomobject]@{ Name = 'Backend'; Port = $BackendPort },
    [pscustomobject]@{ Name = 'Vite'; Port = $FrontendPort }
)) {
    $listeners = Get-Listener $service.Port
    if ($listeners.Count -eq 0) {
        Add-Result 'FAIL' "$($service.Name) has no listening TCP socket on port $($service.Port). Start it, then rerun this check."
    } elseif (@($listeners | Where-Object { $_.LocalAddress -eq '0.0.0.0' -or $_.LocalAddress -in @($addresses.IPAddress) }).Count -gt 0) {
        Add-Result 'PASS' "$($service.Name) listens on IPv4 LAN interfaces on TCP port $($service.Port)."
    } else {
        Add-Result 'FAIL' "$($service.Name) has no IPv4 wildcard/LAN listener on port $($service.Port); bind it to 0.0.0.0."
    }
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/v1/health" -TimeoutSec 4
    if ($health.status -eq 'ok') {
        Add-Result 'PASS' "Local API health check passed (notices_imported=$($health.notices_imported))."
        foreach ($address in $addresses) {
            $lanHealthUri = "http://$($address.IPAddress):$BackendPort/api/v1/health"
            $lanFrontendUri = "http://$($address.IPAddress):$FrontendPort/"
            try {
                $null = Invoke-RestMethod -Uri $lanHealthUri -TimeoutSec 4
                Add-Result 'PASS' "API is reachable through this host's LAN interface: $lanHealthUri (local-host probe only)."
            } catch {
                Add-Result 'FAIL' "API did not answer through local LAN interface $($address.IPAddress): $($_.Exception.Message)"
            }
            try {
                $null = Invoke-WebRequest -UseBasicParsing -Uri $lanFrontendUri -TimeoutSec 4
                Add-Result 'PASS' "Vite is reachable through this host's LAN interface: $lanFrontendUri (local-host probe only)."
            } catch {
                Add-Result 'FAIL' "Vite did not answer through local LAN interface $($address.IPAddress): $($_.Exception.Message)"
            }
            Test-CorsPreflight $address.IPAddress
        }
    } else {
        Add-Result 'FAIL' 'API health endpoint returned an unexpected payload.'
    }
} catch {
    Add-Result 'FAIL' "Local API health check failed: $($_.Exception.Message)"
}

try {
    $profiles = @(Get-NetConnectionProfile -ErrorAction Stop)
    if (@($profiles | Where-Object NetworkCategory -eq 'Public').Count -gt 0) {
        Add-Result 'WARN' 'An active network profile is Public. Use the trusted Private LAN profile for the demo.'
    }
} catch {
    Add-Result 'WARN' 'Could not read Windows network profiles; verify the active LAN is trusted and Private.'
}

try {
    $allowedPorts = @()
    $rules = Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Action Allow -Enabled True -ErrorAction Stop
    foreach ($rule in $rules) {
        $filter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
        $localPorts = @($filter.LocalPort | ForEach-Object { [string]$_ })
        if ($filter.Protocol -in @('TCP', '6') -and
            ($localPorts -contains 'Any' -or $localPorts -contains [string]$BackendPort -or
             $localPorts -contains [string]$FrontendPort)) {
            $allowedPorts += $filter.LocalPort
        }
    }
    if ($allowedPorts.Count -gt 0) {
        Add-Result 'PASS' 'An enabled inbound TCP firewall rule covers at least one demo port; confirm it is scoped to Private/LocalSubnet.'
    } else {
        Add-Result 'WARN' "No explicit inbound TCP allow rule was found for ports $FrontendPort/$BackendPort. Remote reachability still needs a second-device test."
    }
} catch {
    Add-Result 'WARN' 'Could not inspect inbound firewall rules without system firewall access.'
}

Add-Result 'WARN' 'This checks only the demo host. LAN routing, firewall reachability, browser login, and cookie persistence still require another computer.'
Write-Host "Result: $failures failure(s), $warnings warning(s)."
if ($failures -gt 0) { exit 1 }
exit 0
