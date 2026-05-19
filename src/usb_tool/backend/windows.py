# src/usb_tool/backend/windows.py

import ctypes as ct
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from ctypes import wintypes
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from ..constants import EXCLUDED_PIDS
from ..device_config import closest_values
from ..models import UsbDeviceInfo
from ..services import populate_device_version, prune_hidden_version_fields
from ..utils import bytes_to_gb, find_closest, is_oob_mode_size_gb, parse_usb_version
from .base import AbstractBackend

_usb_module: Any | None = None
_usb_import_attempted = False
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_FALSY_VALUES = {"0", "false", "no", "off"}
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_PCI_USB_CONTROLLER_VENDOR_NAMES = {
    "8086": "Intel",
    "1B21": "ASMedia",
    "1912": "Renesas",
    "1033": "Renesas",
    "1022": "AMD",
    "1B73": "Fresco Logic",
    "1106": "VIA",
    "10DE": "NVIDIA",
    "104C": "Texas Instruments",
    "106B": "Apple",
}
_USB_CONTROLLER_NAME_MARKERS = (
    ("ASMedia", "ASMedia"),
    ("Renesas", "Renesas"),
    ("NEC", "Renesas"),
    ("Intel", "Intel"),
    ("AMD", "AMD"),
    ("Fresco", "Fresco Logic"),
    ("VIA", "VIA"),
    ("NVIDIA", "NVIDIA"),
    ("Texas Instruments", "Texas Instruments"),
    ("Apple", "Apple"),
)


def _get_usb_module() -> Any | None:
    global _usb_module, _usb_import_attempted

    if _usb_import_attempted:
        return _usb_module
    _usb_import_attempted = True
    try:
        _usb_module = cast(Any | None, import_module("libusb"))
        if _usb_module is not None:
            try:
                _usb_module.config(LIBUSB=None)
            except Exception:
                pass
    except Exception:  # pragma: no cover - exercised on non-Windows CI
        _usb_module = None
    return _usb_module


