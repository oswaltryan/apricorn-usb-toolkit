from __future__ import annotations

import ctypes
import errno
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


def _build_read_buffer_cdb() -> bytes:
    # READ BUFFER (6) - 0x3C
    return bytes([0x3C, 0x01, 0x00, 0x00, 0x00, 0x00])


def _build_ata_read_buffer_dma_passthrough_cdb() -> bytes:
    # SCSI ATA PASS-THROUGH(16) carrying ATA READ BUFFER DMA - 0xE9.
    return bytes(
        [0x85, 0x15, 0x0E, 0x0, 0x20, 0x0, 0x01, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0xE0, 0xFE, 0x0]
    )


def _is_illegal_request_invalid_command(sense: bytes) -> bool:
    return len(sense) >= 14 and sense[2] == 0x05 and sense[12] == 0x20 and sense[13] == 0x00


class ScsiReadBufferUnsupportedError(RuntimeError):
    def __init__(self, sense: bytes) -> None:
        super().__init__("SCSI READ BUFFER rejected as invalid command operation code")
        self.sense = sense


# --- Windows Logic ---
if sys.platform == "win32":
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 0x3
    INVALID_HANDLE_VALUE = -1
    IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014

    class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("ScsiStatus", wintypes.BYTE),
            ("PathId", wintypes.BYTE),
            ("TargetId", wintypes.BYTE),
            ("Lun", wintypes.BYTE),
            ("CdbLength", wintypes.BYTE),
            ("SenseInfoLength", wintypes.BYTE),
            ("DataIn", wintypes.BYTE),
            ("DataTransferLength", wintypes.ULONG),
            ("TimeOutValue", wintypes.ULONG),
            ("DataBuffer", ctypes.c_void_p),
            ("SenseInfoOffset", wintypes.ULONG),
            ("Cdb", wintypes.BYTE * 16),
        ]

    class SPTD_WITH_SENSE(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("sptd", SCSI_PASS_THROUGH_DIRECT),
            ("ucSenseBuf", ctypes.c_ubyte * 32),
        ]

    def _windows_read_buffer(
        physical_drive_num: int,
        timeout_sec: int = 5,
        profile: dict[str, Any] | None = None,
    ) -> bytes:
        drive_path = rf"\\.\PhysicalDrive{physical_drive_num}"
        h = INVALID_HANDLE_VALUE
        try:
            open_start = time.perf_counter()
            h = ctypes.windll.kernel32.CreateFileW(
                drive_path,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if profile is not None:
                profile["drive_path"] = drive_path
                profile["create_file_ms"] = (time.perf_counter() - open_start) * 1000.0
            if h == INVALID_HANDLE_VALUE:
                win_error = ctypes.GetLastError()
                if profile is not None:
                    profile["open_error"] = win_error
                if win_error == errno.EACCES:
                    raise PermissionError("Administrator privileges required")
                raise ctypes.WinError(win_error)

            cdb = _build_read_buffer_cdb()
            data_len = 1024
            data_buf = ctypes.create_string_buffer(data_len)
            sptd_sense = SPTD_WITH_SENSE()
            ctypes.memset(ctypes.byref(sptd_sense), 0, ctypes.sizeof(sptd_sense))
            sptd = sptd_sense.sptd
            sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
            sptd.PathId = 0
            sptd.TargetId = 0
            sptd.Lun = 0
            sptd.CdbLength = len(cdb)
            sptd.SenseInfoLength = len(sptd_sense.ucSenseBuf)
            sptd.DataIn = 1  # DATA_IN
            sptd.DataTransferLength = data_len
            sptd.TimeOutValue = int(timeout_sec)
            sptd.DataBuffer = ctypes.cast(ctypes.pointer(data_buf), ctypes.c_void_p)
            sptd.SenseInfoOffset = sptd.Length
            ctypes.memmove(sptd.Cdb, (ctypes.c_ubyte * len(cdb))(*cdb), len(cdb))

            returned_bytes = wintypes.DWORD(0)
            ioctl_start = time.perf_counter()
            ok = ctypes.windll.kernel32.DeviceIoControl(
                h,
                IOCTL_SCSI_PASS_THROUGH_DIRECT,
                ctypes.byref(sptd_sense),
                ctypes.sizeof(sptd_sense),
                ctypes.byref(sptd_sense),
                ctypes.sizeof(sptd_sense),
                ctypes.byref(returned_bytes),
                None,
            )
            if profile is not None:
                profile["device_io_control_ms"] = (time.perf_counter() - ioctl_start) * 1000.0
                profile["returned_bytes"] = int(returned_bytes.value)
                profile["scsi_status"] = int(sptd.ScsiStatus)
                profile["sense_hex"] = bytes(sptd_sense.ucSenseBuf).hex(" ")
            if ok == 0:
                if profile is not None:
                    profile["ioctl_error"] = ctypes.GetLastError()
                raise ctypes.WinError(ctypes.GetLastError())
            sense = bytes(sptd_sense.ucSenseBuf)
            if sptd.ScsiStatus != 0 and _is_illegal_request_invalid_command(sense):
                raise ScsiReadBufferUnsupportedError(sense)
            # We return the data buffer regardless of ScsiStatus to support OOB mode
            return data_buf.raw
        finally:
            if h != INVALID_HANDLE_VALUE:
                ctypes.windll.kernel32.CloseHandle(h)

    def _windows_ata_read_buffer_dma(
        physical_drive_num: int,
        timeout_sec: int = 5,
        profile: dict[str, Any] | None = None,
    ) -> bytes:
        drive_path = rf"\\.\PhysicalDrive{physical_drive_num}"
        h = INVALID_HANDLE_VALUE
        try:
            open_start = time.perf_counter()
            h = ctypes.windll.kernel32.CreateFileW(
                drive_path,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if profile is not None:
                profile["ata_drive_path"] = drive_path
                profile["ata_create_file_ms"] = (time.perf_counter() - open_start) * 1000.0
            if h == INVALID_HANDLE_VALUE:
                win_error = ctypes.GetLastError()
                if profile is not None:
                    profile["ata_open_error"] = win_error
                if win_error == errno.EACCES:
                    raise PermissionError("Administrator privileges required")
                raise ctypes.WinError(win_error)

            cdb = _build_ata_read_buffer_dma_passthrough_cdb()
            data_len = 512
            data_buf = ctypes.create_string_buffer(data_len)
            sptd_sense = SPTD_WITH_SENSE()
            ctypes.memset(ctypes.byref(sptd_sense), 0, ctypes.sizeof(sptd_sense))
            sptd = sptd_sense.sptd
            sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
            sptd.CdbLength = len(cdb)
            sptd.SenseInfoLength = len(sptd_sense.ucSenseBuf)
            sptd.DataIn = 1
            sptd.DataTransferLength = data_len
            sptd.TimeOutValue = int(timeout_sec)
            sptd.DataBuffer = ctypes.cast(ctypes.pointer(data_buf), ctypes.c_void_p)
            sptd.SenseInfoOffset = sptd.Length
            ctypes.memmove(sptd.Cdb, (ctypes.c_ubyte * len(cdb))(*cdb), len(cdb))

            returned_bytes = wintypes.DWORD(0)
            ioctl_start = time.perf_counter()
            ok = ctypes.windll.kernel32.DeviceIoControl(
                h,
                IOCTL_SCSI_PASS_THROUGH_DIRECT,
                ctypes.byref(sptd_sense),
                ctypes.sizeof(sptd_sense),
                ctypes.byref(sptd_sense),
                ctypes.sizeof(sptd_sense),
                ctypes.byref(returned_bytes),
                None,
            )
            if profile is not None:
                profile["ata_device_io_control_ms"] = (time.perf_counter() - ioctl_start) * 1000.0
                profile["ata_returned_bytes"] = int(returned_bytes.value)
                profile["ata_scsi_status"] = int(sptd.ScsiStatus)
                profile["ata_sense_hex"] = bytes(sptd_sense.ucSenseBuf).hex(" ")
            if ok == 0:
                if profile is not None:
                    profile["ata_ioctl_error"] = ctypes.GetLastError()
                raise ctypes.WinError(ctypes.GetLastError())
            return data_buf.raw
        finally:
            if h != INVALID_HANDLE_VALUE:
                ctypes.windll.kernel32.CloseHandle(h)


if sys.platform.startswith("linux"):
    import fcntl

    SG_IO = 0x2285
    SG_DXFER_FROM_DEV = -3

    class SG_IO_HDR(ctypes.Structure):
        _fields_ = [
            ("interface_id", ctypes.c_int),
            ("dxfer_direction", ctypes.c_int),
            ("cmd_len", ctypes.c_ubyte),
            ("mx_sb_len", ctypes.c_ubyte),
            ("iovec_count", ctypes.c_ushort),
            ("dxfer_len", ctypes.c_uint),
            ("dxferp", ctypes.c_void_p),
            ("cmdp", ctypes.c_void_p),
            ("sbp", ctypes.c_void_p),
            ("timeout", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("pack_id", ctypes.c_int),
            ("usr_ptr", ctypes.c_void_p),
            ("status", ctypes.c_ubyte),
            ("masked_status", ctypes.c_ubyte),
            ("msg_status", ctypes.c_ubyte),
            ("sb_len_wr", ctypes.c_ubyte),
            ("host_status", ctypes.c_ushort),
            ("driver_status", ctypes.c_ushort),
            ("resid", ctypes.c_int),
            ("duration", ctypes.c_uint),
            ("info", ctypes.c_uint),
        ]

    def _linux_read_buffer(device_path: str, timeout_sec: int = 5) -> bytes:
        fd = -1
        try:
            fd = os.open(device_path, os.O_RDONLY)

            cdb_bytes = _build_read_buffer_cdb()
            cdb = ctypes.create_string_buffer(cdb_bytes, len(cdb_bytes))
            data_len = 1024
            data_buf = ctypes.create_string_buffer(data_len)
            sense_buf = ctypes.create_string_buffer(32)

            sg_io = SG_IO_HDR()
            ctypes.memset(ctypes.byref(sg_io), 0, ctypes.sizeof(sg_io))
            sg_io.interface_id = ord("S")
            sg_io.dxfer_direction = SG_DXFER_FROM_DEV
            sg_io.cmd_len = len(cdb_bytes)
            sg_io.mx_sb_len = ctypes.sizeof(sense_buf)
            sg_io.dxfer_len = data_len
            sg_io.dxferp = ctypes.cast(data_buf, ctypes.c_void_p)
            sg_io.cmdp = ctypes.cast(cdb, ctypes.c_void_p)
            sg_io.sbp = ctypes.cast(sense_buf, ctypes.c_void_p)
            sg_io.timeout = int(timeout_sec * 1000)

            fcntl.ioctl(fd, SG_IO, sg_io)
            actual_len = max(0, data_len - max(sg_io.resid, 0))
            return data_buf.raw[:actual_len]
        finally:
            if fd >= 0:
                os.close(fd)


@dataclass
class DeviceVersionInfo:
    scb_part_number: str
    mcu_fw: tuple[int | None, int | None, int | None]
    hardware_version: str | None = None
    model_id: str | None = None
    bridge_fw: str | None = None
    raw_data: bytes = b""


def _query_usb_core(
    vendor_id: int, product_id: int, serial_number: str, bsd_name: str | None = None
) -> bytes:
    # Ensure usb modules are available
    try:
        import usb.core
        import usb.util
    except ImportError:
        # If pyusb isn't available, we can't use this method.
        # This is expected on minimized Windows builds.
        return b""

    if sys.platform == "darwin" and bsd_name:
        # On macOS, we must unmount the disk to detach the kernel driver safely
        # and allow pyusb to claim the interface.
        try:
            subprocess.run(["diskutil", "unmountDisk", bsd_name], capture_output=True, check=False)
            # Give the OS a moment to release the device
            time.sleep(1)
        except Exception:
            pass

    dev = usb.core.find(idVendor=vendor_id, idProduct=product_id, serial_number=serial_number)
    if dev is None:
        dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)

    if dev is None:
        raise ValueError("Device not found")

    intf = None
    data = b""
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except Exception:
        pass

    try:
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]

        ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            ),
        )
        ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            ),
        )

        if ep_out is None or ep_in is None:
            raise ValueError("Could not find IN and OUT endpoints")

        tag = 0x12345678
        data_len = 1024
        cbw = bytearray(31)
        cbw[0:4] = b"USBC"
        cbw[4:8] = tag.to_bytes(4, "little")
        cbw[8:12] = data_len.to_bytes(4, "little")
        cbw[12] = 0x80  # IN
        cbw[14] = 6  # CDB length
        # Using the same CDB construction
        cbw[15:21] = bytes([0x3C, 0x01, 0x00, 0x00, 0x00, 0x00])

        try:
            ep_out.write(cbw)
        except usb.core.USBError:
            return b""

        try:
            response_data = ep_in.read(data_len, timeout=5000)
        except usb.core.USBError:
            response_data = b""

        try:
            ep_in.read(13, timeout=5000)
        except usb.core.USBError:
            pass

        data = response_data.tobytes() if hasattr(response_data, "tobytes") else b""
    finally:
        if intf is not None:
            usb.util.release_interface(dev, intf)
        try:
            dev.attach_kernel_driver(0)
        except Exception:
            pass

    # Remount on macOS if we unmounted it
    if sys.platform == "darwin" and bsd_name:
        try:
            subprocess.run(["diskutil", "mountDisk", bsd_name], capture_output=True, check=False)
        except Exception:
            pass

    return data


