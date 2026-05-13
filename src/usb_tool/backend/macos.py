# src/usb_tool/backend/macos.py

import json
import os
import plistlib
import re
import subprocess
import sys
import time
from typing import Any

from ..constants import EXCLUDED_PIDS
from ..device_config import closest_values
from ..models import UsbDeviceInfo
from ..services import populate_device_version, prune_hidden_version_fields
from ..utils import bytes_to_gb, find_closest, is_oob_mode_size_bytes
from .base import AbstractBackend


def _normalize_pid(pid: str) -> str:
    if not isinstance(pid, str):
        return ""
    cleaned = pid.lower().replace("0x", "")
    return cleaned.split("&", 1)[0][:4]


def _is_excluded_pid(pid: str) -> bool:
    return _normalize_pid(pid) in EXCLUDED_PIDS


def _normalize_whole_disk_path(bsd_name: str) -> str:
    if not isinstance(bsd_name, str):
        return ""

    disk_name = bsd_name.strip()
    if disk_name.startswith("/dev/"):
        disk_name = disk_name.removeprefix("/dev/")

    if not disk_name.startswith("disk"):
        return ""

    disk_name = re.sub(r"s\d+$", "", disk_name)
    return f"/dev/{disk_name}"


def _normalize_raw_disk_path(device_path: str) -> str:
    if not isinstance(device_path, str):
        return ""

    normalized = device_path.strip()
    if normalized.startswith("/dev/rdisk"):
        return normalized
    if normalized.startswith("/dev/disk"):
        return normalized.replace("/dev/disk", "/dev/rdisk", 1)
    return ""


def _classify_media_type(removable_value: Any) -> str:
    if isinstance(removable_value, bool):
        return "Removable Media" if removable_value else "Basic Disk"

    text = str(removable_value).strip().lower()
    if text in {"yes", "true", "1"}:
        return "Removable Media"
    if text in {"no", "false", "0"}:
        return "Basic Disk"
    return "Unknown"


def _fallback_media_type(pid: str, product_name: str) -> str:
    product_hint = closest_values.get(pid, ("", []))[0]
    normalized = " ".join(part for part in (product_name, product_hint) if part).lower()
    if any(token in normalized for token in ("secure key", "fortress", "padlock", "aegis")):
        return "Basic Disk"
    return "Unknown"


