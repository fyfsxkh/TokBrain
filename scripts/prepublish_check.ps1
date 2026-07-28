param(
    [string]$GitExecutable = "git",
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    Write-Host "==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Invoke-Checked "Git repository check" {
    & $GitExecutable rev-parse --is-inside-work-tree
}

$requiredFiles = @(
    ".gitignore",
    "LICENSE",
    "LICENSE.zh-CN",
    "THIRD_PARTY_NOTICES.md",
    "OPEN_SOURCE_RISK_REPORT.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "README.md",
    ".env.example",
    "requirements.txt",
    "requirements-f2.txt",
    "frontend/package-lock.json",
    ".github/workflows/ci.yml",
    ".github/dependabot.yml"
)

$missing = @($requiredFiles | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($missing.Count) {
    throw "Missing release files: $($missing -join ', ')"
}

$candidateFiles = @(
    & $GitExecutable -c core.quotepath=false ls-files --cached --others --exclude-standard
)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to enumerate release candidates"
}

$forbiddenPathPattern = (
    '(^|/)(data|logs|backups|node_modules|\.next|\.venv(?:-[^/]+)?|\.vendor|\.agents|\.codex)/' +
    '|(^|/)\.env$|\.(db|db-[^/]+|sqlite|sqlite3|pem|key|zip|log)$'
)
$forbiddenFiles = @(
    $candidateFiles |
        ForEach-Object { $_.Replace('\', '/') } |
        Where-Object { $_ -match $forbiddenPathPattern }
)
if ($forbiddenFiles.Count) {
    Write-Host "Forbidden release candidates:" -ForegroundColor Red
    $forbiddenFiles | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "Private or generated files are included in the release candidate"
}

$secretPatterns = @(
    'sk-[A-Za-z0-9_-]{20,}',
    'LTAI[A-Za-z0-9]{12,}',
    '(?:AKIA|ASIA)[0-9A-Z]{16}',
    'gh[pousr]_[A-Za-z0-9]{20,}',
    'AIza[A-Za-z0-9_-]{20,}',
    'xox[baprs]-[A-Za-z0-9-]{10,}',
    '(?:sk|rk)_live_[A-Za-z0-9]{16,}',
    'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}',
    '-----BEGIN [A-Z ]*PRIVATE KEY-----',
    '(?i)(sessionid(?:_ss)?|sid_guard|sid_tt|uid_tt|ttwid|msToken|odin_tt|passport_csrf_token)=[A-Za-z0-9%._-]{12,}',
    '(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*["''][^"'']{16,}["'']',
    '(?i)[A-Z]:\\Users\\(?!Public\\)[^\\\r\n]{1,64}\\',
    '(?i)(?:^|[\s"''])/(?:Users|home)/[A-Za-z0-9._-]+/'
)
$secretHits = [System.Collections.Generic.HashSet[string]]::new()
$binaryExtensions = @(
    ".7z", ".avi", ".bmp", ".db", ".dll", ".exe", ".gif", ".gz", ".ico",
    ".jpeg", ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".pdf", ".png",
    ".sqlite", ".sqlite3", ".tar", ".webm", ".webp", ".woff", ".woff2", ".zip"
)

foreach ($relativePath in $candidateFiles) {
    $absolutePath = Join-Path $projectRoot $relativePath
    if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
        continue
    }
    $file = Get-Item -LiteralPath $absolutePath
    if ($file.Length -gt 10MB) {
        throw "Release candidate exceeds 10 MB: $relativePath"
    }
    if ($binaryExtensions -contains $file.Extension.ToLowerInvariant()) {
        continue
    }
    $content = Get-Content -Raw -LiteralPath $absolutePath -ErrorAction Stop
    foreach ($pattern in $secretPatterns) {
        if ($content -match $pattern) {
            [void]$secretHits.Add($relativePath)
        }
    }
}

if ($secretHits.Count) {
    Write-Host "Possible secrets found; inspect these files without posting values:" -ForegroundColor Red
    $secretHits | Sort-Object | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "Secret scan failed"
}

$brokenLinks = [System.Collections.Generic.HashSet[string]]::new()
foreach ($relativePath in $candidateFiles | Where-Object { $_ -match '\.md$' }) {
    $absolutePath = Join-Path $projectRoot $relativePath
    $content = Get-Content -Raw -LiteralPath $absolutePath -ErrorAction Stop
    foreach ($match in [regex]::Matches($content, '\[[^\]]*\]\(([^)\s]+)')) {
        $target = $match.Groups[1].Value.Trim('<', '>')
        if ($target -match '^(?:https?://|mailto:|#)') {
            continue
        }
        $targetPath = $target.Split('#', 2)[0]
        if (-not $targetPath) {
            continue
        }
        $resolved = Join-Path (Split-Path -Parent $absolutePath) $targetPath
        if (-not (Test-Path -LiteralPath $resolved)) {
            [void]$brokenLinks.Add("${relativePath}: $target")
        }
    }
}
if ($brokenLinks.Count) {
    Write-Host "Broken relative Markdown links:" -ForegroundColor Red
    $brokenLinks | Sort-Object | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "Markdown link check failed"
}

$temporaryIndex = Join-Path (
    [System.IO.Path]::GetTempPath()
) "tokbrain-prepublish-$([guid]::NewGuid().ToString('N')).index"
$temporaryObjects = Join-Path (
    [System.IO.Path]::GetTempPath()
) "tokbrain-prepublish-objects-$([guid]::NewGuid().ToString('N'))"
$previousIndex = $env:GIT_INDEX_FILE
$previousObjectDirectory = $env:GIT_OBJECT_DIRECTORY
try {
    [void](New-Item -ItemType Directory -Path $temporaryObjects)
    $env:GIT_INDEX_FILE = $temporaryIndex
    $env:GIT_OBJECT_DIRECTORY = $temporaryObjects
    Invoke-Checked "Temporary release index" {
        & $GitExecutable read-tree --empty
        & $GitExecutable add --all -- .
    }
    Invoke-Checked "Git whitespace check" {
        & $GitExecutable diff --cached --check
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryIndex) {
        Remove-Item -LiteralPath $temporaryIndex -Force
    }
    if (Test-Path -LiteralPath $temporaryObjects) {
        $temporaryRoot = [System.IO.Path]::GetFullPath(
            [System.IO.Path]::GetTempPath()
        )
        $resolvedObjects = [System.IO.Path]::GetFullPath($temporaryObjects)
        if (-not $resolvedObjects.StartsWith(
            $temporaryRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to remove unexpected temporary object path"
        }
        Remove-Item -LiteralPath $resolvedObjects -Recurse -Force
    }
    if ($null -eq $previousIndex) {
        Remove-Item Env:\GIT_INDEX_FILE -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_INDEX_FILE = $previousIndex
    }
    if ($null -eq $previousObjectDirectory) {
        Remove-Item Env:\GIT_OBJECT_DIRECTORY -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_OBJECT_DIRECTORY = $previousObjectDirectory
    }
}

if ($Full) {
    Invoke-Checked "Backend tests" {
        & ".\.venv\Scripts\python.exe" -m pytest -q
    }
    Push-Location frontend
    try {
        Invoke-Checked "Frontend tests" { npm test }
        Invoke-Checked "Frontend lint" { npm run lint }
        Invoke-Checked "Frontend production build" { npm run build }
        Invoke-Checked "Production dependency audit" {
            npm audit --omit=dev --audit-level=high
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host "Release candidate check passed ($($candidateFiles.Count) files)." -ForegroundColor Green