def query_device_version(
    vendor_id: int,
    product_id: int,
    serial_number: str,
    bsd_name: str | None = None,
    physical_drive_num: int | None = None,
    device_path: str | None = None,
    profile: dict[str, Any] | None = None,
) -> DeviceVersionInfo:
    data = b""
    timings = profile if profile is not None else {}

    # Try Windows SPTI first if index is provided
    if sys.platform == "win32" and physical_drive_num is not None:
        try:
            timings["transport"] = "windows_spti"
            data = _windows_read_buffer(physical_drive_num, profile=timings)
        except ScsiReadBufferUnsupportedError as exc:
            timings["scsi_read_buffer_rejected"] = "illegal_request_invalid_command"
            timings["scsi_read_buffer_sense_hex"] = exc.sense.hex(" ")
            try:
                timings["transport"] = "windows_spti_ata_read_buffer_dma"
                data = _windows_ata_read_buffer_dma(physical_drive_num, profile=timings)
            except Exception:
                data = b""
        except Exception:
            # Fallback or just empty
            data = b""
    elif sys.platform.startswith("linux") and device_path:
        try:
            data = _linux_read_buffer(device_path)
        except Exception:
            data = b""
    else:
        # Fallback to libusb (macOS/Linux)
        try:
            data = _query_usb_core(vendor_id, product_id, serial_number, bsd_name)
        except Exception:
            data = b""

    parse_start = time.perf_counter()
    info = _parse_payload_best_effort(data)
    timings["parse_payload_ms"] = (time.perf_counter() - parse_start) * 1000.0
    timings["payload_len"] = len(data)
    timings["parsed_scb_part_number"] = info.scb_part_number
    timings["parsed_bridge_fw"] = info.bridge_fw or "N/A"
    info.raw_data = data
    return info


