from __future__ import annotations

import platform
import shutil
import subprocess
import sys

WINDOWS_COMPILE_COMMAND = """
$ErrorActionPreference = 'Stop'
$vswhere = 'C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe'
if (-not (Test-Path $vswhere)) { Write-Error 'vswhere.exe not found'; exit 1 }
$installPath = & $vswhere `
  -latest `
  -products * `
  -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
  -property installationPath
if (-not $installPath) { Write-Error 'Visual Studio C++ Build Tools not found'; exit 1 }
$vsDevCmd = Join-Path $installPath 'Common7\\Tools\\VsDevCmd.bat'
if (-not (Test-Path $vsDevCmd)) { Write-Error "VsDevCmd.bat not found at $vsDevCmd"; exit 1 }
$objDir = Join-Path $env:TEMP 'usb_native_obj'
New-Item -ItemType Directory -Force -Path $objDir | Out-Null
$sources = @(
  'utils\\windows_native_scan\\main.c'
  'utils\\windows_native_scan\\common.c'
  'utils\\windows_native_scan\\storage.c'
  'utils\\windows_native_scan\\devnode.c'
  'utils\\windows_native_scan\\topology.c'
  'utils\\windows_native_scan\\json_emit.c'
  'utils\\windows_native_scan\\enumerate.c'
)
$sourceArgs = $sources -join ' '
$compile =
  'if not exist "' + $objDir + '" mkdir "' + $objDir +
  '" && call "' + $vsDevCmd + '" -arch=x64 -host_arch=x64 >nul && ' +
  'cl /nologo /W4 /WX /analyze /sdl /utf-8 /c /Fo"' +
  $objDir +
  '\\\\" ' + $sourceArgs
cmd /c $compile
exit $LASTEXITCODE
""".strip()


def main() -> int:
    if platform.system() != "Windows":
        return 0

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        print("pwsh or powershell not found in PATH", file=sys.stderr)
        return 1

    completed = subprocess.run(
        [shell, "-NoProfile", "-Command", WINDOWS_COMPILE_COMMAND],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
