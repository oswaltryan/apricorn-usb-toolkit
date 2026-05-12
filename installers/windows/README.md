# Windows Installer

Builds a Windows Installer (`.msi`) that installs the `usb` CLI under `%ProgramFiles%\Apricorn\Apricorn USB Toolkit` and exposes it system-wide via PATH.

## Prerequisites

- Windows 10/11 build machine
- [WiX Toolset 3.14+](https://wixtoolset.org/) available in `PATH` (`candle.exe`, `light.exe`)
- PowerShell 5.1+ or PowerShell 7+
- Python 3.10+ (used to build the PyInstaller binary and read the project version)

## Build Instructions

1. Build the PyInstaller executable (or let the helper do it):
   ```powershell
   build\build_windows.bat
   ```
2. Create the MSI artifact via the helper script (runs PyInstaller automatically when needed):
   ```powershell
   pwsh build\build_windows_msi.ps1
   ```
   Skip PyInstaller if the executable is already in `dist\`:
   ```powershell
   pwsh build\build_windows_msi.ps1 -SkipPyInstaller
   ```
   If WiX ICE validation fails on a local build host because the Windows Installer service is unavailable, you can build a smoke-test MSI without ICE validation:
   ```powershell
   pwsh build\build_windows_msi.ps1 -SkipPyInstaller -SuppressIceValidation
   ```
3. The resulting `apricorn-usb-toolkit-<version>-x64.msi` is placed in `dist/` ready for distribution.

The MSI supports in-place upgrades through a stable `UpgradeCode`; installing a newer version replaces the existing Apricorn USB Toolkit installation instead of creating a separate product entry. Same-version rebuilds are also treated as upgrades to make signed local rebuilds testable, but release artifacts should still bump the package version.

The CLI is installed as `usb.exe` under `%ProgramFiles%\Apricorn\Apricorn USB Toolkit`, that directory is added to the system `PATH`, and Desktop and Start Menu shortcuts are controlled by installer checkboxes that default to selected and create shortcuts for the user running the installer. Open a new terminal after installing so Windows exposes the updated `PATH` to the shell. Uninstall via **Settings -> Apps -> Apricorn USB Toolkit**.
