# src/usb_tool/cli.py

import argparse
import ctypes
import json
import os
import platform
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

_SYSTEM = platform.system().lower()
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_WINDOWS_TERMINAL_PARENTS = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "wt.exe",
    "windowsterminal.exe",
    "bash.exe",
    "mintty.exe",
}
_WINDOWS_OUTPUT_FIELD_ORDER = (
    "bcdUSB",
    "idVendor",
    "idProduct",
    "bcdDevice",
    "iManufacturer",
    "iProduct",
    "iSerial",
    "usbController",
    "driverTransport",
    "usbDriverProvider",
    "usbDriverVersion",
    "usbDriverInf",
    "diskDriverProvider",
    "diskDriverVersion",
    "diskDriverInf",
    "physicalDriveNum",
    "busNumber",
    "deviceAddress",
    "mediaType",
    "driveSizeGB",
    "readOnly",
    "fileSystem",
    "driveLetter",
    "deviceMode",
)


def is_admin_windows() -> bool:
    if not _SYSTEM.startswith("win"):
        return False
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        shell32 = getattr(windll, "shell32", None)
        if shell32 is None:
            return False
        is_user_an_admin = getattr(shell32, "IsUserAnAdmin", None)
        if is_user_an_admin is None:
            return False
        return bool(is_user_an_admin())
    except (AttributeError, Exception):
        return False


def is_root_posix() -> bool:
    if not (_SYSTEM.startswith("linux") or _SYSTEM.startswith("darwin")):
        return False

    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return False

    geteuid_fn = cast(Callable[[], int], geteuid)

    try:
        return geteuid_fn() == 0
    except OSError:
        return False


def _is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def _get_parent_process_chain_windows() -> list[str]:
    if not _SYSTEM.startswith("win"):
        return []

    try:
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return []
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is None:
            return []

        current_pid = os.getpid()
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return []

        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return []

            process_map: dict[int, tuple[int, str]] = {}
            while True:
                process_map[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile).lower(),
                )
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break

            chain: list[str] = []
            seen: set[int] = set()
            pid = current_pid
            while pid > 0 and pid not in seen and pid in process_map:
                seen.add(pid)
                parent_pid, _name = process_map[pid]
                if parent_pid <= 0 or parent_pid not in process_map:
                    break
                pid = parent_pid
                chain.append(process_map[pid][1])
            return chain
        finally:
            kernel32.CloseHandle(snapshot)
    except (AttributeError, ValueError, TypeError, OSError, Exception):
        return []


def _is_standalone_windows_console_launch() -> bool:
    for process_name in _get_parent_process_chain_windows():
        if process_name in _WINDOWS_TERMINAL_PARENTS:
            return False
        if process_name == "explorer.exe":
            return True
    return False


def _should_pause_before_exit() -> bool:
    if not _SYSTEM.startswith("win"):
        return False

    force_pause = os.getenv("USB_TOOL_PAUSE_ON_EXIT", "").strip().lower()
    if force_pause in _TRUTHY_VALUES:
        return True

    if not _is_frozen_app():
        return False

    # Avoid blocking scripted invocations that pass arguments.
    if len(sys.argv) > 1:
        return False

    # Pause only when the packaged exe appears to have been launched from Explorer.
    return _is_standalone_windows_console_launch()


def _pause_before_exit_if_needed() -> None:
    if not _should_pause_before_exit():
        return
    _wait_for_user_acknowledgement()


def _wait_for_user_acknowledgement() -> None:
    if _SYSTEM.startswith("win"):
        try:
            import msvcrt

            print("\nPress any key to close...", end="", flush=True)
            getwch = getattr(msvcrt, "getwch", None)
            if callable(getwch):
                getwch()
            else:
                getch = getattr(msvcrt, "getch", None)
                if callable(getch):
                    getch()
                else:
                    raise RuntimeError("No console key reader available")
            print()
            return
        except Exception:
            pass
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass


def _error_log_path() -> Path:
    override = os.getenv("USB_TOOL_ERROR_LOG", "").strip()
    if override:
        return Path(override)

    for var in ("TEMP", "TMP"):
        value = os.getenv(var, "").strip()
        if value:
            return Path(value) / "usb_tool_error.log"
    return Path.cwd() / "usb_tool_error.log"


def _write_startup_error_log(exc: BaseException) -> str | None:
    path = _error_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"\n[{timestamp}] usb startup error\n")
            fh.write(tb_text)
        return str(path)
    except OSError:
        return None


def _load_print_help():
    try:
        from usb_tool.help_text import print_help as _print_help

        return _print_help
    except Exception:
        from .help_text import print_help as _print_help

        return _print_help


def _load_device_manager_class():
    try:
        from usb_tool.services import DeviceManager as _DeviceManager

        return _DeviceManager
    except Exception:
        from .services import DeviceManager as _DeviceManager

        return _DeviceManager


