@echo off
setlocal

REM Build a minimal source-review archive from the repository root.
cd /d "%~dp0"
for %%I in ("%CD%") do set "PROJECT_NAME=%%~nxI"
set "ZIP_FILE=%CD%\%PROJECT_NAME%.zip"

echo.
echo ========================================
echo Project: %PROJECT_NAME%
echo Output : %ZIP_FILE%
echo ========================================
echo.

if exist "%ZIP_FILE%" del /f /q "%ZIP_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$root = (Get-Location).Path;" ^
    "$zip = Join-Path $root '%PROJECT_NAME%.zip';" ^
    "$sourceRoots = @('agent','docs','examples','tests','web','AGENTS.md','README.md','.gitignore','pyproject.toml','requirements.txt','pack.bat');" ^
    "$excludedDirectories = @('.git','.venv','.serena','.worktrees','.playwright-cli','output','node_modules','__pycache__','.pytest_cache','coverage','htmlcov','.mypy_cache','.ruff_cache','.tox','.idea','.vscode','dist','build');" ^
    "$excludedExtensions = @('.pyc','.pyo','.pyd','.zip','.tsbuildinfo');" ^
    "$temp = Join-Path ([IO.Path]::GetTempPath()) ('agent_pack_' + [guid]::NewGuid().ToString());" ^
    "New-Item -ItemType Directory -Path $temp | Out-Null;" ^
    "try {" ^
    "  foreach ($name in $sourceRoots) {" ^
    "    $src = Join-Path $root $name;" ^
    "    if (-not (Test-Path -LiteralPath $src)) { Write-Warning ('Not found: ' + $name); continue }" ^
    "    if (Test-Path -LiteralPath $src -PathType Leaf) {" ^
    "      $dst = Join-Path $temp $name; New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null; Copy-Item -LiteralPath $src -Destination $dst -Force; continue" ^
    "    }" ^
    "    $srcRoot = (Resolve-Path -LiteralPath $src).Path;" ^
    "    foreach ($file in Get-ChildItem -LiteralPath $srcRoot -Recurse -Force -File) {" ^
    "      $relative = $file.FullName.Substring($root.Length) -replace '^[\\/]+', '';" ^
    "      $parts = $relative -split '[\\/]';" ^
    "      if (@($parts | Where-Object { $excludedDirectories -contains $_ }).Count -gt 0) { continue }" ^
    "      if ($excludedExtensions -contains $file.Extension.ToLowerInvariant()) { continue }" ^
    "      $dst = Join-Path $temp $relative; New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null; Copy-Item -LiteralPath $file.FullName -Destination $dst -Force" ^
    "    }" ^
    "  }" ^
    "  Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
    "  [IO.Compression.ZipFile]::CreateFromDirectory($temp, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)" ^
    "} finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }"

if errorlevel 1 (
    echo.
    echo [ERROR] Source-review archive creation failed.
    exit /b 1
)

echo.
echo ========================================
echo Done: %ZIP_FILE%
echo ========================================
exit /b 0
