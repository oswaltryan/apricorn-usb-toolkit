# src/usb_tool/backend/linux.py

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from ..constants import EXCLUDED_PIDS
from ..device_config import closest_values
from ..models import UsbDeviceInfo

# For Phase 3/4, still import from legacy if not moved
from ..services import populate_device_version, prune_hidden_version_fields
from ..utils import bytes_to_gb, find_closest, is_oob_mode_size_gb
from .base import AbstractBackend


def _normalize_pid(pid: str) -> str:
    if not isinstance(pid, str):
        return ""
    cleaned = pid.lower().replace("0x", "")
    return cleaned.split("&", 1)[0][:4]


def _is_excluded_pid(pid: str) -> bool:
    return _normalize_pid(pid) in EXCLUDED_PIDS


def _normalize_linux_serial(value: Any) -> str:
    serial = str(value or "").strip()
    if serial in {"", "-", "N/A"}:
        return ""
    return serial


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


@dataclass
class _LinuxBlockDeviceProbe:
    # Field mapping for the Linux hot path:
    # - serial: lsblk SERIAL, then sysfs USB serial, then udev properties
    # - driver_name / driver_transport: sysfs USB interface driver, then udev
    # - pci_addr: sysfs topology path, then udev ID_PATH/DEVPATH
    block_device: str
    serial: str = ""
    driver_name: str = ""
    driver_transport: str = "Unknown"
    pci_addr: str = ""
    controller_name: str = "N/A"
    udev_info: dict[str, str] = field(default_factory=dict)