def _device_mode_from_drive_size(drive_size: Any) -> str:
    size_text = str(drive_size if drive_size is not None else "").strip().upper()
    return "OOB Mode" if size_text.startswith("N/A") else "Unlocked"


def _has_drive_letter_value(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    if not text or text.lower() == "not formatted":
        return False

    tokens = [token.strip() for token in text.split(",") if token.strip()]
    for token in tokens:
        if len(token) >= 2 and token[0].isalpha() and token[1] == ":":
            return True
    return False


def _apply_device_mode_output_fields(device_dict: dict[str, Any]) -> None:
    device_mode = _device_mode_from_drive_size(device_dict.get("driveSizeGB"))
    device_dict["deviceMode"] = device_mode
    if device_mode == "OOB Mode":
        device_dict.pop("mediaType", None)
        device_dict.pop("driveSizeGB", None)
        device_dict.pop("driveLetter", None)
        device_dict.pop("fileSystem", None)
        device_dict.pop("readOnly", None)
        return

    media_type = str(device_dict.get("mediaType", "")).strip().lower()
    if (
        _SYSTEM.startswith("win")
        and media_type == "basic disk"
        and not _has_drive_letter_value(device_dict.get("driveLetter"))
    ):
        file_system = str(device_dict.get("fileSystem", "")).strip()
        if not file_system or file_system.upper() == "RAW":
            device_dict["fileSystem"] = "Unallocated"
        device_dict.pop("driveLetter", None)


def _order_windows_output_fields(device_dict: dict[str, Any]) -> dict[str, Any]:
    if not _SYSTEM.startswith("win"):
        return dict(device_dict)

    ordered: dict[str, Any] = {}
    for field_name in _WINDOWS_OUTPUT_FIELD_ORDER:
        if field_name in device_dict:
            ordered[field_name] = device_dict[field_name]

    for field_name, value in device_dict.items():
        if field_name not in ordered:
            ordered[field_name] = value

    return ordered


def _filter_json_fields(device_dict: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(device_dict)
    _apply_device_mode_output_fields(filtered)

    if _SYSTEM.startswith("win"):
        return _order_windows_output_fields(filtered)

    for field_name in (
        "usbDriverProvider",
        "usbDriverVersion",
        "usbDriverInf",
        "diskDriverProvider",
        "diskDriverVersion",
        "diskDriverInf",
        "physicalDriveNum",
        "driveLetter",
    ):
        filtered.pop(field_name, None)

    for field_name in ("busNumber", "deviceAddress"):
        value = filtered.get(field_name)
        if isinstance(value, int) and value < 0:
            filtered.pop(field_name, None)

    return filtered


def _devices_to_json_payload(devices: list[Any]) -> dict[str, list[dict[str, Any]]]:
    devices_mapping = {
        str(i + 1): _filter_json_fields(dev.to_dict()) for i, dev in enumerate(devices)
    }
    return {"devices": [devices_mapping] if devices_mapping else []}


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _filter_printable_fields(device_dict: dict[str, Any]) -> dict[str, Any]:
    printable = dict(device_dict)
    printable.pop("bridgeFW", None)
    _apply_device_mode_output_fields(printable)

    if _SYSTEM.startswith("win"):
        for field_name in (
            "usbDriverProvider",
            "usbDriverVersion",
            "usbDriverInf",
            "diskDriverProvider",
            "diskDriverVersion",
            "diskDriverInf",
            "busNumber",
            "deviceAddress",
        ):
            printable.pop(field_name, None)
        return _order_windows_output_fields(printable)

    for field_name in (
        "usbDriverProvider",
        "usbDriverVersion",
        "usbDriverInf",
        "diskDriverProvider",
        "diskDriverVersion",
        "diskDriverInf",
        "physicalDriveNum",
        "driveLetter",
    ):
        printable.pop(field_name, None)

    for field_name in ("busNumber", "deviceAddress"):
        value = printable.get(field_name)
        if isinstance(value, int) and value < 0:
            printable.pop(field_name, None)

    return printable


def _handle_list_action(devices: list[Any], json_mode: bool = False) -> None:
    if json_mode:
        payload = _devices_to_json_payload(devices)
        print(json.dumps(payload, indent=2, default=_json_default))
        return

    if not devices:
        print("\nNo Apricorn devices found.\n")
        return

    print(f"\nFound {len(devices)} Apricorn device(s):")
    for idx, dev in enumerate(devices, start=1):
        print(f"\n=== Apricorn Device #{idx} ===")
        printable = _filter_printable_fields(dev.to_dict())
        max_key_len = max((len(str(k)) for k in printable.keys()), default=0)
        for field_name, value in printable.items():
            print(f"  {str(field_name):<{max_key_len}} : {value}")
    print()


def _parse_poke_targets(
    poke_input: str, devices: list[Any]
) -> tuple[list[tuple[str, Any]], list[str]]:
    def _device_is_oob(device: Any) -> bool:
        size = str(getattr(device, "driveSizeGB", "")).strip().upper()
        return size.startswith("N/A")

    def _device_identifier(device: Any) -> Any:
        if _SYSTEM.startswith("win"):
            drive_num = getattr(device, "physicalDriveNum", -1)
            if isinstance(drive_num, int) and drive_num >= 0:
                return drive_num
            return -1

        block_device = getattr(device, "blockDevice", "")
        if isinstance(block_device, str) and block_device.startswith("/dev/"):
            return block_device
        return -1

    targets: list[tuple[str, Any]] = []
    skipped: list[str] = []

    if poke_input.lower() == "all":
        for i, device in enumerate(devices, start=1):
            label = f"#{i}"
            identifier = _device_identifier(device)
            if identifier == -1 or _device_is_oob(device):
                skipped.append(label)
                continue
            targets.append((label, identifier))
        return targets, skipped

    elements = [s.strip() for s in poke_input.split(",") if s.strip()]
    if not elements:
        raise ValueError("No targets")

    invalid: list[str] = []
    seen: set[tuple[str, Any]] = set()

    for token in elements:
        try:
            idx = int(token)
        except ValueError:
            idx = -1

        if idx != -1:
            if not (1 <= idx <= len(devices)):
                invalid.append(token)
                continue

            device = devices[idx - 1]
            identifier = _device_identifier(device)
            label = f"#{idx}"
            if identifier == -1 or _device_is_oob(device):
                skipped.append(label)
                continue
            target = (label, identifier)
            if target not in seen:
                seen.add(target)
                targets.append(target)
            continue

        if _SYSTEM.startswith("win"):
            invalid.append(token)
            continue

        if not token.startswith("/dev/"):
            invalid.append(token)
            continue

        matched_idx = -1
        matched_device = None
        for i, device in enumerate(devices, start=1):
            if getattr(device, "blockDevice", "") == token:
                matched_idx = i
                matched_device = device
                break

        if matched_idx < 0 or matched_device is None:
            invalid.append(token)
            continue

        if _device_is_oob(matched_device):
            skipped.append(token)
            continue

        target = (token, token)
        if target not in seen:
            seen.add(target)
            targets.append(target)

    if invalid:
        raise ValueError(f"Invalid format: {', '.join(invalid)}")
    return targets, skipped


def _validate_poke_permissions(parser: argparse.ArgumentParser) -> None:
    if _SYSTEM.startswith("darwin"):
        parser.error("--poke is not currently supported on macOS.")
    if _SYSTEM.startswith("win") and not is_admin_windows():
        parser.error("--poke requires Administrator privileges on Windows.")


def main() -> None:
    parser = argparse.ArgumentParser(description="USB tool for Apricorn devices.", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-p", "--poke", type=str, metavar="TARGETS")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--profile-scan", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.help:
        print_help = _load_print_help()
        print_help()
        sys.exit(0)

    if args.json and args.poke:
        parser.error("--json cannot be used together with --poke.")

    if args.poke:
        _validate_poke_permissions(parser)

    DeviceManager = _load_device_manager_class()
    manager = DeviceManager()
    scan_message = "Scanning for Apricorn devices..."
    if args.json:
        print(scan_message, file=sys.stderr)
    else:
        print(scan_message)

    try:
        devices = manager.list_devices(
            expanded=args.json,
            profile_scan=args.profile_scan,
        )
    except Exception as e:
        print(f"Error during device scan: {e}", file=sys.stderr)
        devices = None

    if devices is None:
        print("Device scan failed.", file=sys.stderr)
        sys.exit(1)

    if args.poke:
        had_poke_failure = False
        try:
            targets, skipped = _parse_poke_targets(args.poke, devices)
        except ValueError as e:
            parser.error(str(e))

        if not targets and not skipped:
            print("\nNo valid targets specified for poke.\n")
            return

        print()
        for label, identifier in targets:
            if identifier == -1:
                print(f"  Device {label}: SKIPPED (OOB Mode / No drive index)")
                continue

            print(f"Poking device {label}...")
            try:
                if manager.poke(identifier):
                    print(f"  Device {label}: SUCCESS")
                else:
                    print(f"  Device {label}: FAILED")
                    had_poke_failure = True
            except Exception as e:
                print(f"  Device {label}: ERROR ({e})")
                had_poke_failure = True

        for label in skipped:
            print(f"  Device {label}: SKIPPED")
        print()
        if had_poke_failure:
            sys.exit(1)
    else:
        _handle_list_action(devices, json_mode=args.json)


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        exit_code = 130
    except SystemExit as e:
        if isinstance(e.code, int):
            exit_code = e.code
        else:
            exit_code = 0 if e.code is None else 1
    except Exception as e:
        log_path = _write_startup_error_log(e)
        print(f"\nAn unexpected error occurred: {e}", file=sys.stderr)
        if log_path:
            print(f"Full traceback saved to: {log_path}", file=sys.stderr)
        traceback.print_exc()
        exit_code = 1
    finally:
        _pause_before_exit_if_needed()
    sys.exit(exit_code)