def _parse_payload_best_effort(data: bytes) -> DeviceVersionInfo:
    """Parse the payload to match expected fields for Apricorn devices."""
    bridge_fw: str | None = None
    scb_part: str = ""
    mcu_fw: tuple[int | None, int | None, int | None] = (None, None, None)
    hw_ver: str | None = None
    model_id: str | None = None

    if data and len(data) >= 4:
        bridge_fw = f"{data[2]:02X}{data[3]:02X}"

    match = re.search(rb"(\d{2})-(\d{11})", data)

    if match:
        try:
            prefix_str = match.group(1).decode("ascii")
            body_str = match.group(2).decode("ascii")
            scb_part = f"{prefix_str}-{body_str[:4]}"
            if len(body_str) >= 11:
                model_id = f"{body_str[5]}{body_str[4]}"
                hw_ver = f"{body_str[7]}{body_str[6]}"
                mj = int(body_str[10])
                mn = int(body_str[9])
                sb = int(body_str[8])
                mcu_fw = (mj, mn, sb)
        except (ValueError, IndexError):
            pass

    return DeviceVersionInfo(
        scb_part_number=scb_part if scb_part else "N/A",
        mcu_fw=mcu_fw,
        hardware_version=hw_ver,
        model_id=model_id,
        bridge_fw=bridge_fw,
    )


__all__ = ["DeviceVersionInfo", "query_device_version"]
