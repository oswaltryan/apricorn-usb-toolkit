[CmdletBinding()]
param(
    [switch]$SkipPyInstaller,
    [switch]$SuppressIceValidation
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptRoot '..')
$distDir = Join-Path $repoRoot 'dist'
$wxsPath = Join-Path $repoRoot 'installers/windows/usb-tool.wxs'
$licensePath = Join-Path $repoRoot 'installers/windows/license.rtf'
$intermediateDir = Join-Path $repoRoot 'installers/windows/build'
$iconPath = Join-Path $repoRoot 'build/USBTool.ico'
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
New-Item -ItemType Directory -Force -Path $intermediateDir | Out-Null

function Get-MsiVersion([string]$version) {
    if (-not $version) { return '0.0.0' }
    if ($version -notmatch '^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?') {
        return '0.0.0'
    }
    $parts = @($Matches[1], $Matches[2], $Matches[3], $Matches[4]) | ForEach-Object {
        if ($null -eq $_) { '0' } else { $_ }
    }
    while ($parts.Count -gt 4) { $parts = $parts[0..3] }
    while ($parts.Count -lt 3) { $parts += '0' }
    if ($parts.Count -lt 4) { $parts += '0' }
    return $parts -join '.'
}

function Get-UsbVersion {
    $version = & python (Join-Path $repoRoot 'utils/project_version.py') read
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to determine project version from pyproject.toml'
    }
    $raw = ($version | Out-String).Trim()
    if (-not $raw) {
        throw 'Unable to determine project version from pyproject.toml'
    }
    return $raw.Trim()
}

function Find-PyInstallerBinary {
    $candidates = @(
        (Join-Path $distDir 'usb-windows.exe')
        (Join-Path $distDir 'usb.exe')
        (Join-Path $distDir 'usb/usb.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    throw "PyInstaller binary not found under $distDir. Build it first."
}

if (-not $SkipPyInstaller) {
    & (Join-Path $repoRoot 'build/build_windows.bat')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
}

$usbBinaryPath = Find-PyInstallerBinary
$stagedBinary = Join-Path $distDir 'usb-windows.exe'
if ($usbBinaryPath -ne $stagedBinary) {
    Copy-Item $usbBinaryPath $stagedBinary -Force
}

$version = Get-UsbVersion
$msiVersion = Get-MsiVersion $version
$wixObj = Join-Path $intermediateDir 'usb-tool.wixobj'
if (-not (Test-Path $iconPath)) {
    throw "Product icon not found at $iconPath. Ensure the .ico is present before building the MSI."
}
if (-not (Test-Path $licensePath)) {
    throw "Installer license file not found at $licensePath."
}

try {
    $candle = Get-Command candle.exe -ErrorAction Stop
    $light = Get-Command light.exe -ErrorAction Stop
}
catch {
    throw "WiX Toolset binaries (candle.exe/light.exe) not found in PATH. Install WiX 3.14+ and ensure its bin directory is available."
}

$productVersionDefine = "-dProductVersion=$msiVersion"
$binaryDefine = "-dUsbBinary=$stagedBinary"
$iconDefine = "-dProductIcon=$iconPath"
$licenseDefine = "-dLicenseRtf=$licensePath"

$candleArgs = @(
    '-ext', 'WixUtilExtension',
    '-ext', 'WixUIExtension',
    $productVersionDefine,
    $binaryDefine,
    $iconDefine,
    $licenseDefine,
    '-out', $wixObj,
    $wxsPath
)

& $candle.Path @candleArgs
if ($LASTEXITCODE -ne 0) {
    throw "WiX candle.exe failed with exit code $LASTEXITCODE"
}

$msiPath = Join-Path $distDir "apricorn-usb-toolkit-$version-x64.msi"
$lightArgs = @(
    '-ext', 'WixUtilExtension',
    '-ext', 'WixUIExtension',
    $iconDefine,
    '-out', $msiPath,
    $wixObj
)
if ($SuppressIceValidation) {
    $lightArgs = @('-sval') + $lightArgs
}

& $light.Path @lightArgs
if ($LASTEXITCODE -ne 0) {
    throw "WiX light.exe failed with exit code $LASTEXITCODE"
}

Write-Host "MSI created at $msiPath"