class LinuxBackend(AbstractBackend):
    def scan_devices(
        self,
        expanded: bool = False,
        profile_scan: bool = False,
    ) -> list[UsbDeviceInfo]:
        self._profile_scan_enabled = profile_scan
        self._profile_helper_events_enabled = False
        scan_start = time.perf_counter()

        lsblk_start = time.perf_counter()
        lsblk_drives = self._list_usb_drives()
        lsblk_ms = (time.perf_counter() - lsblk_start) * 1000.0

        probe_start = time.perf_counter()
        probe_map = self._probe_block_devices(lsblk_drives)
        probe_ms = (time.perf_counter() - probe_start) * 1000.0

        controller_lookup_start = time.perf_counter()
        controller_map = self._resolve_probe_controllers(probe_map)
        controller_lookup_ms = (time.perf_counter() - controller_lookup_start) * 1000.0
        for block_device, controller_name in controller_map.items():
            probe_map[block_device].controller_name = controller_name

        lsusb_start = time.perf_counter()
        lsusb_details = self._get_lsusb_details()
        descriptor_lookup_ms = (time.perf_counter() - lsusb_start) * 1000.0

        devices = []
        version_query_ms = 0.0
        device_build_start = time.perf_counter()
        for lsblk_info in lsblk_drives:
            block_path = lsblk_info.get("name", "")
            if not block_path:
                continue

            probe = probe_map.get(block_path) or _LinuxBlockDeviceProbe(block_device=block_path)
            serial = probe.serial or _normalize_linux_serial(lsblk_info.get("serial"))

            if not serial:
                continue

            lsusb_info = lsusb_details.get(serial)
            if not lsusb_info:
                lsusb_info = self._get_sysfs_usb_details(block_path)
            if not lsusb_info:
                continue

            vid = lsusb_info.get("idVendor", "").lower()
            pid = _normalize_pid(lsusb_info.get("idProduct", ""))
            if vid != "0984" or pid in EXCLUDED_PIDS:
                continue

            bcd_usb = 0.0
            try:
                bcd_usb = float(lsusb_info.get("bcdUSB", "0"))
            except (ValueError, TypeError):
                pass

            bcd_dev = (
                lsusb_info.get("bcdDevice", "0000")
                .lower()
                .replace("0x", "")
                .replace(".", "")
                .zfill(4)
            )

            size_raw = lsblk_info.get("size_gb", 0.0)
            size_gb = "N/A (OOB Mode)"
            if size_raw > 0 and not is_oob_mode_size_gb(size_raw):
                opts = (
                    closest_values.get(pid, (None, []))[1]
                    or closest_values.get(bcd_dev, (None, []))[1]
                )
                if opts:
                    closest = find_closest(size_raw, opts)
                    size_gb = str(closest) if closest else str(round(size_raw))
                else:
                    size_gb = str(round(size_raw))

            version_info = self._timed_populate_device_version(
                vid,
                pid,
                serial,
                block_path,
                size_gb,
            )
            version_query_ms += version_info.pop("_profile_ms", 0.0)

            dev_info = UsbDeviceInfo(
                bcdUSB=bcd_usb,
                idVendor=vid,
                idProduct=pid,
                bcdDevice=bcd_dev,
                iManufacturer=lsusb_info.get("iManufacturer", "Apricorn"),
                iProduct=lsusb_info.get("iProduct", "Unknown"),
                iSerial=serial,
                driverTransport=probe.driver_transport or "Unknown",
                driveSizeGB=size_gb,
                mediaType=lsblk_info.get("mediaType", "Unknown"),
                **version_info,
            )
            dev_info.blockDevice = block_path
            dev_info.usbController = probe.controller_name or "N/A"
            dev_info.readOnly = bool(lsblk_info.get("readOnly", False))

            prune_hidden_version_fields(dev_info)
            devices.append(dev_info)

        device_build_ms = (time.perf_counter() - device_build_start) * 1000.0
        _emit_profile_event(
            profile_scan,
            "linux-scan-profile details",
            populate_device_version_total=f"{version_query_ms:.2f}ms",
            device_count=len(devices),
        )
        total_ms = (time.perf_counter() - scan_start) * 1000.0
        _emit_profile_summary(
            profile_scan,
            "linux-scan-profile",
            [
                ("lsblk", lsblk_ms),
                ("device_probe", probe_ms),
                ("controller_lookup", controller_lookup_ms),
                ("descriptor_lookup", descriptor_lookup_ms),
                ("device_build", device_build_ms),
                ("total", total_ms),
            ],
            expanded=str(expanded).lower(),
            lsblk_drives=len(lsblk_drives),
            probed_devices=len(probe_map),
            unique_pci_addrs=len(
                {probe.pci_addr for probe in probe_map.values() if probe.pci_addr}
            ),
            lsusb_devices=len(lsusb_details),
            devices=len(devices),
        )
        return devices

    def poke_device(self, device_identifier: Any) -> bool:
        # Ported logic from poke_device.py

        try:
            fd = os.open(device_identifier, os.O_RDWR)
            # This is a stub for the complex IOCTL logic
            os.close(fd)
            return True  # Assume success for now if we can open it
        except OSError:
            return False

    def sort_devices(self, devices: list[UsbDeviceInfo]) -> list[UsbDeviceInfo]:
        def _key(dev):
            path = getattr(dev, "blockDevice", "")
            return path if path.startswith("/dev/") else "~~~~~"

        return sorted(devices, key=_key)

    def _timed_populate_device_version(
        self,
        vid: str,
        pid: str,
        serial: str,
        block_path: str,
        size_gb: str,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        profile: dict[str, Any] = {}
        version_info = populate_device_version(
            int(vid, 16),
            int(pid, 16),
            serial,
            device_path=block_path,
            profile=profile,
        )
        profile_ms = (time.perf_counter() - start) * 1000.0
        version_info["_profile_ms"] = profile_ms
        _emit_profile_event(
            getattr(self, "_profile_scan_enabled", False),
            "linux-version-profile",
            block_device=block_path,
            size_mode=("oob" if str(size_gb).strip() == "N/A (OOB Mode)" else "mounted_media"),
            serial=serial or "unknown",
            duration_ms=f"{profile_ms:.2f}",
            transport=profile.get("transport", "unknown"),
            payload_len=profile.get("payload_len", "unknown"),
            parsed_scb_part_number=profile.get("parsed_scb_part_number", "N/A"),
            parsed_bridge_fw=profile.get("parsed_bridge_fw", "N/A"),
            status=profile.get("linux_status", "unknown"),
            resid=profile.get("linux_resid", "unknown"),
            sense=profile.get("linux_sense_hex", "unknown"),
            ata_status=profile.get("linux_ata_status", "unknown"),
            ata_resid=profile.get("linux_ata_resid", "unknown"),
            ata_sense=profile.get("linux_ata_sense_hex", "unknown"),
            error=profile.get("linux_error", ""),
        )
        return version_info

    # --- Internal Helpers ---
    def list_usb_drives(self):
        return self._list_usb_drives()

    def _probe_block_devices(
        self, lsblk_drives: list[dict[str, Any]]
    ) -> dict[str, _LinuxBlockDeviceProbe]:
        candidates = [drive for drive in lsblk_drives if drive.get("name")]
        if not candidates:
            return {}

        max_workers = min(len(candidates), max(os.cpu_count() or 1, 1), 8)
        if max_workers <= 1:
            return {
                drive["name"]: self._probe_block_device_context(drive["name"], drive)
                for drive in candidates
            }

        results: dict[str, _LinuxBlockDeviceProbe] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._probe_block_device_context, drive["name"], drive): drive
                for drive in candidates
            }
            for future in as_completed(futures):
                drive = futures[future]
                block_device = drive["name"]
                try:
                    results[block_device] = future.result()
                except Exception:
                    results[block_device] = _LinuxBlockDeviceProbe(
                        block_device=block_device,
                        serial=_normalize_linux_serial(drive.get("serial")),
                    )
        return results

    def _probe_block_device_context(
        self,
        block_device: str,
        lsblk_info: dict[str, Any],
    ) -> _LinuxBlockDeviceProbe:
        probe = _LinuxBlockDeviceProbe(
            block_device=block_device,
            serial=_normalize_linux_serial(lsblk_info.get("serial")),
        )
        sysfs_path = self._get_block_device_sysfs_path(block_device)

        if sysfs_path:
            probe.driver_name = self._find_usb_driver_name_in_sysfs(sysfs_path)
            if not probe.serial:
                probe.serial = self._find_usb_serial_in_sysfs(sysfs_path)
            probe.pci_addr = self._extract_pci_address_from_text(sysfs_path)

        if not probe.serial or not probe.driver_name or not probe.pci_addr:
            probe.udev_info = self._get_udev_info(block_device)
            if not probe.serial:
                probe.serial = self._extract_serial_from_udev_info(probe.udev_info)
            if not probe.driver_name:
                probe.driver_name = probe.udev_info.get("ID_USB_DRIVER", "").strip()
            if not probe.pci_addr:
                probe.pci_addr = self._extract_pci_controller_address(probe.udev_info)

        probe.driver_name = probe.driver_name.strip().lower()
        probe.driver_transport = self._classify_driver_transport_name(probe.driver_name)
        return probe

    def _resolve_probe_controllers(
        self, probe_map: dict[str, _LinuxBlockDeviceProbe]
    ) -> dict[str, str]:
        pci_addresses = sorted({probe.pci_addr for probe in probe_map.values() if probe.pci_addr})
        if not pci_addresses:
            return {block_device: "N/A" for block_device in probe_map}

        max_workers = min(len(pci_addresses), 4)
        cache: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._get_pci_controller_name, pci_addr): pci_addr
                for pci_addr in pci_addresses
            }
            for future in as_completed(futures):
                pci_addr = futures[future]
                try:
                    cache[pci_addr] = future.result()
                except Exception:
                    cache[pci_addr] = "N/A"

        return {
            block_device: cache.get(probe.pci_addr, "N/A") if probe.pci_addr else "N/A"
            for block_device, probe in probe_map.items()
        }

    def _get_block_device_sysfs_path(self, block_device: str) -> str:
        dev_name = os.path.basename(block_device)
        if not dev_name:
            return ""

        sysfs_path = os.path.join("/sys/class/block", dev_name)
        if not os.path.exists(sysfs_path):
            return ""
        return os.path.realpath(sysfs_path)

    def _iter_sysfs_ancestors(self, start_path: str):
        current = os.path.realpath(start_path)
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            yield current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    def _read_sysfs_link_name(self, path: str) -> str:
        if not os.path.lexists(path):
            return ""
        return os.path.basename(os.path.realpath(path))

    def _read_sysfs_text(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def _find_usb_driver_name_in_sysfs(self, sysfs_path: str) -> str:
        for candidate in self._iter_sysfs_ancestors(sysfs_path):
            if self._read_sysfs_link_name(os.path.join(candidate, "subsystem")) != "usb":
                continue
            driver_name = self._read_sysfs_link_name(os.path.join(candidate, "driver"))
            if driver_name and driver_name != "usb":
                return driver_name.strip().lower()
        return ""

    def _find_usb_serial_in_sysfs(self, sysfs_path: str) -> str:
        for candidate in self._iter_sysfs_ancestors(sysfs_path):
            if self._read_sysfs_link_name(os.path.join(candidate, "subsystem")) != "usb":
                continue
            serial = _normalize_linux_serial(
                self._read_sysfs_text(os.path.join(candidate, "serial"))
            )
            if serial:
                return serial
        return ""

    def _get_sysfs_usb_details(self, block_device: str) -> dict[str, str]:
        sysfs_path = self._get_block_device_sysfs_path(block_device)
        if not sysfs_path:
            return {}

        for candidate in self._iter_sysfs_ancestors(sysfs_path):
            if self._read_sysfs_link_name(os.path.join(candidate, "subsystem")) != "usb":
                continue

            vid = self._read_sysfs_text(os.path.join(candidate, "idVendor")).lower()
            pid = _normalize_pid(self._read_sysfs_text(os.path.join(candidate, "idProduct")))
            serial = _normalize_linux_serial(
                self._read_sysfs_text(os.path.join(candidate, "serial"))
            )
            if not vid or not pid or not serial:
                continue

            return {
                "idVendor": vid,
                "idProduct": pid,
                "bcdUSB": self._read_sysfs_text(os.path.join(candidate, "version")) or "0",
                "bcdDevice": self._read_sysfs_text(os.path.join(candidate, "bcdDevice"))
                or "0000",
                "iManufacturer": self._read_sysfs_text(os.path.join(candidate, "manufacturer"))
                or "Apricorn",
                "iProduct": self._read_sysfs_text(os.path.join(candidate, "product")) or "Unknown",
                "iSerial": serial,
            }

        return {}

    def _list_usb_drives(self):
        cmd = [
            "lsblk",
            "-p",
            "-o",
            "NAME,SERIAL,SIZE,RM,RO",
            "-d",
            "-n",
            "-l",
            "-e",
            "7",
        ]
        try:
            exec_start = time.perf_counter()
            res = subprocess.run(cmd, capture_output=True, text=True)
            exec_ms = (time.perf_counter() - exec_start) * 1000.0
            if res.returncode != 0:
                _emit_profile_event(
                    getattr(self, "_profile_helper_events_enabled", False),
                    "linux-lsblk-profile",
                    exec_ms=f"{exec_ms:.2f}",
                    parse_ms="0.00",
                    returncode=res.returncode,
                    drives=0,
                )
                return []
            parse_start = time.perf_counter()
            drives = []
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                drives.append(
                    {
                        "name": parts[0],
                        "serial": _normalize_linux_serial(parts[1]),
                        "size_gb": self.parse_lsblk_size(parts[2]),
                        "mediaType": ("Removable Media" if parts[3] == "1" else "Basic Disk"),
                        "readOnly": parts[4] == "1",
                    }
                )
            parse_ms = (time.perf_counter() - parse_start) * 1000.0
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lsblk-profile",
                exec_ms=f"{exec_ms:.2f}",
                parse_ms=f"{parse_ms:.2f}",
                returncode=res.returncode,
                drives=len(drives),
            )
            return drives
        except Exception:
            return []

    def parse_lsblk_size(self, size_str: str) -> float:
        if not size_str:
            return 0.0
        m = re.match(r"([\d\.,]+)\s*([GMTEK])?", size_str.upper())
        if not m:
            return 0.0
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2)
        if unit == "G":
            return val
        if unit == "M":
            return val / 1024
        if unit == "T":
            return val * 1024
        if unit == "K":
            return val / (1024**2)
        if unit == "E":
            return val * (1024**2)
        return bytes_to_gb(val)

    def _parse_uasp_info(self):
        try:
            exec_start = time.perf_counter()
            res = subprocess.run(
                ["lshw", "-class", "disk", "-class", "storage", "-json"],
                capture_output=True,
                text=True,
                check=False,
            )
            exec_ms = (time.perf_counter() - exec_start) * 1000.0
        except Exception:
            return {}

        if res.returncode != 0 or not res.stdout.strip():
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lshw-profile",
                exec_ms=f"{exec_ms:.2f}",
                json_loads_ms="0.00",
                walk_ms="0.00",
                returncode=res.returncode,
                stdout_bytes=len(res.stdout or ""),
                block_devices=0,
            )
            return {}

        try:
            json_start = time.perf_counter()
            raw_data = json.loads(res.stdout)
            json_ms = (time.perf_counter() - json_start) * 1000.0
        except json.JSONDecodeError:
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lshw-profile",
                exec_ms=f"{exec_ms:.2f}",
                json_loads_ms="0.00",
                walk_ms="0.00",
                returncode=res.returncode,
                stdout_bytes=len(res.stdout),
                block_devices=0,
                json_error="decode_failed",
            )
            return {}

        entries = raw_data if isinstance(raw_data, list) else [raw_data]
        by_block_device: dict[str, dict[str, str]] = {}
        walk_start = time.perf_counter()

        def _walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    _walk(item)
                return

            if not isinstance(node, dict):
                return

            logical_name = node.get("logicalname")
            if isinstance(logical_name, str) and logical_name.startswith("/dev/"):
                by_block_device[logical_name] = {
                    "driver": str(node.get("driver", "")).strip().lower(),
                    "serial": str(node.get("serial", "")).strip(),
                }

            for child in node.get("children", []) or []:
                _walk(child)

        _walk(entries)
        walk_ms = (time.perf_counter() - walk_start) * 1000.0
        _emit_profile_event(
            getattr(self, "_profile_helper_events_enabled", False),
            "linux-lshw-profile",
            exec_ms=f"{exec_ms:.2f}",
            json_loads_ms=f"{json_ms:.2f}",
            walk_ms=f"{walk_ms:.2f}",
            returncode=res.returncode,
            stdout_bytes=len(res.stdout),
            block_devices=len(by_block_device),
        )
        return by_block_device

    def _classify_driver_transport_name(self, driver_name: str) -> str:
        driver_name = str(driver_name or "").strip().lower()
        if driver_name == "uas":
            return "UAS"
        if driver_name == "usb-storage":
            return "BOT"
        if driver_name:
            return "Vendor"
        return "Unknown"

    def _classify_driver_transport(self, lshw_entry: dict[str, Any] | None) -> str:
        return self._classify_driver_transport_name((lshw_entry or {}).get("driver", ""))

    def _get_transport_map(self, udev_map: dict[str, dict[str, str]]) -> dict[str, str]:
        transport_map: dict[str, str] = {}
        for block_device, udev_info in udev_map.items():
            driver_name = udev_info.get("ID_USB_DRIVER", "").strip().lower()
            transport_map[block_device] = self._classify_driver_transport({"driver": driver_name})
        return transport_map

    def _get_transport_map_by_serial(self) -> dict[str, str]:
        try:
            exec_start = time.perf_counter()
            res = subprocess.run(
                ["usb-devices"],
                capture_output=True,
                text=True,
                check=False,
            )
            exec_ms = (time.perf_counter() - exec_start) * 1000.0
        except Exception:
            return {}

        if res.returncode != 0 or not res.stdout.strip():
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-usb-devices-profile",
                exec_ms=f"{exec_ms:.2f}",
                parse_ms="0.00",
                returncode=res.returncode,
                blocks=0,
                transport_serials=0,
            )
            return {}

        transport_map: dict[str, str] = {}
        parse_start = time.perf_counter()
        blocks = res.stdout.strip().split("\n\n")
        for block in blocks:
            serial = ""
            driver_name = ""
            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith("S:  SerialNumber="):
                    serial = stripped.partition("=")[2].strip()
                elif stripped.startswith("I:") and "Driver=" in stripped:
                    match = re.search(r"Driver=([^\s]+)", stripped)
                    if match:
                        driver_name = match.group(1).strip().lower()
            if serial and driver_name:
                transport_map[serial] = self._classify_driver_transport({"driver": driver_name})
        parse_ms = (time.perf_counter() - parse_start) * 1000.0
        _emit_profile_event(
            getattr(self, "_profile_helper_events_enabled", False),
            "linux-usb-devices-profile",
            exec_ms=f"{exec_ms:.2f}",
            parse_ms=f"{parse_ms:.2f}",
            returncode=res.returncode,
            blocks=len(blocks),
            transport_serials=len(transport_map),
        )
        return transport_map

    def _get_udev_info_map(self, block_devices) -> dict[str, dict[str, str]]:
        return {block_device: self._get_udev_info(block_device) for block_device in block_devices}

    def _parse_udev_properties(self, output: str) -> dict[str, str]:
        info: dict[str, str] = {}
        for line in output.splitlines():
            if not line.startswith("E: "):
                continue
            key, _, value = line[3:].partition("=")
            if key:
                info[key.strip()] = value.strip()
        return info

    def _extract_serial_from_udev_info(self, udev_info: dict[str, str]) -> str:
        for key in (
            "ID_SERIAL_SHORT",
            "ID_SCSI_SERIAL",
            "SCSI_IDENT_SERIAL",
            "ID_SERIAL_SHORT_ENC",
        ):
            serial = _normalize_linux_serial(udev_info.get(key, ""))
            if serial:
                return serial
        return ""

    def _get_udev_info(self, block_device: str) -> dict[str, str]:
        try:
            exec_start = time.perf_counter()
            res = subprocess.run(
                ["udevadm", "info", "--query=all", f"--name={block_device}"],
                capture_output=True,
                text=True,
                check=False,
            )
            exec_ms = (time.perf_counter() - exec_start) * 1000.0
        except Exception:
            return {}

        if res.returncode != 0:
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-udev-profile",
                block_device=block_device,
                exec_ms=f"{exec_ms:.2f}",
                parse_ms="0.00",
                returncode=res.returncode,
                keys=0,
            )
            return {}

        parse_start = time.perf_counter()
        info = self._parse_udev_properties(res.stdout)
        parse_ms = (time.perf_counter() - parse_start) * 1000.0
        _emit_profile_event(
            getattr(self, "_profile_helper_events_enabled", False),
            "linux-udev-profile",
            block_device=block_device,
            exec_ms=f"{exec_ms:.2f}",
            parse_ms=f"{parse_ms:.2f}",
            returncode=res.returncode,
            keys=len(info),
        )
        return info

    def _get_controller_map(self, udev_map: dict[str, dict[str, str]]) -> dict[str, str]:
        cache: dict[str, str] = {}
        controller_map: dict[str, str] = {}
        for block_device, udev_info in udev_map.items():
            pci_addr = self._extract_pci_controller_address(udev_info)
            if not pci_addr:
                controller_map[block_device] = "N/A"
                continue
            if pci_addr not in cache:
                cache[pci_addr] = self._get_pci_controller_name(pci_addr)
            controller_map[block_device] = cache[pci_addr]
        return controller_map

    def _extract_pci_address_from_text(self, value: str) -> str:
        match = re.search(r"(0000:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])", value or "")
        return match.group(1) if match else ""

    def _extract_pci_controller_address(self, udev_info: dict[str, str]) -> str:
        for key in ("ID_PATH", "DEVPATH"):
            pci_addr = self._extract_pci_address_from_text(udev_info.get(key, ""))
            if pci_addr:
                return pci_addr
        return ""

    def _get_pci_controller_name(self, pci_addr: str) -> str:
        try:
            exec_start = time.perf_counter()
            res = subprocess.run(
                ["lspci", "-s", pci_addr],
                capture_output=True,
                text=True,
                check=False,
            )
            exec_ms = (time.perf_counter() - exec_start) * 1000.0
        except Exception:
            return "N/A"

        if res.returncode != 0:
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lspci-profile",
                pci_addr=pci_addr,
                exec_ms=f"{exec_ms:.2f}",
                parse_ms="0.00",
                returncode=res.returncode,
                controller="N/A",
            )
            return "N/A"

        parse_start = time.perf_counter()
        line = res.stdout.strip().splitlines()
        if not line:
            parse_ms = (time.perf_counter() - parse_start) * 1000.0
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lspci-profile",
                pci_addr=pci_addr,
                exec_ms=f"{exec_ms:.2f}",
                parse_ms=f"{parse_ms:.2f}",
                returncode=res.returncode,
                controller="N/A",
            )
            return "N/A"
        parts = line[0].split(": ", 1)
        description = parts[1].strip() if len(parts) == 2 else line[0].strip()
        manufacturer = description.split(None, 1)[0].strip()
        parse_ms = (time.perf_counter() - parse_start) * 1000.0
        controller = manufacturer or "N/A"
        _emit_profile_event(
            getattr(self, "_profile_helper_events_enabled", False),
            "linux-lspci-profile",
            pci_addr=pci_addr,
            exec_ms=f"{exec_ms:.2f}",
            parse_ms=f"{parse_ms:.2f}",
            returncode=res.returncode,
            controller=controller,
        )
        return controller

    def _get_lsusb_details(self):
        try:
            list_exec_start = time.perf_counter()
            res = subprocess.run(["lsusb"], capture_output=True, text=True, check=False)
            list_exec_ms = (time.perf_counter() - list_exec_start) * 1000.0
        except Exception:
            return {}

        if res.returncode != 0:
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lsusb-profile",
                list_exec_ms=f"{list_exec_ms:.2f}",
                list_parse_ms="0.00",
                verbose_exec_total_ms="0.00",
                verbose_parse_total_ms="0.00",
                verbose_calls=0,
                apricorn_pids=0,
                serials=0,
            )
            return {}

        list_parse_start = time.perf_counter()
        apricorn_pairs = {
            match.group(1).lower()
            for match in re.finditer(r"ID\s+0984:([0-9a-fA-F]{4})", res.stdout)
        }
        list_parse_ms = (time.perf_counter() - list_parse_start) * 1000.0
        details: dict[str, dict[str, str]] = {}
        verbose_exec_total_ms = 0.0
        verbose_parse_total_ms = 0.0

        for pid in apricorn_pairs:
            try:
                verbose_exec_start = time.perf_counter()
                verbose = subprocess.run(
                    ["lsusb", "-v", "-d", f"0984:{pid}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                verbose_exec_ms = (time.perf_counter() - verbose_exec_start) * 1000.0
                verbose_exec_total_ms += verbose_exec_ms
            except Exception:
                continue

            if verbose.returncode != 0:
                _emit_profile_event(
                    getattr(self, "_profile_helper_events_enabled", False),
                    "linux-lsusb-verbose-profile",
                    pid=pid,
                    exec_ms=f"{verbose_exec_ms:.2f}",
                    parse_ms="0.00",
                    returncode=verbose.returncode,
                    serials=0,
                )
                continue

            current: dict[str, str] = {}
            serial_count_before = len(details)
            verbose_parse_start = time.perf_counter()
            for line in verbose.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("idVendor"):
                    current["idVendor"] = "0984"
                elif stripped.startswith("idProduct"):
                    match = re.search(r"idProduct\s+0x([0-9a-fA-F]{4})", stripped)
                    if match:
                        current["idProduct"] = match.group(1).lower()
                elif stripped.startswith("bcdUSB"):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        current["bcdUSB"] = parts[1]
                elif stripped.startswith("bcdDevice"):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        current["bcdDevice"] = parts[1]
                elif stripped.startswith("iManufacturer"):
                    current["iManufacturer"] = stripped.split(None, 2)[-1]
                elif stripped.startswith("iProduct"):
                    current["iProduct"] = stripped.split(None, 2)[-1]
                elif stripped.startswith("iSerial"):
                    serial = stripped.split(None, 2)[-1]
                    if serial and serial != "0":
                        current["iSerial"] = serial
                        details[serial] = current.copy()
                        current = {}
            verbose_parse_ms = (time.perf_counter() - verbose_parse_start) * 1000.0
            verbose_parse_total_ms += verbose_parse_ms
            _emit_profile_event(
                getattr(self, "_profile_helper_events_enabled", False),
                "linux-lsusb-verbose-profile",
                pid=pid,
                exec_ms=f"{verbose_exec_ms:.2f}",
                parse_ms=f"{verbose_parse_ms:.2f}",
                returncode=verbose.returncode,
                serials=len(details) - serial_count_before,
            )

        _emit_profile_event(
            getattr(self, "_profile_helper_events_enabled", False),
            "linux-lsusb-profile",
            list_exec_ms=f"{list_exec_ms:.2f}",
            list_parse_ms=f"{list_parse_ms:.2f}",
            verbose_exec_total_ms=f"{verbose_exec_total_ms:.2f}",
            verbose_parse_total_ms=f"{verbose_parse_total_ms:.2f}",
            verbose_calls=len(apricorn_pairs),
            apricorn_pids=len(apricorn_pairs),
            serials=len(details),
        )
        return details