class _LazyWin32ComClient:
    def __init__(self) -> None:
        self._module: Any | None = None
        self._error: Exception | None = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        if self._error is not None:
            raise ImportError("pywin32 is required for Windows backend") from self._error
        try:
            self._module = import_module("win32com.client")
        except Exception as exc:  # pragma: no cover - exercised on non-Windows CI
            self._error = exc
            raise ImportError("pywin32 is required for Windows backend") from exc
        return self._module

    def Dispatch(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802
        return self._load().Dispatch(*args, **kwargs)


win32com = SimpleNamespace(client=_LazyWin32ComClient())


_windll = getattr(ct, "windll", None)
kernel32 = getattr(_windll, "kernel32", None) if _windll is not None else None


def _get_last_error() -> int:
    getter = cast(Any, getattr(ct, "get_last_error", None))
    return int(getter()) if getter is not None else 0


def _set_last_error(value: int) -> None:
    setter = cast(Any, getattr(ct, "set_last_error", None))
    if setter is not None:
        setter(value)


def _extract_vid_pid(device_id: str) -> tuple[str, str]:
    if not isinstance(device_id, str):
        return "", ""
    vid_match = re.search(r"VID_([0-9A-Fa-f]{4})", device_id)
    pid_match = re.search(r"PID_([0-9A-Fa-f]{4})", device_id)
    vid = vid_match.group(1).lower() if vid_match else ""
    pid = pid_match.group(1).lower() if pid_match else ""
    return vid, pid


def _classify_usb_controller_name(name: Any = "", device_id: Any = "") -> str:
    name_text = str(name or "").strip()
    for marker, label in _USB_CONTROLLER_NAME_MARKERS:
        if marker.lower() in name_text.lower():
            return label

    device_text = str(device_id or "")
    vendor_match = re.search(r"PCI\\VEN_([0-9A-Fa-f]{4})", device_text)
    if vendor_match:
        return _PCI_USB_CONTROLLER_VENDOR_NAMES.get(vendor_match.group(1).upper(), "N/A")

    return "N/A"


def _is_excluded_pid(pid: str) -> bool:
    if not pid:
        return False
    normalized = pid.lower().split("&", 1)[0].replace("0x", "")
    return normalized in EXCLUDED_PIDS


def _normalize_driver_value(value: Any, default: str = "N/A") -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _escape_wmi_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _normalize_logical_disk_identifier(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return ""

    match = re.search(r'DeviceID="([^"]+)"', text)
    if match:
        return match.group(1)
    return text


def _normalize_disk_media_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "removable" in text:
        return "Removable Media"
    return "Basic Disk"


def _derive_media_type_from_drive_letters(drive_letters: Any, fallback: Any = "Basic Disk") -> str:
    fallback_media_type = _normalize_disk_media_type(fallback)
    letters_text = str(drive_letters or "").strip()
    if not letters_text or letters_text.lower() == "not formatted":
        return fallback_media_type

    get_drive_type = getattr(kernel32, "GetDriveTypeW", None) if kernel32 is not None else None
    if not callable(get_drive_type):
        return fallback_media_type

    saw_fixed = False
    for token in (part.strip() for part in letters_text.split(",")):
        if len(token) < 2 or token[1] != ":" or not token[0].isalpha():
            continue
        root = f"{token[0].upper()}:\\"
        try:
            drive_type = int(get_drive_type(root))
        except Exception:
            continue

        if drive_type == _DRIVE_REMOVABLE:
            return "Removable Media"
        if drive_type == _DRIVE_FIXED:
            saw_fixed = True

    if saw_fixed:
        return "Basic Disk"
    return fallback_media_type


def _has_drive_letter_token(drive_letters: Any) -> bool:
    letters_text = str(drive_letters or "").strip()
    if not letters_text or letters_text.lower() == "not formatted":
        return False

    for token in (part.strip() for part in letters_text.split(",")):
        if len(token) >= 2 and token[0].isalpha() and token[1] == ":":
            return True
    return False


def _normalize_serial_candidates(serial: str) -> list[str]:
    text = str(serial or "").strip()
    if not text:
        return []

    candidates: list[str] = []
    for candidate in (text, text.removeprefix("MSFT30"), text.split("&", 1)[0]):
        normalized = candidate.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _get_attr(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


class _StageTimer:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.start = time.perf_counter() if enabled else 0.0
        self.last = self.start
        self.measurements: list[tuple[str, float]] = []

    def mark(self, label: str) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        self.measurements.append((label, (now - self.last) * 1000.0))
        self.last = now

    def emit(self, suffix: str = "") -> None:
        if not self.enabled:
            return
        total_ms = (time.perf_counter() - self.start) * 1000.0
        parts = [f"{label}={duration_ms:.2f}ms" for label, duration_ms in self.measurements]
        parts.append(f"total={total_ms:.2f}ms")
        line = "windows-scan-profile"
        if suffix:
            line = f"{line} {suffix}"
        print(f"{line}: {', '.join(parts)}", file=sys.stderr)


def _emit_profile_json(line: str, payload: dict[str, Any]) -> None:
    print(
        f"{line}: {json.dumps(payload, sort_keys=True, indent=2)}",
        file=sys.stderr,
    )


class _GUID(ct.Structure):
    _fields_ = [
        ("Data1", ct.c_ulong),
        ("Data2", ct.c_ushort),
        ("Data3", ct.c_ushort),
        ("Data4", ct.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ct.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ct.c_void_p),
    ]


class _STORAGE_DEVICE_NUMBER(ct.Structure):
    _fields_ = [
        ("DeviceType", wintypes.DWORD),
        ("DeviceNumber", wintypes.DWORD),
        ("PartitionNumber", wintypes.DWORD),
    ]


GUID_DEVINTERFACE_DISK = _GUID(
    0x53F56307,
    0xB6BF,
    0x11D0,
    (ct.c_ubyte * 8)(0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B),
)
DIGCF_PRESENT = 0x2
DIGCF_DEVICEINTERFACE = 0x10
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 0x3
INVALID_HANDLE_VALUE = -1


class WindowsBackend(AbstractBackend):
    def __init__(self):
        self._profile_scan_enabled = False
        self._scan_pass_index = 1
        self._storage_metrics_map_cache: dict[int, dict[str, Any]] | None = None
        self._native_scan_binary = self._resolve_native_scan_binary()
        self._native_scan_enabled = self._native_scan_binary is not None
        self._native_scan_path_for_run: Path | None = None

        self.locator: Any = None
        self.service: Any = None
        self._wmi_ready = False
        if not self._native_scan_enabled:
            self._initialize_wmi()

    @property
    def service(self):
        return self._service

    @service.setter
    def service(self, value):
        self._service = value

    def _initialize_wmi(self) -> None:
        self.locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        self.service = self.locator.ConnectServer(".", "root\\cimv2")
        self._wmi_ready = True

    def _ensure_wmi_ready(self) -> None:
        if self._wmi_ready:
            return
        self._initialize_wmi()

    def _resolve_native_scan_binary(self) -> Path | None:
        candidates: list[Path] = []
        repo_root = Path(__file__).resolve().parents[3]
        pyinstaller_bundle_dir = getattr(sys, "_MEIPASS", None)
        if pyinstaller_bundle_dir:
            candidates.append(Path(pyinstaller_bundle_dir) / "windows_native_scan.exe")
        candidates.extend(
            [
                repo_root / "utils" / "windows_native_scan.exe",
                repo_root / "windows_native_scan.exe",
                Path(sys.executable).resolve().parent / "windows_native_scan.exe",
                Path.cwd() / "utils" / "windows_native_scan.exe",
                Path.cwd() / "windows_native_scan.exe",
            ]
        )

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return candidate
        return None

    def scan_devices(
        self,
        expanded: bool = False,
        profile_scan: bool = False,
    ) -> list[UsbDeviceInfo]:
        self._profile_scan_enabled = profile_scan
        self._scan_pass_index = 1

        if self._native_scan_enabled:
            native_devices = self._scan_devices_native(profile_scan=profile_scan)
            if native_devices is not None:
                return self.sort_devices(native_devices)

        self._ensure_wmi_ready()
        devices, lengths = self._perform_scan_pass(minimal=False, expanded=expanded)
        if not devices and len(set(lengths)) != 1 and any(lengths):
            time.sleep(1.0)
            self._scan_pass_index = 2
            devices, _ = self._perform_scan_pass(minimal=False, expanded=expanded)
        return devices or []

    def _scan_devices_native(
        self,
        profile_scan: bool = False,
    ) -> list[UsbDeviceInfo] | None:
        native_path = self._native_scan_binary
        if native_path is None:
            return None

        cmd = [str(native_path)]
        if profile_scan:
            cmd.append("--profile")

        run_start = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception as exc:
            if profile_scan:
                _emit_profile_json(
                    "windows-native-scan-profile",
                    {
                        "exec_failed": True,
                        "error": str(exc),
                    },
                )
            return None

        elapsed_ms = (time.perf_counter() - run_start) * 1000.0
        if result.returncode != 0:
            if profile_scan:
                stderr_text = (result.stderr or "").strip().replace("\n", " | ")
                _emit_profile_json(
                    "windows-native-scan-profile",
                    {
                        "exec_failed": True,
                        "returncode": result.returncode,
                        "elapsed_ms": round(elapsed_ms, 2),
                        "stderr": stderr_text or "n/a",
                    },
                )
            return None

        parse_start = time.perf_counter()
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            if profile_scan:
                _emit_profile_json(
                    "windows-native-scan-profile",
                    {
                        "parse_failed": True,
                        "elapsed_ms": round(elapsed_ms, 2),
                        "error": str(exc),
                    },
                )
            return None

        devices = self._native_payload_to_devices(payload)
        self._correct_native_usb_controllers(devices)
        parse_ms = (time.perf_counter() - parse_start) * 1000.0
        version_query_ms = 0.0
        version_create_file_ms = 0.0
        version_device_io_control_ms = 0.0
        version_parse_payload_ms = 0.0
        for dev_info in devices:
            serial = str(getattr(dev_info, "iSerial", "") or "").strip()
            if not serial:
                continue
            drive_size = str(getattr(dev_info, "driveSizeGB", "") or "").strip()
            if drive_size != "N/A":
                continue

            version_info = self._timed_populate_device_version(
                getattr(dev_info, "idVendor", ""),
                getattr(dev_info, "idProduct", ""),
                serial,
                getattr(dev_info, "physicalDriveNum", -1),
            )
            version_query_ms += version_info.pop("_profile_ms", 0.0)
            version_create_file_ms += version_info.pop("_profile_create_file_ms", 0.0)
            version_device_io_control_ms += version_info.pop("_profile_device_io_control_ms", 0.0)
            version_parse_payload_ms += version_info.pop("_profile_parse_payload_ms", 0.0)
            version_info.pop("_profile_open_error", None)
            version_info.pop("_profile_payload_len", None)

            for key, value in version_info.items():
                setattr(dev_info, key, value)

            prune_hidden_version_fields(dev_info)
        self._native_scan_path_for_run = native_path

        if profile_scan:
            native_profile = payload.get("profile", {})
            native_profile_json: dict[str, Any] = {
                "exec_ms": round(elapsed_ms, 2),
                "parse_ms": round(parse_ms, 2),
                "populate_device_version_total": {
                    "total_ms": round(version_query_ms, 2),
                    "created_file_ms": round(version_create_file_ms, 2),
                    "device_io_control_ms": round(version_device_io_control_ms, 2),
                    "parse_payload_ms": round(version_parse_payload_ms, 2),
                },
                "total_ms": round(elapsed_ms + parse_ms + version_query_ms, 2),
                "device_count": len(devices),
                "native_total_ms": "n/a",
                "native_enumeration_ms": "n/a",
                "native_drive_letters_ms": "n/a",
            }
            if isinstance(native_profile, dict):
                native_profile_json["native_total_ms"] = native_profile.get("totalMs", "n/a")
                native_profile_json["native_enumeration_ms"] = native_profile.get(
                    "enumerationMs", "n/a"
                )
                native_profile_json["native_drive_letters_ms"] = native_profile.get(
                    "driveLettersMs", "n/a"
                )
            _emit_profile_json("windows-native-scan-profile", native_profile_json)

        return devices

    def _correct_native_usb_controllers(self, devices: list[UsbDeviceInfo]) -> None:
        if not devices:
            return
        try:
            self._ensure_wmi_ready()
            controllers = self._get_usb_controllers_wmi()
        except Exception:
            return
        if not controllers:
            return

        controller_by_serial: dict[str, str] = {}
        for controller in controllers:
            device_id = str(controller.get("DeviceID", "") or "")
            serial = device_id.rsplit("\\", 1)[-1].strip()
            name = str(controller.get("ControllerName", "") or "").strip()
            if serial and name and name != "N/A":
                controller_by_serial[serial] = name

        for device in devices:
            for serial in _normalize_serial_candidates(str(getattr(device, "iSerial", "") or "")):
                controller_name = controller_by_serial.get(serial)
                if controller_name:
                    device.usbController = controller_name
                    break

    def _native_payload_to_devices(self, payload: dict[str, Any]) -> list[UsbDeviceInfo]:
        devices: list[UsbDeviceInfo] = []
        raw_devices = payload.get("devices", [])
        if not isinstance(raw_devices, list) or not raw_devices:
            return devices

        first = raw_devices[0]
        if not isinstance(first, dict):
            return devices

        def _key_fn(k: str) -> tuple[int, str]:
            try:
                return (int(k), "")
            except (TypeError, ValueError):
                return (10**9, str(k))

        for key in sorted(first.keys(), key=_key_fn):
            entry = first.get(key, {})
            if not isinstance(entry, dict):
                continue
            drive_letter = self._as_text(entry.get("driveLetter"), "Not Formatted")
            media_type = _derive_media_type_from_drive_letters(
                drive_letter,
                self._as_text(entry.get("mediaType"), "Unknown"),
            )
            try:
                dev_info = UsbDeviceInfo(
                    bcdUSB=self._as_float(entry.get("bcdUSB"), 0.0),
                    idVendor=self._as_text(entry.get("idVendor"), ""),
                    idProduct=self._as_text(entry.get("idProduct"), ""),
                    bcdDevice=self._as_text(entry.get("bcdDevice"), "0000"),
                    iManufacturer=self._as_text(entry.get("iManufacturer"), "Apricorn"),
                    iProduct=self._as_text(entry.get("iProduct"), "Apricorn USB Device"),
                    iSerial=self._as_text(entry.get("iSerial"), ""),
                    driveSizeGB=self._coerce_drive_size(entry.get("driveSizeGB")),
                    mediaType=media_type,
                    driverTransport=self._as_text(entry.get("driverTransport"), "Unknown"),
                    usbController=self._as_text(entry.get("usbController"), "N/A"),
                    usbDriverProvider=self._as_text(entry.get("usbDriverProvider"), "N/A"),
                    usbDriverVersion=self._as_text(entry.get("usbDriverVersion"), "N/A"),
                    usbDriverInf=self._as_text(entry.get("usbDriverInf"), "N/A"),
                    diskDriverProvider=self._as_text(entry.get("diskDriverProvider"), "N/A"),
                    diskDriverVersion=self._as_text(entry.get("diskDriverVersion"), "N/A"),
                    diskDriverInf=self._as_text(entry.get("diskDriverInf"), "N/A"),
                    busNumber=self._as_int(entry.get("busNumber"), -1),
                    deviceAddress=self._as_int(entry.get("deviceAddress"), -1),
                    physicalDriveNum=self._as_int(entry.get("physicalDriveNum"), -1),
                    driveLetter=drive_letter,
                    readOnly=self._as_bool(entry.get("readOnly"), False),
                )
            except Exception:
                continue

            file_system = self._as_text(entry.get("fileSystem"), "")
            has_drive_letter = _has_drive_letter_token(drive_letter)
            if media_type == "Basic Disk" and not has_drive_letter:
                if not file_system or file_system.upper() == "RAW":
                    file_system = "Unallocated"
            if file_system:
                dev_info.fileSystem = file_system

            if "scbPartNumber" in entry:
                dev_info.scbPartNumber = self._as_text(entry.get("scbPartNumber"), "N/A")
            if "hardwareVersion" in entry:
                dev_info.hardwareVersion = self._as_text(entry.get("hardwareVersion"), "N/A")
            if "modelID" in entry:
                dev_info.modelID = self._as_text(entry.get("modelID"), "N/A")
            if "mcuFW" in entry:
                dev_info.mcuFW = self._as_text(entry.get("mcuFW"), "N/A")

            prune_hidden_version_fields(dev_info)
            devices.append(dev_info)

        return devices

    def _as_text(self, value: Any, default: str) -> str:
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    def _as_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _as_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _as_bool(self, value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _TRUTHY_VALUES:
                return True
            if lowered in _FALSY_VALUES:
                return False
        return default

    def _coerce_drive_size(self, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        text = self._as_text(value, "N/A")
        try:
            return int(text)
        except ValueError:
            return text

    def _perform_scan_pass(self, minimal: bool = False, expanded: bool = False):
        timer = _StageTimer(self._profile_scan_enabled)
        wmi_usb_devices = self._get_wmi_usb_devices()
        timer.mark("wmi_usb_devices")
        wmi_diskdrives = self._get_wmi_diskdrives()
        timer.mark("disk_interfaces")
        storage_metrics_map = self._get_usb_storage_metrics_map_wmi()
        self._storage_metrics_map_cache = storage_metrics_map
        timer.mark("usb_storage_metrics")
        wmi_usb_drives = self._get_wmi_usb_drives(wmi_diskdrives)
        timer.mark("usb_drive_build")
        libusb_data = self._get_apricorn_libusb_data()
        timer.mark("libusb_data")
        physical_drives = self._get_physical_drive_number(wmi_usb_drives)
        timer.mark("physical_drive_map")

        device_ids = {
            device.get("device_id", "") for device in wmi_usb_devices if device.get("device_id", "")
        }
        if expanded:
            device_ids.update(
                drive.get("pnpdeviceid", "")
                for drive in wmi_usb_drives
                if drive.get("pnpdeviceid", "")
            )
        signed_driver_map: dict[str, dict[str, str]] = {}
        if expanded:
            signed_driver_map = self._get_signed_driver_info_map(device_ids)
        timer.mark("signed_driver_query")
        if expanded:
            self._apply_usb_driver_info(wmi_usb_devices, signed_driver_map)
        timer.mark("apply_usb_driver_info")
        if expanded:
            self._apply_disk_driver_info(wmi_usb_drives, signed_driver_map)
        timer.mark("apply_disk_driver_info")

        wmi_usb_drives = self._sort_wmi_drives(wmi_usb_devices, wmi_usb_drives)
        timer.mark("sort_wmi_drives")

        include_controller = not minimal
        if include_controller:
            usb_controllers = self._get_usb_controllers_wmi()
            usb_controllers = self._sort_usb_controllers(wmi_usb_devices, usb_controllers)
        else:
            usb_controllers = [{"ControllerName": "N/A"}] * len(wmi_usb_devices)
        timer.mark("usb_controllers")

        libusb_data = self._sort_libusb_data(wmi_usb_devices, libusb_data)
        timer.mark("sort_libusb_data")

        drive_indices = set()
        if not minimal and physical_drives:
            for device, drive in zip(wmi_usb_devices, wmi_usb_drives, strict=False):
                if drive.get("size_gb", 0.0) > 0:
                    serial = device.get("serial", "")
                    idx = physical_drives.get(serial, -1)
                    if idx >= 0:
                        drive_indices.add(idx)

        readonly_map = {
            drive_num: details.get("read_only", False)
            for drive_num, details in storage_metrics_map.items()
        }
        timer.mark("readonly_map")
        drive_letters_map = {}
        if not minimal:
            drive_letters_map = self._get_drive_letters_map_wmi(wmi_usb_drives, drive_indices)
        timer.mark("drive_letters_map")

        devices = self._instantiate_devices(
            wmi_usb_devices,
            wmi_usb_drives,
            usb_controllers,
            libusb_data,
            physical_drives,
            readonly_map,
            drive_letters_map,
            include_controller=include_controller,
            include_drive_letter=not minimal,
        )
        timer.mark("instantiate_devices")
        timer.emit(
            suffix=(
                f"pass={self._scan_pass_index} "
                f"minimal={str(minimal).lower()} expanded={str(expanded).lower()} "
                f"usb={len(wmi_usb_devices)} disks={len(wmi_usb_drives)} "
                f"libusb={len(libusb_data)}"
            )
        )

        self._storage_metrics_map_cache = None

        return devices, [len(wmi_usb_devices), len(wmi_usb_drives), len(libusb_data)]

    def poke_device(self, device_identifier: Any) -> bool:
        # Simplified version of _windows_read10 from poke_device.py
        from ctypes import wintypes

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x1
        FILE_SHARE_WRITE = 0x2
        OPEN_EXISTING = 0x3
        INVALID_HANDLE_VALUE = -1
        IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
        SCSI_IOCTL_DATA_IN = 1

        class SCSI_PASS_THROUGH_DIRECT(ct.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("ScsiStatus", ct.c_byte),
                ("PathId", ct.c_byte),
                ("TargetId", ct.c_byte),
                ("Lun", ct.c_byte),
                ("CdbLength", ct.c_byte),
                ("SenseInfoLength", ct.c_byte),
                ("DataIn", ct.c_byte),
                ("DataTransferLength", wintypes.ULONG),
                ("TimeOutValue", wintypes.ULONG),
                ("DataBuffer", ct.c_void_p),
                ("SenseInfoOffset", wintypes.ULONG),
                ("Cdb", ct.c_byte * 16),
            ]

        class SPTD_WITH_SENSE(ct.Structure):
            _pack_ = 1
            _fields_ = [
                ("sptd", SCSI_PASS_THROUGH_DIRECT),
                ("ucSenseBuf", ct.c_ubyte * 32),
            ]

        windll = getattr(ct, "windll", None)
        if windll is None:
            return False
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is None:
            return False

        drive_path = rf"\\.\PhysicalDrive{device_identifier}"
        h_drive = kernel32.CreateFileW(
            drive_path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if h_drive == INVALID_HANDLE_VALUE:
            return False

        try:
            sptd_sense = SPTD_WITH_SENSE()
            ct.memset(ct.byref(sptd_sense), 0, ct.sizeof(sptd_sense))
            sptd = sptd_sense.sptd

            cdb = [0] * 10
            cdb[0] = 0x28  # READ(10)
            cdb[8] = 1  # Transfer 1 block

            data_buffer = ct.create_string_buffer(512)
            sptd.Length = ct.sizeof(SCSI_PASS_THROUGH_DIRECT)
            sptd.CdbLength = 10
            sptd.SenseInfoLength = 32
            sptd.DataIn = SCSI_IOCTL_DATA_IN
            sptd.DataTransferLength = 512
            sptd.TimeOutValue = 5
            sptd.DataBuffer = ct.addressof(data_buffer)
            sptd.SenseInfoOffset = sptd.Length
            ct.memmove(sptd.Cdb, (ct.c_ubyte * 10)(*cdb), 10)

            returned = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                h_drive,
                IOCTL_SCSI_PASS_THROUGH_DIRECT,
                ct.byref(sptd_sense),
                ct.sizeof(sptd_sense),
                ct.byref(sptd_sense),
                ct.sizeof(sptd_sense),
                ct.byref(returned),
                None,
            )
            return bool(ok and sptd.ScsiStatus == 0)
        finally:
            kernel32.CloseHandle(h_drive)

    def sort_devices(self, devices: list[UsbDeviceInfo]) -> list[UsbDeviceInfo]:
        def _key(dev):
            p_num = getattr(dev, "physicalDriveNum", -1)
            return p_num if isinstance(p_num, int) and p_num >= 0 else float("inf")

        return sorted(devices, key=_key)

    # --- Internal Helpers adapted from legacy windows_usb.py ---

    def get_drive_letter_via_ps(self, drive_index: int) -> str:
        if drive_index < 0:
            return "Not Formatted"
        try:
            cmd = f"(Get-Partition -DiskNumber {drive_index} | Get-Volume).DriveLetter"
            result = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                check=False,
            )
            letter = result.stdout.strip()
            if not letter:
                return "Not Formatted"
            return f"{letter}:" if ":" not in letter else letter
        except Exception:
            return "Not Formatted"

    def _should_retry_scan(self, lengths: list[int]) -> bool:
        if not lengths:
            return False
        if not any(lengths):
            return False
        return len(set(lengths)) != 1

    def _get_wmi_usb_devices(self):
        self._ensure_wmi_ready()
        query = "SELECT * FROM Win32_PnPEntity WHERE DeviceID LIKE 'USB%'"
        devices = self.service.ExecQuery(query)
        info = []
        for d in devices:
            vid, pid = _extract_vid_pid(d.DeviceID)
            if vid == "0984" and not _is_excluded_pid(pid):
                info.append(
                    {
                        "vid": vid,
                        "pid": pid,
                        "manufacturer": "Apricorn",
                        "description": d.Description or "",
                        "device_id": d.DeviceID,
                        "serial": (d.DeviceID.split("\\")[-1] if "\\" in d.DeviceID else ""),
                        "usbDriverProvider": "N/A",
                        "usbDriverVersion": "N/A",
                        "usbDriverInf": "N/A",
                    }
                )
        return info

    def _get_signed_driver_info_map(self, device_ids: set[str]) -> dict[str, dict[str, str]]:
        cleaned_ids = sorted({device_id for device_id in device_ids if device_id})
        if not cleaned_ids:
            return {}

        where_clause = " OR ".join(
            f"DeviceID='{_escape_wmi_string(device_id)}'" for device_id in cleaned_ids
        )
        query = (
            "SELECT DeviceID, DriverProviderName, DriverVersion, InfName "
            f"FROM Win32_PnPSignedDriver WHERE {where_clause}"
        )
        try:
            records = list(self.service.ExecQuery(query))
        except Exception:
            return {}

        info_map = {}
        for record in records:
            device_id = _normalize_driver_value(getattr(record, "DeviceID", None), "")
            if not device_id:
                continue
            info_map[device_id] = {
                "provider": _normalize_driver_value(getattr(record, "DriverProviderName", None)),
                "version": _normalize_driver_value(getattr(record, "DriverVersion", None)),
                "inf": _normalize_driver_value(getattr(record, "InfName", None)),
            }
        return info_map

    def _get_signed_driver_info(self, device_id: str) -> dict[str, str]:
        return self._get_signed_driver_info_map({device_id}).get(
            device_id, {"provider": "N/A", "version": "N/A", "inf": "N/A"}
        )

    def _apply_usb_driver_info(
        self,
        wmi_usb_devices: list[dict[str, Any]],
        driver_info_map: dict[str, dict[str, str]],
    ) -> None:
        for device in wmi_usb_devices:
            info = driver_info_map.get(device.get("device_id", ""), {})
            device["usbDriverProvider"] = info.get("provider", "N/A")
            device["usbDriverVersion"] = info.get("version", "N/A")
            device["usbDriverInf"] = info.get("inf", "N/A")

    def _apply_disk_driver_info(
        self,
        wmi_usb_drives: list[dict[str, Any]],
        driver_info_map: dict[str, dict[str, str]],
    ) -> None:
        for drive in wmi_usb_drives:
            drive["diskDriverInfo"] = driver_info_map.get(
                drive.get("pnpdeviceid", ""),
                {"provider": "N/A", "version": "N/A", "inf": "N/A"},
            )

    def _classify_driver_transport(
        self, usb_device: dict[str, Any], usb_drive: dict[str, Any], scsi_device: bool
    ) -> str:
        pnp_id = str(usb_drive.get("pnpdeviceid", "")).upper()
        if pnp_id.startswith("SCSI\\"):
            return "UAS"
        if pnp_id.startswith("USBSTOR\\"):
            return "BOT"
        if pnp_id.startswith("\\\\?\\USBSTOR#"):
            return "BOT"

        provider = str(usb_device.get("usbDriverProvider", "")).strip().lower()
        if provider.startswith("apricorn"):
            return "Vendor"
        if scsi_device:
            return "UAS"
        return "Unknown"

    def _get_setupapi(self):
        setupapi = ct.WinDLL("setupapi", use_last_error=True)
        setupapi.SetupDiGetClassDevsW.argtypes = [
            ct.POINTER(_GUID),
            ct.c_wchar_p,
            wintypes.HWND,
            wintypes.DWORD,
        ]
        setupapi.SetupDiGetClassDevsW.restype = ct.c_void_p
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            ct.c_void_p,
            ct.c_void_p,
            ct.POINTER(_GUID),
            wintypes.DWORD,
            ct.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ]
        setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ct.c_void_p,
            ct.POINTER(_SP_DEVICE_INTERFACE_DATA),
            ct.c_void_p,
            wintypes.DWORD,
            ct.POINTER(wintypes.DWORD),
            ct.c_void_p,
        ]
        setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ct.c_void_p]
        setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
        return setupapi

    def _query_storage_device_number(self, path: str) -> int | None:
        if kernel32 is None:
            return None
        handle = kernel32.CreateFileW(
            path,
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return None
        try:
            device_number = _STORAGE_DEVICE_NUMBER()
            returned_bytes = wintypes.DWORD(0)
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_GET_DEVICE_NUMBER,
                None,
                0,
                ct.byref(device_number),
                ct.sizeof(device_number),
                ct.byref(returned_bytes),
                None,
            )
            if ok == 0:
                return None
            return int(device_number.DeviceNumber)
        finally:
            kernel32.CloseHandle(handle)

    def _normalize_interface_path(self, path: str) -> str:
        return str(path or "").strip().lower()

    def _extract_product_from_interface_path(self, path: str) -> str:
        normalized = self._normalize_interface_path(path)
        match = re.search(r"prod_([^#&]+)", normalized)
        if not match:
            return ""
        return match.group(1).replace("_", " ").strip().title()

    def _get_disk_interface_records(self) -> list[dict[str, Any]]:
        start = time.perf_counter()
        setupapi = self._get_setupapi()
        device_info_set = setupapi.SetupDiGetClassDevsW(
            ct.byref(GUID_DEVINTERFACE_DISK),
            None,
            None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if device_info_set in (None, 0, ct.c_void_p(-1).value):
            return []

        records: list[dict[str, Any]] = []
        try:
            index = 0
            while True:
                interface_data = _SP_DEVICE_INTERFACE_DATA()
                interface_data.cbSize = ct.sizeof(_SP_DEVICE_INTERFACE_DATA)
                ok = setupapi.SetupDiEnumDeviceInterfaces(
                    device_info_set,
                    None,
                    ct.byref(GUID_DEVINTERFACE_DISK),
                    index,
                    ct.byref(interface_data),
                )
                if ok == 0:
                    last_error = _get_last_error()
                    if last_error == ERROR_NO_MORE_ITEMS:
                        break
                    index += 1
                    continue

                required_size = wintypes.DWORD(0)
                _set_last_error(0)
                setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_info_set,
                    ct.byref(interface_data),
                    None,
                    0,
                    ct.byref(required_size),
                    None,
                )
                last_error = _get_last_error()
                if required_size.value == 0 or last_error not in (
                    0,
                    ERROR_INSUFFICIENT_BUFFER,
                ):
                    index += 1
                    continue

                detail_buffer = ct.create_string_buffer(required_size.value)
                ct.cast(detail_buffer, ct.POINTER(wintypes.DWORD)).contents.value = (
                    8 if ct.sizeof(ct.c_void_p) == 8 else 6
                )

                ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_info_set,
                    ct.byref(interface_data),
                    detail_buffer,
                    required_size.value,
                    ct.byref(required_size),
                    None,
                )
                if ok == 0:
                    index += 1
                    continue

                device_path = ct.wstring_at(ct.addressof(detail_buffer) + ct.sizeof(wintypes.DWORD))
                normalized_path = self._normalize_interface_path(device_path)
                device_number = self._query_storage_device_number(device_path)
                records.append(
                    {
                        "device_path": device_path,
                        "normalized_path": normalized_path,
                        "device_number": device_number,
                        "product_hint": self._extract_product_from_interface_path(device_path),
                    }
                )
                index += 1
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(device_info_set)

        if self._profile_scan_enabled:
            print(
                "windows-disk-interface-profile: "
                f"pass={self._scan_pass_index} "
                f"records={len(records)} "
                f"duration_ms={(time.perf_counter() - start) * 1000.0:.2f}",
                file=sys.stderr,
            )
        return records

    def _get_usb_storage_metrics_map_wmi(self) -> dict[int, dict[str, Any]]:
        try:
            storage = self.locator.ConnectServer(".", "root\\Microsoft\\Windows\\Storage")
            disks = storage.ExecQuery(
                "SELECT Number, IsReadOnly, BusType, Size, FriendlyName, Model FROM MSFT_Disk"
            )
            return {
                int(d.Number): {
                    "read_only": bool(d.IsReadOnly),
                    "size_gb": bytes_to_gb(int(getattr(d, "Size", 0) or 0)),
                    "friendly_name": str(getattr(d, "FriendlyName", "") or ""),
                    "model": str(getattr(d, "Model", "") or ""),
                }
                for d in disks
                if int(d.BusType) == 7
            }
        except Exception:
            return {}

    def _get_disk_media_type_map_wmi(self) -> dict[int, str]:
        self._ensure_wmi_ready()
        try:
            drives = self.service.ExecQuery("SELECT Index, MediaType FROM Win32_DiskDrive")
        except Exception:
            return {}

        media_type_map: dict[int, str] = {}
        for drive in drives:
            try:
                drive_index = int(getattr(drive, "Index", -1))
            except (TypeError, ValueError):
                continue
            if drive_index < 0:
                continue
            media_type_map[drive_index] = _normalize_disk_media_type(
                getattr(drive, "MediaType", "")
            )
        return media_type_map

    def _match_disk_interface_record(
        self,
        usb_device: dict[str, Any],
        disk_interfaces: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        candidates = _normalize_serial_candidates(usb_device.get("serial", ""))
        for candidate in candidates:
            token = f"#{candidate.lower()}&0#"
            for record in disk_interfaces:
                path = record.get("normalized_path", "")
                if "ven_apricorn" not in path:
                    continue
                if token in path:
                    return record
        return None

    def _build_usb_drives_from_interfaces(
        self,
        wmi_usb_devices: list[dict[str, Any]],
        disk_interfaces: list[dict[str, Any]],
        storage_metrics_map: dict[int, dict[str, Any]],
        media_type_map: dict[int, str],
    ) -> list[dict[str, Any]]:
        drives: list[dict[str, Any]] = []
        for device in wmi_usb_devices:
            matched = self._match_disk_interface_record(device, disk_interfaces)
            if matched is None:
                if self._profile_scan_enabled:
                    print(
                        "windows-disk-interface-match-profile: "
                        f"pass={self._scan_pass_index} "
                        f"serial={device.get('serial', '')} matched=false",
                        file=sys.stderr,
                    )
                continue

            drive_num = matched.get("device_number")
            metrics = storage_metrics_map.get(drive_num, {}) if isinstance(drive_num, int) else {}
            media_type = (
                media_type_map.get(drive_num, "Basic Disk")
                if isinstance(drive_num, int)
                else "Basic Disk"
            )
            product = matched.get("product_hint", "") or device.get("description", "")
            drives.append(
                {
                    "caption": product or device.get("description", ""),
                    "size_gb": float(metrics.get("size_gb", 0.0) or 0.0),
                    "iProduct": product,
                    "pnpdeviceid": matched.get("device_path", ""),
                    "serial": device.get("serial", ""),
                    "mediaType": media_type,
                    "diskDriverInfo": {
                        "provider": "N/A",
                        "version": "N/A",
                        "inf": "N/A",
                    },
                    "Index": drive_num if isinstance(drive_num, int) else -1,
                    "DeviceID": (
                        rf"\\.\PHYSICALDRIVE{drive_num}"
                        if isinstance(drive_num, int) and drive_num >= 0
                        else ""
                    ),
                }
            )
            if self._profile_scan_enabled:
                print(
                    "windows-disk-interface-match-profile: "
                    f"pass={self._scan_pass_index} "
                    f"serial={device.get('serial', '')} "
                    f"matched=true drive_num={drive_num} "
                    f"path={matched.get('device_path', '')}",
                    file=sys.stderr,
                )
        return drives

    def _get_wmi_diskdrives(self, query: str | None = None):
        del query
        return self._get_disk_interface_records()

    def _get_wmi_usb_drives(self, wmi_diskdrives):
        return self._build_usb_drives_from_interfaces(
            self._get_wmi_usb_devices(),
            list(wmi_diskdrives or []),
            getattr(self, "_storage_metrics_map_cache", None)
            or self._get_usb_storage_metrics_map_wmi(),
            self._get_disk_media_type_map_wmi(),
        )

    def _get_apricorn_libusb_data(self):
        usb = _get_usb_module()
        if usb is None:
            return []
        devices = []
        ctx = ct.POINTER(usb.context)()
        if usb.init(ct.byref(ctx)) != 0:
            return []
        try:
            dev_list = ct.POINTER(ct.POINTER(usb.device))()
            cnt = usb.get_device_list(ctx, ct.byref(dev_list))
            for i in range(cnt):
                dev = dev_list[i]
                desc = usb.device_descriptor()
                if usb.get_device_descriptor(dev, ct.byref(desc)) == 0:
                    vid = f"{desc.idVendor:04x}"
                    pid = f"{desc.idProduct:04x}"
                    if vid == "0984" and not _is_excluded_pid(pid):
                        devices.append(
                            {
                                "iProduct": pid,
                                "bcdDevice": f"{desc.bcdDevice:04x}",
                                "bcdUSB": float(parse_usb_version(desc.bcdUSB)),
                                "bus_number": usb.get_bus_number(dev),
                                "dev_address": usb.get_device_address(dev),
                            }
                        )
            usb.free_device_list(dev_list, 1)
        finally:
            usb.exit(ctx)
        return devices

    def _get_physical_drive_number(self, wmi_diskdrives):
        drives = {}
        for r in wmi_diskdrives or []:
            explicit_serial = str(_get_attr(r, "serial", "") or "").strip()
            if explicit_serial:
                try:
                    drive_num = int(_get_attr(r, "Index", -1))
                except (TypeError, ValueError):
                    drive_num = -1
                if drive_num >= 0:
                    drives[explicit_serial] = drive_num
                    continue

            pnp_device_id = str(_get_attr(r, "pnpdeviceid", _get_attr(r, "PNPDeviceID", "")) or "")
            if "SATAWIRE" in pnp_device_id or "FLASH_DISK" in pnp_device_id:
                continue
            if "APRI" in pnp_device_id.upper():
                try:
                    drive_num = int(_get_attr(r, "Index", -1))
                    source = pnp_device_id.replace("#", "\\")
                    serial_part = source.rsplit("\\", 1)[1]
                    serial = serial_part.split("&")[0]
                    drives[serial] = drive_num
                except (ValueError, TypeError, IndexError):
                    continue
        return drives

    def _get_usb_controllers_wmi(self):
        controllers = []
        try:
            records = self.service.ExecQuery("SELECT * FROM Win32_USBControllerDevice")
            for r in records:
                try:
                    ctrl = self.service.Get(r.Antecedent)
                    dev = self.service.Get(r.Dependent)
                    vid, pid = _extract_vid_pid(dev.DeviceID)
                    if vid == "0984" and not _is_excluded_pid(pid):
                        controllers.append(
                            {
                                "DeviceID": str(dev.DeviceID).upper(),
                                "ControllerName": _classify_usb_controller_name(
                                    getattr(ctrl, "Name", ""),
                                    getattr(ctrl, "DeviceID", ""),
                                ),
                            }
                        )
                except Exception:
                    continue
        except Exception:
            pass
        return controllers

    def _get_usb_readonly_status_map_wmi(self):
        try:
            storage = self.locator.ConnectServer(".", "root\\Microsoft\\Windows\\Storage")
            disks = storage.ExecQuery("SELECT Number, IsReadOnly, BusType FROM MSFT_Disk")
            return {int(d.Number): bool(d.IsReadOnly) for d in disks if int(d.BusType) == 7}
        except Exception:
            return {}

    def _get_drive_letters_map_wmi(self, wmi_diskdrives, drive_indices):
        mapping = {}
        if not drive_indices:
            if self._profile_scan_enabled:
                print(
                    "windows-drive-letter-profile: "
                    f"pass={self._scan_pass_index} skipped=no_candidate_drive_indices",
                    file=sys.stderr,
                )
            return mapping

        try:
            partition_links = list(
                self.service.ExecQuery(
                    "SELECT Antecedent, Dependent FROM Win32_DiskDriveToDiskPartition"
                )
            )
            logical_links = list(
                self.service.ExecQuery(
                    "SELECT Antecedent, Dependent FROM Win32_LogicalDiskToPartition"
                )
            )
        except Exception:
            if self._profile_scan_enabled:
                print(
                    "windows-drive-letter-profile: "
                    f"pass={self._scan_pass_index} stage=bulk_query_exception "
                    f"error={sys.exc_info()[1]}",
                    file=sys.stderr,
                )
            return mapping

        partition_to_letters: dict[str, list[str]] = {}
        for link in logical_links:
            antecedent = str(getattr(link, "Antecedent", "") or "")
            dependent = _normalize_logical_disk_identifier(getattr(link, "Dependent", ""))
            if dependent:
                partition_to_letters.setdefault(antecedent, []).append(dependent)

        for d in wmi_diskdrives or []:
            try:
                idx = int(_get_attr(d, "Index", -1))
            except (TypeError, ValueError):
                continue

            if drive_indices and idx not in drive_indices:
                continue

            escaped_device_id = _escape_wmi_string(str(_get_attr(d, "DeviceID", "") or ""))
            disk_token = f'DeviceID="{escaped_device_id}"'
            index_token = f"Index={idx}"
            matching_partitions: list[str] = []
            for link in partition_links:
                antecedent = str(getattr(link, "Antecedent", "") or "")
                dependent = str(getattr(link, "Dependent", "") or "")
                if disk_token in antecedent or index_token in antecedent:
                    matching_partitions.append(dependent)

            if self._profile_scan_enabled:
                print(
                    "windows-drive-letter-profile: "
                    f"pass={self._scan_pass_index} disk_index={idx} "
                    f"stage=bulk_partitions count={len(matching_partitions)} "
                    f"device_id={_get_attr(d, 'DeviceID', '')}",
                    file=sys.stderr,
                )

            letters: list[str] = []
            for partition in matching_partitions:
                partition_letters = partition_to_letters.get(partition, [])
                letters.extend(partition_letters)
                if self._profile_scan_enabled:
                    print(
                        "windows-drive-letter-profile: "
                        f"pass={self._scan_pass_index} disk_index={idx} "
                        f"stage=bulk_partition_result "
                        f"partition={partition} letters={', '.join(partition_letters) or 'none'}",
                        file=sys.stderr,
                    )

            mapping[idx] = ", ".join(letters) if letters else "Not Formatted"
        return mapping

    def _sort_wmi_drives(self, wmi_usb_devices, wmi_usb_drives):
        drives_to_process = list(wmi_usb_drives)
        sorted_drives = []
        for device in wmi_usb_devices:
            serial = device.get("serial", "")
            found_idx = -1
            best_score = -1
            for i, drive in enumerate(drives_to_process):
                pnp_id = drive["pnpdeviceid"]
                instance_id = pnp_id.rsplit("\\", 1)[-1]
                pnp_serial = instance_id.split("&")[0]
                score = -1
                if serial and serial == pnp_serial:
                    score = 3
                elif serial and (pnp_serial in serial or serial in pnp_serial):
                    score = 2
                elif (
                    "SCSI" in device.get("description", "")
                    and "SCSI" in pnp_id
                    and "PADLOCK_NVX" in pnp_id
                ):
                    score = 1
                if score > best_score:
                    best_score = score
                    found_idx = i
            if found_idx != -1 and best_score > 0:
                sorted_drives.append(drives_to_process.pop(found_idx))
        return sorted_drives + drives_to_process

    def _sort_usb_controllers(self, wmi_usb_devices, usb_controllers):
        to_process = list(usb_controllers)
        sorted_ctrls = []
        for device in wmi_usb_devices:
            serial = device["serial"]
            for i, ctrl in enumerate(to_process):
                if ctrl["DeviceID"].rsplit("\\", 1)[-1] == serial:
                    sorted_ctrls.append(to_process.pop(i))
                    break
        return sorted_ctrls + to_process

    def _sort_libusb_data(self, wmi_usb_devices, libusb_data):
        if not libusb_data:
            return []
        pid_map = defaultdict(list)
        for entry in libusb_data:
            pid_map[entry.get("iProduct")].append(entry)

        sorted_data = []
        used = set()
        for device in wmi_usb_devices:
            pid = device.get("pid")
            candidates = pid_map.get(pid, [])
            candidates.sort(key=lambda x: x.get("bcdUSB", 0.0), reverse=True)
            best = None
            for c in candidates:
                key = (c.get("iProduct"), c.get("bcdDevice"))
                if key not in used:
                    best = c
                    used.add(key)
                    break
            if not best and candidates:
                best = candidates[0]
            sorted_data.append(
                best
                or {
                    "iProduct": pid,
                    "bcdDevice": "0000",
                    "bcdUSB": 0.0,
                    "bus_number": -1,
                    "dev_address": -1,
                }
            )
        return sorted_data

    def _instantiate_devices(
        self,
        wmi_usb_devices,
        wmi_usb_drives,
        usb_controllers,
        libusb_data,
        physical_drives,
        readonly_map,
        drive_letters_map,
        include_controller,
        include_drive_letter,
    ):
        devices = []
        version_query_ms = 0.0
        drive_letter_fallback_ms = 0.0
        count = min(
            len(wmi_usb_devices),
            len(wmi_usb_drives),
            len(usb_controllers),
            len(libusb_data),
        )
        for i in range(count):
            device_start = time.perf_counter()
            pid = wmi_usb_devices[i]["pid"]
            vid = wmi_usb_devices[i]["vid"]
            serial = wmi_usb_devices[i]["serial"]
            if serial.startswith("MSFT30"):
                scsi, serial = True, serial[6:]
            else:
                scsi = False
            driver_transport = self._classify_driver_transport(
                wmi_usb_devices[i], wmi_usb_drives[i], scsi
            )

            drive_num = -1
            if physical_drives:
                for k, v in physical_drives.items():
                    if k == serial:
                        drive_num = v
                        break

            size_raw = wmi_usb_drives[i]["size_gb"]
            is_oob_size = size_raw == 0.0 or is_oob_mode_size_gb(size_raw)
            size_gb = (
                "N/A (OOB Mode)" if is_oob_size else find_closest(size_raw, closest_values[pid][1])
            )
            drive_letter = "Not Formatted"
            media_type = _normalize_disk_media_type(wmi_usb_drives[i].get("mediaType", "Unknown"))

            version_info = (
                {}
                if not serial
                else self._timed_populate_device_version(
                    vid,
                    pid,
                    serial,
                    drive_num,
                )
            )
            version_query_ms += version_info.pop("_profile_ms", 0.0)
            for profile_key in [k for k in list(version_info.keys()) if k.startswith("_profile_")]:
                version_info.pop(profile_key, None)

            dev_info = UsbDeviceInfo(
                bcdUSB=libusb_data[i]["bcdUSB"],
                idVendor=vid,
                idProduct=pid,
                bcdDevice=libusb_data[i]["bcdDevice"],
                iManufacturer="Apricorn",
                iProduct=wmi_usb_drives[i]["iProduct"],
                iSerial=serial,
                driverTransport=driver_transport,
                driveSizeGB=size_gb,
                mediaType=media_type,
                usbDriverProvider=wmi_usb_devices[i].get("usbDriverProvider", "N/A"),
                usbDriverVersion=wmi_usb_devices[i].get("usbDriverVersion", "N/A"),
                usbDriverInf=wmi_usb_devices[i].get("usbDriverInf", "N/A"),
                diskDriverProvider=wmi_usb_drives[i]
                .get("diskDriverInfo", {})
                .get("provider", "N/A"),
                diskDriverVersion=wmi_usb_drives[i].get("diskDriverInfo", {}).get("version", "N/A"),
                diskDriverInf=wmi_usb_drives[i].get("diskDriverInfo", {}).get("inf", "N/A"),
                **version_info,
            )
            if include_controller:
                dev_info.usbController = usb_controllers[i]["ControllerName"]
            else:
                try:
                    delattr(dev_info, "usbController")
                except AttributeError:
                    pass
            dev_info.busNumber = libusb_data[i]["bus_number"]
            dev_info.deviceAddress = libusb_data[i]["dev_address"]
            dev_info.physicalDriveNum = drive_num
            if include_drive_letter:
                drive_letter = drive_letters_map.get(drive_num, "Not Formatted")
                if (
                    not is_oob_size
                    and isinstance(drive_num, int)
                    and drive_num >= 0
                    and drive_letter == "Not Formatted"
                ):
                    if self._profile_scan_enabled:
                        print(
                            "windows-drive-letter-profile: "
                            f"pass={self._scan_pass_index} disk_index={drive_num} "
                            f"stage=fallback_triggered "
                            f"serial={serial} size_raw={size_raw}",
                            file=sys.stderr,
                        )
                    drive_letter_start = time.perf_counter()
                    drive_letter = self.get_drive_letter_via_ps(drive_num)
                    drive_letter_fallback_ms += (time.perf_counter() - drive_letter_start) * 1000.0
                    if self._profile_scan_enabled:
                        print(
                            "windows-drive-letter-profile: "
                            f"pass={self._scan_pass_index} disk_index={drive_num} "
                            f"stage=fallback_result "
                            f"letter={drive_letter or 'Not Formatted'}",
                            file=sys.stderr,
                        )
                dev_info.driveLetter = drive_letter or "Not Formatted"
                dev_info.mediaType = _derive_media_type_from_drive_letters(
                    drive_letter,
                    media_type,
                )
            else:
                try:
                    delattr(dev_info, "driveLetter")
                except AttributeError:
                    pass
            dev_info.readOnly = readonly_map.get(drive_num, False)

            prune_hidden_version_fields(dev_info)
            devices.append(dev_info)
            if self._profile_scan_enabled:
                print(
                    "windows-instantiate-device-profile: "
                    f"pass={self._scan_pass_index} "
                    f"index={i + 1} "
                    f"serial={serial} "
                    f"drive_num={drive_num} "
                    f"size_mode={'oob' if is_oob_size else 'mounted_media'} "
                    f"total_ms={(time.perf_counter() - device_start) * 1000.0:.2f}",
                    file=sys.stderr,
                )
        if self._profile_scan_enabled:
            _emit_profile_json(
                "windows-scan-profile-details",
                {
                    "pass": self._scan_pass_index,
                    "populate_device_version_total_ms": round(version_query_ms, 2),
                    "drive_letter_fallback_total_ms": round(drive_letter_fallback_ms, 2),
                    "device_count": count,
                },
            )
        return devices

    def _timed_populate_device_version(
        self, vid: str, pid: str, serial: str, drive_num: int
    ) -> dict[str, Any]:
        start = time.perf_counter()
        profile: dict[str, Any] = {
            "serial": serial,
            "drive_num": drive_num,
        }
        version_info = populate_device_version(
            int(vid, 16),
            int(pid, 16),
            serial,
            physical_drive_num=drive_num if drive_num != -1 else None,
            profile=profile,
        )
        total_ms = (time.perf_counter() - start) * 1000.0
        version_info["_profile_ms"] = total_ms
        try:
            version_info["_profile_create_file_ms"] = float(
                profile.get("create_file_ms", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            version_info["_profile_create_file_ms"] = 0.0
        try:
            version_info["_profile_device_io_control_ms"] = float(
                profile.get("device_io_control_ms", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            version_info["_profile_device_io_control_ms"] = 0.0
        try:
            version_info["_profile_parse_payload_ms"] = float(
                profile.get("parse_payload_ms", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            version_info["_profile_parse_payload_ms"] = 0.0
        return version_info
