$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendRoot = Join-Path $repoRoot 'backend'
$envPath = Join-Path $backendRoot '.env'
$dataRoot = Join-Path $backendRoot '.data'
$credentialPath = Join-Path $dataRoot 'reviewer-credentials.txt'

function New-UrlSafeSecret([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $backendRoot '.env.example') -Destination $envPath
}
$managedNames = 'AUTH_ENABLED|AUTH_USERNAME|AUTH_PASSWORD|AUTH_SECRET_KEY|AUTH_SESSION_HOURS|AUTH_COOKIE_SECURE'
$keptLines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -notmatch "^\s*(?:$managedNames)\s*=" })
$username = 'reviewer'
$password = New-UrlSafeSecret 32
$signingKey = New-UrlSafeSecret 48
$authLines = @(
    'AUTH_ENABLED=true',
    "AUTH_USERNAME=$username",
    "AUTH_PASSWORD=$password",
    "AUTH_SECRET_KEY=$signingKey",
    'AUTH_SESSION_HOURS=8',
    'AUTH_COOKIE_SECURE=false'
)
[IO.File]::WriteAllLines($envPath, @($keptLines + $authLines), [Text.UTF8Encoding]::new($false))

New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
[IO.File]::WriteAllText(
    $credentialPath,
    "Reviewer account (private local file)`r`nUsername: $username`r`nPassword: $password`r`n`r`nShare with evaluators only through a controlled channel. This file is ignored by Git.`r`n",
    [Text.UTF8Encoding]::new($false)
)
Write-Host 'Reviewer account configured. Credentials were not printed; find them in the Git-ignored backend/.data/reviewer-credentials.txt. Restart the backend.'