def _parse_media_size_bytes(media: dict[str, Any]) -> int | float | None:
    size_in_bytes = media.get("size_in_bytes")
    if isinstance(size_in_bytes, (int, float)) and not isinstance(size_in_bytes, bool):
        return size_in_bytes

    size_text = str(media.get("size", "")).strip()
    if not size_text:
        return None

    match = re.fullmatch(
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMGTPE]?i?B|bytes?)(?:\s*\([^)]*\))?",
        size_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    value = float(match.group(1).replace(",", ""))
    unit = match.group(2).lower()
    multipliers = {
        "byte": 1,
        "bytes": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
        "pb": 1000**5,
        "pib": 1024**5,
        "eb": 1000**6,
        "eib": 1024**6,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        return None
    return value * multiplier


def _classify_mass_storage_protocol(protocol_value: Any) -> str:
    try:
        protocol = int(str(protocol_value).strip(), 0)
    except (TypeError, ValueError):
        return "Unknown"

    if protocol == 0x62:
        return "UAS"
    if protocol == 0x50:
        return "BOT"
    if protocol >= 0:
        return "Vendor"
    return "Unknown"


def _extract_ioreg_dict_value(payload: str, key: str) -> str:
    if not isinstance(payload, str) or not payload:
        return ""

    match = re.search(rf'"{re.escape(key)}"\s*=\s*("[^"]*"|[^,}}\n]+)', payload)
    if not match:
        return ""
    return match.group(1).strip().strip('"')


def _parse_ioreg_bool(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in {"yes", "true", "1"}:
        return True
    if text in {"no", "false", "0"}:
        return False
    return None


def _emit_profile_event(enabled: bool, prefix: str, **fields: Any) -> None:
    if not enabled:
        return

    parts = [f"{key}={value}" for key, value in fields.items()]
    suffix = f" {' '.join(parts)}" if parts else ""
    print(f"{prefix}:{suffix}", file=sys.stderr)


def _emit_profile_summary(
    enabled: bool,
    prefix: str,
    metrics: list[tuple[str, float]],
    **fields: Any,
) -> None:
    if not enabled:
        return

    context = " ".join(f"{key}={value}" for key, value in fields.items())
    metric_text = ", ".join(f"{label}={duration_ms:.2f}ms" for label, duration_ms in metrics)
    line = prefix if not context else f"{prefix} {context}"
    print(f"{line}: {metric_text}", file=sys.stderr)


class MacOSBackend(AbstractBackend):
    def scan_devices(
        self,
        expanded: bool = False,
        profile_scan: bool = False,
    ) -> list[UsbDeviceInfo]:
        scan_start = time.perf_counter()
        all_drives = self._list_usb_drives()
        system_profiler_ms = (time.perf_counter() - scan_start) * 1000.0

        ioreg_start = time.perf_counter()
        storage_info_map = self._get_mass_storage_info_map()
        ioreg_mass_storage_ms = (time.perf_counter() - ioreg_start) * 1000.0

        devices = []
        version_query_ms = 0.0
        diskutil_fallback_ms = 0.0
        diskutil_fallback_count = 0
        device_build_start = time.perf_counter()
        for drive in all_drives:
            name = drive.get("_name")
            if not name:
                continue

            vid = drive.get("vendor_id", "").replace("0x", "")[:4].lower()
            pid_raw = drive.get("product_id", "").replace("0x", "").lower()
            pid = _normalize_pid(pid_raw)
            if vid != "0984" or _is_excluded_pid(pid):
                continue

            serial = drive.get("serial_num", "")
            bcd_dev = drive.get("bcd_device", "").replace(".", "")
            storage_info = storage_info_map.get(serial) or {}
            if not storage_info and not serial:
                storage_info = storage_info_map.get(name) or {}

            size_gb = "0"
            media_type = "Unknown"
            bsd_name = ""
            block_device = storage_info.get("blockDevice", "")

            if "Media" in drive and drive["Media"]:
                m = drive["Media"][0]
                media_type = _classify_media_type(m.get("removable_media"))
                bsd_name = m.get("bsd_name", "")
                block_device = _normalize_whole_disk_path(bsd_name)
                media_size_bytes = _parse_media_size_bytes(m)
                if (
                    media_size_bytes is None
                    or media_size_bytes <= 0
                    or is_oob_mode_size_bytes(media_size_bytes)
                ):
                    size_gb = "N/A (OOB Mode)"
                else:
                    size_raw = bytes_to_gb(media_size_bytes)
                    closest = find_closest(size_raw, closest_values.get(pid, (0, []))[1])
                    size_gb = str(closest) if closest is not None else "0"
            else:
                size_gb = "N/A (OOB Mode)"

            if media_type == "Unknown" and block_device:
                diskutil_fallback_count += 1
                _emit_profile_event(
                    profile_scan,
                    "macos-media-type-profile",
                    stage="diskutil_fallback_triggered",
                    block_device=block_device,
                    serial=serial or "unknown",
                )
                media_type_start = time.perf_counter()
                media_type = self._get_media_type_from_diskutil(block_device)
                diskutil_fallback_ms += (time.perf_counter() - media_type_start) * 1000.0
                _emit_profile_event(
                    profile_scan,
                    "macos-media-type-profile",
                    stage="diskutil_fallback_result",
                    block_device=block_device,
                    media_type=media_type,
                )

            if media_type == "Unknown":
                media_type = _fallback_media_type(pid, name)

            version_info = {}
            if self._should_probe_version_info(size_gb, block_device):
                version_info = self._timed_populate_device_version(
                    vid,
                    pid,
                    serial,
                    block_device or bsd_name,
                    size_gb,
                    profile_scan,
                )
                version_query_ms += version_info.pop("_profile_ms", 0.0)
            else:
                _emit_profile_event(
                    profile_scan,
                    "macos-version-profile",
                    stage="skipped",
                    reason="mounted_media",
                    block_device=block_device,
                    serial=serial or "unknown",
                )

            dev_info = UsbDeviceInfo(
                bcdUSB=(3.0 if int(drive.get("bus_power", "0")) > 500 else 2.0),
                idVendor=vid,
                idProduct=pid,
                bcdDevice=f"0{bcd_dev}" if bcd_dev else "N/A",
                iManufacturer=drive.get("manufacturer", "Apricorn"),
                iProduct=name,
                iSerial=serial,
                driverTransport=storage_info.get("driverTransport", "Unknown"),
                usbController=drive.get("host_controller", "N/A"),
                driveSizeGB=str(size_gb),
                mediaType=media_type,
                readOnly=bool(storage_info.get("readOnly", False)),
                **version_info,
            )
            if block_device:
                dev_info.blockDevice = block_device

            prune_hidden_version_fields(dev_info)
            devices.append(dev_info)

        device_build_ms = (time.perf_counter() - device_build_start) * 1000.0
        _emit_profile_event(
            profile_scan,
            "macos-scan-profile details",
            populate_device_version_total=f"{version_query_ms:.2f}ms",
            diskutil_fallback_total=f"{diskutil_fallback_ms:.2f}ms",
            diskutil_fallback_count=diskutil_fallback_count,
            device_count=len(devices),
        )
        total_ms = (time.perf_counter() - scan_start) * 1000.0
        _emit_profile_summary(
            profile_scan,
            "macos-scan-profile",
            [
                ("system_profiler", system_profiler_ms),
                ("ioreg_mass_storage", ioreg_mass_storage_ms),
                ("device_build", device_build_ms),
                ("total", total_ms),
            ],
            expanded=str(expanded).lower(),
            profiler_matches=len(all_drives),
            storage_nodes=len(storage_info_map),
            devices=len(devices),
        )
        return devices

    def poke_device(self, device_identifier: Any) -> bool:
        raise RuntimeError("macOS poke is not currently supported.")

    def sort_devices(self, devices: list[UsbDeviceInfo]) -> list[UsbDeviceInfo]:
        def _key(device: UsbDeviceInfo) -> str:
            block_device = getattr(device, "blockDevice", "")
            if isinstance(block_device, str) and block_device.startswith("/dev/disk"):
                return block_device
            return getattr(device, "iSerial", "") or "~~~~~"

        return sorted(devices, key=_key)

    def _timed_populate_device_version(
        self,
        vid: str,
        pid: str,
        serial: str,
        device_path: str,
        size_gb: str,
        profile_scan: bool = False,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        profile: dict[str, Any] = {}
        version_info = populate_device_version(
            int(vid, 16),
            int(pid, 16),
            serial,
            bsd_name=device_path,
            profile=profile,
        )
        profile_ms = (time.perf_counter() - start) * 1000.0
        version_info["_profile_ms"] = profile_ms
        _emit_profile_event(
            profile_scan,
            "macos-version-profile",
            block_device=device_path,
            size_mode=("oob" if str(size_gb).strip() == "N/A (OOB Mode)" else "mounted_media"),
            serial=serial or "unknown",
            duration_ms=f"{profile_ms:.2f}",
            transport=profile.get("transport", "unknown"),
            payload_len=profile.get("payload_len", "unknown"),
            parsed_scb_part_number=profile.get("parsed_scb_part_number", "N/A"),
            parsed_bridge_fw=profile.get("parsed_bridge_fw", "N/A"),
            read_buffer_empty=profile.get("usb_core_read_buffer_empty", ""),
            read_buffer_error=profile.get("usb_core_read_buffer_error", ""),
            read_buffer_stage=profile.get("usb_core_read_buffer_error_stage", ""),
            ata_error=profile.get("usb_core_ata_read_buffer_error", ""),
            ata_stage=profile.get("usb_core_ata_read_buffer_error_stage", ""),
            reclaimed_before_ata=profile.get("usb_core_reclaimed_before_ata", ""),
            usb_core_error=profile.get("usb_core_error", ""),
        )
        return version_info

    def _should_probe_version_info(self, size_gb: str, block_device: str) -> bool:
        if os.getenv("USB_TOOL_FORCE_MACOS_VERSION_PROBE") == "1":
            return True
        if size_gb == "N/A (OOB Mode)":
            return True
        return not bool(block_device)

    def list_usb_drives(self):
        return self._list_usb_drives()

    def parse_uasp_info(self, drives=None):
        transport_map = self._get_transport_map()
        return {key: (value == "UAS") for key, value in transport_map.items()}

    def find_apricorn_device(self):
        return self.scan_devices()

    def _list_usb_drives(self):
        try:
            res = subprocess.run(
                ["system_profiler", "SPUSBDataType", "-json"],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                return []
            data = json.loads(res.stdout)
            matches = []

            def recurse(obj, host_controller: str = ""):
                if isinstance(obj, dict):
                    current_host_controller = obj.get("host_controller", "") or host_controller
                    if "0984" in obj.get("vendor_id", "") or "Apricorn" in obj.get(
                        "manufacturer", ""
                    ):
                        drive = dict(obj)
                        if current_host_controller:
                            drive["host_controller"] = current_host_controller
                        matches.append(drive)
                    for v in obj.values():
                        recurse(v, current_host_controller)
                elif isinstance(obj, list):
                    for i in obj:
                        recurse(i, host_controller)

            recurse(data.get("SPUSBDataType", []))
            return matches
        except Exception:
            return []

    def _parse_uasp_info(self, drives):
        uas = {}
        for d in drives:
            name = d.get("_name")
            if "Media" in d and d["Media"]:
                bsd = d["Media"][0].get("bsd_name")
                if name and bsd:
                    try:
                        res = subprocess.run(
                            ["diskutil", "info", bsd], capture_output=True, text=True
                        )
                        if (
                            res.returncode == 0
                            and "Protocol: USB" in res.stdout
                            and "Transport: UAS" in res.stdout
                        ):
                            uas[name] = True
                    except Exception:
                        pass
        return uas

    def _get_transport_map(self) -> dict[str, str]:
        return {
            key: info["driverTransport"]
            for key, info in self._get_mass_storage_info_map().items()
            if info.get("driverTransport")
        }

    def _get_mass_storage_info_map(self) -> dict[str, dict[str, Any]]:
        try:
            res = subprocess.run(
                ["ioreg", "-r", "-c", "IOUSBMassStorageDriverNub", "-w0", "-l"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return {}

        if res.returncode != 0 or not res.stdout.strip():
            return {}

        storage_info_map: dict[str, dict[str, Any]] = {}
        block_lines: list[str] = []

        def _flush() -> None:
            if not block_lines:
                return

            block = "\n".join(block_lines)
            if "IOClass" not in block or "IOUSBMassStorageDriverNub" not in block:
                return

            device_info = _extract_ioreg_dict_value(block, "USB Device Info")
            interface_class = _extract_ioreg_dict_value(block, "bInterfaceClass") or (
                _extract_ioreg_dict_value(device_info, "bInterfaceClass")
            )
            interface_subclass = _extract_ioreg_dict_value(block, "bInterfaceSubClass") or (
                _extract_ioreg_dict_value(device_info, "bInterfaceSubClass")
            )
            if interface_class != "8" or interface_subclass != "6":
                return

            protocol = _classify_mass_storage_protocol(
                _extract_ioreg_dict_value(block, "bInterfaceProtocol")
                or _extract_ioreg_dict_value(device_info, "bInterfaceProtocol")
            )
            writable = _parse_ioreg_bool(_extract_ioreg_dict_value(block, "Writable"))
            bsd_name = _extract_ioreg_dict_value(block, "BSD Name")

            info: dict[str, Any] = {}
            if protocol != "Unknown":
                info["driverTransport"] = protocol
            if writable is not None:
                info["readOnly"] = not writable
            if bsd_name:
                info["blockDevice"] = _normalize_whole_disk_path(bsd_name)

            keys = (
                _extract_ioreg_dict_value(block, "USB Serial Number"),
                _extract_ioreg_dict_value(block, "kUSBSerialNumberString"),
                _extract_ioreg_dict_value(device_info, "kUSBSerialNumberString"),
                _extract_ioreg_dict_value(block, "USB Product Name"),
                _extract_ioreg_dict_value(block, "kUSBProductString"),
                _extract_ioreg_dict_value(device_info, "USB Product Name"),
                _extract_ioreg_dict_value(device_info, "kUSBProductString"),
            )
            for value in keys:
                if value:
                    storage_info_map[value] = dict(info)

        for line in res.stdout.splitlines():
            if line.startswith("+-o IOUSBMassStorageDriverNub "):
                if block_lines:
                    _flush()
                block_lines = [line]
                continue
            if block_lines:
                block_lines.append(line)

        if block_lines:
            _flush()

        return storage_info_map

    def _get_media_type_from_diskutil(self, block_device: str) -> str:
        try:
            res = subprocess.run(
                ["diskutil", "info", "-plist", block_device],
                capture_output=True,
                check=False,
            )
        except Exception:
            return "Unknown"

        if res.returncode != 0 or not res.stdout:
            return "Unknown"

        try:
            info = plistlib.loads(res.stdout)
        except Exception:
            return "Unknown"

        for key in ("RemovableMedia", "Removable", "EjectableOnly"):
            media_type = _classify_media_type(info.get(key))
            if media_type != "Unknown":
                return media_type

        return "Unknown"
