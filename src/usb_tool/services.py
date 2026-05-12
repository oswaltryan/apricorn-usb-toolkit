# src/usb_tool/services.py

import platform
import string
from typing import Any

from .backend.base import AbstractBackend
from .device_version import query_device_version
from .models import UsbDeviceInfo

VERSION_FIELD_NAMES = (
    "scbPartNumber",
    "hardwareVersion",
    "modelID",
    "mcuFW",
    "bridgeFW",
)


def _should_probe_device_version() -> bool:
    system = platform.system().lower()
    return system.startswith("win") or system.startswith("linux") or system.startswith("darwin")


def _normalize_revision(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return ""
    cleaned = "".join(ch for ch in text.lower().removeprefix("0x") if ch in string.hexdigits)
    if not cleaned:
        return ""
    return cleaned.lower().zfill(4)


def should_display_version_fields(device: UsbDeviceInfo) -> bool:
    drive_size = str(getattr(device, "driveSizeGB", "") or "").strip().upper()
    if drive_size.startswith("N/A"):
        return True

    scb_part = str(getattr(device, "scbPartNumber", "N/A") or "").strip()
    if not scb_part or scb_part.upper() == "N/A":
        return False

    bridge_fw = _normalize_revision(getattr(device, "bridgeFW", ""))
    bcd_device = _normalize_revision(getattr(device, "bcdDevice", ""))
    if bridge_fw and bcd_device and bridge_fw != bcd_device:
        return False

    return True


def prune_hidden_version_fields(device: UsbDeviceInfo) -> None:
    if should_display_version_fields(device):
        return

    for field_name in VERSION_FIELD_NAMES:
        try:
            delattr(device, field_name)
        except AttributeError:
            pass


def populate_device_version(
    vendor_id: int,
    product_id: int,
    serial_number: str,
    bsd_name: str | None = None,
    physical_drive_num: int | None = None,
    device_path: str | None = None,
    profile: dict[str, Any] | None = None,
) -> dict:
    """
    Queries the device version and returns a dictionary of formatted strings.
    """
    version_info = {
        "scbPartNumber": "N/A",
        "hardwareVersion": "N/A",
        "modelID": "N/A",
        "mcuFW": "N/A",
        "bridgeFW": "N/A",
    }

    if not _should_probe_device_version():
        return version_info

    try:
        _ver = query_device_version(
            vendor_id,
            product_id,
            serial_number,
            bsd_name=bsd_name,
            physical_drive_num=physical_drive_num,
            device_path=device_path,
            profile=profile,
        )

        if getattr(_ver, "scb_part_number", "N/A") != "N/A":
            version_info["scbPartNumber"] = _ver.scb_part_number

        version_info["hardwareVersion"] = getattr(_ver, "hardware_version", "N/A") or "N/A"

        version_info["modelID"] = getattr(_ver, "model_id", "N/A") or "N/A"

        mj, mn, sb = getattr(_ver, "mcu_fw", (None, None, None))
        if mj is not None and mn is not None and sb is not None:
            version_info["mcuFW"] = f"{mj}.{mn}.{sb}"

        version_info["bridgeFW"] = getattr(_ver, "bridge_fw", "N/A") or "N/A"

    except Exception:
        pass

    return version_info


class DeviceManager:
    def __init__(self, backend: AbstractBackend | None = None):
        if backend is None:
            self.backend = self._get_default_backend()
        else:
            self.backend = backend

    def _get_default_backend(self) -> AbstractBackend:
        system = platform.system().lower()
        if system.startswith("win"):
            from .backend.windows import WindowsBackend

            return WindowsBackend()
        elif system.startswith("linux"):
            from .backend.linux import LinuxBackend

            return LinuxBackend()
        elif system.startswith("darwin"):
            from .backend.macos import MacOSBackend

            return MacOSBackend()
        else:
            raise NotImplementedError(f"Unsupported platform: {system}")

    def list_devices(
        self,
        expanded: bool = False,
        profile_scan: bool = False,
    ) -> list[UsbDeviceInfo]:
        devices = self.backend.scan_devices(expanded=expanded, profile_scan=profile_scan)
        return self.backend.sort_devices(devices)

    def poke(self, device_identifier: Any) -> bool:
        return self.backend.poke_device(device_identifier)
