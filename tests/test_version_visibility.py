import sys
import types
from unittest.mock import patch

import pytest

from usb_tool import device_version
from usb_tool.backend.linux import LinuxBackend, _LinuxBlockDeviceProbe
from usb_tool.models import UsbDeviceInfo
from usb_tool.services import (
    VERSION_FIELD_NAMES,
    _should_probe_device_version,
    populate_device_version,
    prune_hidden_version_fields,
    should_display_version_fields,
)


def _make_device(**overrides):
    data = {
        "bcdUSB": 3.2,
        "idVendor": "0984",
        "idProduct": "1407",
        "bcdDevice": "0502",
        "iManufacturer": "Apricorn",
        "iProduct": "Secure Key 3.0",
        "iSerial": "SER123",
        "driveSizeGB": "16",
        "mediaType": "Basic Disk",
        "scbPartNumber": "12-3456",
        "hardwareVersion": "01",
        "modelID": "AA",
        "mcuFW": "1.2.3",
        "bridgeFW": "0502",
    }
    data.update(overrides)
    return UsbDeviceInfo(**data)


def test_should_display_version_fields_requires_non_na_scb_part():
    assert should_display_version_fields(_make_device(scbPartNumber="N/A")) is False


def test_should_display_version_fields_hides_on_bridge_mismatch():
    assert should_display_version_fields(_make_device(bridgeFW="0503")) is False


def test_should_display_version_fields_allows_matching_bridge_and_bcd():
    assert should_display_version_fields(_make_device(bridgeFW="0x502")) is True


def test_should_probe_device_version_on_windows_and_linux(monkeypatch):
    monkeypatch.setattr("usb_tool.services.platform.system", lambda: "Linux")
    assert _should_probe_device_version() is True

    monkeypatch.setattr("usb_tool.services.platform.system", lambda: "Darwin")
    assert _should_probe_device_version() is True

    monkeypatch.setattr("usb_tool.services.platform.system", lambda: "Windows")
    assert _should_probe_device_version() is True


def test_prune_hidden_version_fields_removes_all_version_keys_when_hidden():
    device = _make_device(bridgeFW="0503")
    prune_hidden_version_fields(device)
    serialized = device.to_dict()
    for name in VERSION_FIELD_NAMES:
        assert name not in serialized


def test_prune_hidden_version_fields_keeps_version_keys_when_visible():
    device = _make_device(bridgeFW="0502")
    prune_hidden_version_fields(device)
    serialized = device.to_dict()
    for name in VERSION_FIELD_NAMES:
        assert name in serialized


def test_should_display_version_fields_keeps_oob_devices_visible():
    assert should_display_version_fields(
        _make_device(
            driveSizeGB="N/A (OOB Mode)",
            scbPartNumber="N/A",
            hardwareVersion="N/A",
            modelID="N/A",
            mcuFW="N/A",
            bridgeFW="N/A",
        )
    )


def test_should_display_version_fields_treats_plain_na_size_as_oob():
    assert should_display_version_fields(
        _make_device(
            driveSizeGB="N/A",
            bridgeFW="040F",
            bcdDevice="0902",
        )
    )


def test_prune_hidden_version_fields_keeps_oob_version_placeholders():
    device = _make_device(
        driveSizeGB="N/A (OOB Mode)",
        scbPartNumber="N/A",
        hardwareVersion="N/A",
        modelID="N/A",
        mcuFW="N/A",
        bridgeFW="N/A",
    )
    prune_hidden_version_fields(device)
    serialized = device.to_dict()
    for name in VERSION_FIELD_NAMES:
        assert name in serialized


def test_populate_device_version_queries_linux(monkeypatch):
    monkeypatch.setattr("usb_tool.services.platform.system", lambda: "Linux")

    captured = {}

    def _fake_query(*_args, **kwargs):
        captured["device_path"] = kwargs.get("device_path")
        return device_version.DeviceVersionInfo(
            scb_part_number="21-0010",
            hardware_version="00",
            model_id="00",
            mcu_fw=(1, 0, 0),
            bridge_fw="0463",
        )

    monkeypatch.setattr("usb_tool.services.query_device_version", _fake_query)

    info = populate_device_version(
        0x0984,
        0x1400,
        "SER123",
        device_path="/dev/sda",
    )

    assert info == {
        "scbPartNumber": "21-0010",
        "hardwareVersion": "00",
        "modelID": "00",
        "mcuFW": "1.0.0",
        "bridgeFW": "0463",
    }
    assert captured["device_path"] == "/dev/sda"


def test_populate_device_version_queries_macos(monkeypatch):
    monkeypatch.setattr("usb_tool.services.platform.system", lambda: "Darwin")

    captured = {}

    def _fake_query(*_args, **kwargs):
        captured["bsd_name"] = kwargs.get("bsd_name")
        return device_version.DeviceVersionInfo(
            scb_part_number="21-0010",
            hardware_version="00",
            model_id="00",
            mcu_fw=(1, 0, 0),
            bridge_fw="0463",
        )

    monkeypatch.setattr("usb_tool.services.query_device_version", _fake_query)

    info = populate_device_version(
        0x0984,
        0x1407,
        "SER123",
        bsd_name="/dev/disk4",
    )

    assert info == {
        "scbPartNumber": "21-0010",
        "hardwareVersion": "00",
        "modelID": "00",
        "mcuFW": "1.0.0",
        "bridgeFW": "0463",
    }
    assert captured["bsd_name"] == "/dev/disk4"


def test_query_device_version_uses_linux_sg_io(monkeypatch):
    payload = bytes.fromhex(
        "5917046341707269636f726e536563757265204b657920332e3020203678000328"
        "43292032303131202d2032302020202032312d3030313030303030303031e0"
    )

    monkeypatch.setattr(device_version.sys, "platform", "linux")
    monkeypatch.setattr(
        device_version,
        "_linux_read_buffer",
        lambda path, **_kwargs: payload,
        raising=False,
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("_query_usb_core should not run for Linux block devices")

    monkeypatch.setattr(device_version, "_query_usb_core", _unexpected)

    info = device_version.query_device_version(
        0x0984,
        0x1407,
        "000000000001",
        device_path="/dev/sda",
    )

    assert info.raw_data == payload
    assert info.scb_part_number == "21-0010"
    assert info.model_id == "00"
    assert info.hardware_version == "00"
    assert info.mcu_fw == (1, 0, 0)
    assert info.bridge_fw == "0463"


def test_parse_payload_flips_model_and_hardware_digit_order():
    info = device_version._parse_payload_best_effort(b"\x00\x00\x04\x63Apricorn21-00101234567")

    assert info.scb_part_number == "21-0010"
    assert info.model_id == "21"
    assert info.hardware_version == "43"
    assert info.mcu_fw == (7, 6, 5)
    assert info.bridge_fw == "0463"


def test_illegal_request_invalid_command_sense_is_detected():
    sense = bytes.fromhex("70 00 05 00 00 00 00 0a 00 00 00 00 20 00 00 00")

    assert device_version._is_illegal_request_invalid_command(sense) is True
    assert device_version._is_illegal_request_invalid_command(b"\x70\x00\x05") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific fallback path")
def test_query_device_version_falls_back_to_ata_read_buffer_dma(monkeypatch):
    payload = bytes.fromhex(
        "0000040f01000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000020202032312d3030313032303030363031e0"
    )
    sense = bytes.fromhex("70 00 05 00 00 00 00 0a 00 00 00 00 20 00 00 00")

    monkeypatch.setattr(device_version.sys, "platform", "win32")

    def _reject_scsi(*_args, **_kwargs):
        raise device_version.ScsiReadBufferUnsupportedError(sense)

    monkeypatch.setattr(device_version, "_windows_read_buffer", _reject_scsi)
    monkeypatch.setattr(
        device_version, "_windows_ata_read_buffer_dma", lambda *_args, **_kwargs: payload
    )

    profile = {}
    info = device_version.query_device_version(
        0x0984,
        0x1408,
        "000000000014",
        physical_drive_num=1,
        profile=profile,
    )

    assert info.raw_data == payload
    assert info.scb_part_number == "21-0010"
    assert info.bridge_fw == "040F"
    assert profile["transport"] == "windows_spti_ata_read_buffer_dma"
    assert profile["scsi_read_buffer_rejected"] == "illegal_request_invalid_command"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-specific fallback path")
def test_query_device_version_falls_back_to_linux_ata_read_buffer_dma(monkeypatch):
    payload = bytes.fromhex(
        "0000040f01000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000020202032312d3030313032303030363031e0"
    )
    sense = bytes.fromhex("70 00 05 00 00 00 00 0a 00 00 00 00 20 00 00 00")
    calls = []

    monkeypatch.setattr(device_version.sys, "platform", "linux")
    monkeypatch.setattr(device_version.os, "open", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(device_version.os, "close", lambda *_args, **_kwargs: None)

    def _fake_sg_io_read(_fd, cdb, *_args, **_kwargs):
        calls.append(cdb)
        if len(calls) == 1:
            raise device_version.ScsiReadBufferUnsupportedError(sense)
        return payload

    monkeypatch.setattr(device_version, "_linux_sg_io_read", _fake_sg_io_read)

    profile = {}
    info = device_version.query_device_version(
        0x0984,
        0x1408,
        "000000000014",
        device_path="/dev/sda",
        profile=profile,
    )

    assert info.raw_data == payload
    assert info.scb_part_number == "21-0010"
    assert info.bridge_fw == "040F"
    assert len(calls[0]) == 10
    assert len(calls[1]) == 16
    assert profile["transport"] == "linux_sg_io_ata_read_buffer_dma"
    assert profile["linux_read_buffer_rejected"] == "illegal_request_invalid_command"


def test_query_device_version_falls_back_to_libusb_ata_read_buffer_dma(monkeypatch):
    payload = bytes.fromhex(
        "0000040f01000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000020202032312d3030313032303030363031e0"
    )
    writes = []
    resets = []
    cleared_halts = []

    class FakeUSBError(Exception):
        pass

    class FakeEndpoint:
        def __init__(self, address, reads=None):
            self.bEndpointAddress = address
            self._reads = list(reads or [])

        def write(self, data):
            writes.append(bytes(data))

        def read(self, *_args, **_kwargs):
            if self._reads:
                return self._reads.pop(0)
            return b""

    ep_out = FakeEndpoint(0x01)
    ep_in = FakeEndpoint(0x81, reads=[b"", b"\x00" * 13, payload, b"\x00" * 13])

    class FakeDevice:
        def is_kernel_driver_active(self, *_args):
            return False

        def set_configuration(self):
            pass

        def get_active_configuration(self):
            return {(0, 0): [ep_out, ep_in]}

        def ctrl_transfer(self, *args, **kwargs):
            resets.append((args, kwargs))

        def attach_kernel_driver(self, *_args):
            pass

    usb_pkg = types.ModuleType("usb")
    core_mod = types.ModuleType("usb.core")
    util_mod = types.ModuleType("usb.util")
    core_mod.USBError = FakeUSBError
    core_mod.find = lambda **_kwargs: FakeDevice()
    util_mod.ENDPOINT_OUT = 0
    util_mod.ENDPOINT_IN = 0x80
    util_mod.endpoint_direction = lambda address: address & 0x80
    util_mod.find_descriptor = lambda intf, custom_match: next(
        endpoint for endpoint in intf if custom_match(endpoint)
    )
    util_mod.release_interface = lambda *_args: None
    util_mod.clear_halt = lambda _dev, endpoint_address: cleared_halts.append(endpoint_address)
    usb_pkg.core = core_mod
    usb_pkg.util = util_mod
    monkeypatch.setitem(sys.modules, "usb", usb_pkg)
    monkeypatch.setitem(sys.modules, "usb.core", core_mod)
    monkeypatch.setitem(sys.modules, "usb.util", util_mod)
    monkeypatch.setattr(device_version.sys, "platform", "darwin")

    profile = {}
    info = device_version.query_device_version(
        0x0984,
        0x1408,
        "000000000014",
        profile=profile,
    )

    assert info.raw_data == payload
    assert info.scb_part_number == "21-0010"
    assert info.bridge_fw == "040F"
    assert writes[0][15:25] == device_version._build_read_buffer_10_cdb(1024)
    assert writes[1][15:31] == device_version._build_ata_read_buffer_dma_passthrough_cdb()
    assert resets
    assert cleared_halts == [ep_in.bEndpointAddress, ep_out.bEndpointAddress]
    assert profile["transport"] == "usb_core_ata_read_buffer_dma"
    assert profile["usb_core_read_buffer_empty"] is True
    assert profile["usb_core_bot_recovery_before_ata"] is True


def test_linux_scan_hides_version_fields_when_bridge_mismatches_bcd():
    backend = LinuxBackend()
    lsblk_rows = [
        {
            "name": "/dev/sda",
            "serial": "SER123",
            "size_gb": 15.8,
            "mediaType": "Basic Disk",
        }
    ]
    lsusb_details = {
        "SER123": {
            "idVendor": "0984",
            "idProduct": "1407",
            "bcdUSB": "3.2",
            "bcdDevice": "0502",
            "iManufacturer": "Apricorn",
            "iProduct": "Secure Key 3.0",
        }
    }
    with (
        patch.object(LinuxBackend, "_list_usb_drives", return_value=lsblk_rows),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sda": _LinuxBlockDeviceProbe(
                    block_device="/dev/sda",
                    serial="SER123",
                    driver_name="uas",
                    driver_transport="UAS",
                    pci_addr="0000:00:14.0",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sda": "Intel"},
        ),
        patch.object(LinuxBackend, "_get_lsusb_details", return_value=lsusb_details),
        patch(
            "usb_tool.backend.linux.populate_device_version",
            return_value={
                "scbPartNumber": "12-3456",
                "hardwareVersion": "01",
                "modelID": "AA",
                "mcuFW": "1.2.3",
                "bridgeFW": "0503",
            },
        ),
    ):
        devices = backend.scan_devices()

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    for name in VERSION_FIELD_NAMES:
        assert name not in serialized


def test_linux_scan_keeps_version_fields_when_bridge_matches_bcd():
    backend = LinuxBackend()
    lsblk_rows = [
        {
            "name": "/dev/sda",
            "serial": "SER123",
            "size_gb": 15.8,
            "mediaType": "Basic Disk",
        }
    ]
    lsusb_details = {
        "SER123": {
            "idVendor": "0984",
            "idProduct": "1407",
            "bcdUSB": "3.2",
            "bcdDevice": "0502",
            "iManufacturer": "Apricorn",
            "iProduct": "Secure Key 3.0",
        }
    }
    with (
        patch.object(LinuxBackend, "_list_usb_drives", return_value=lsblk_rows),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sda": _LinuxBlockDeviceProbe(
                    block_device="/dev/sda",
                    serial="SER123",
                    driver_name="uas",
                    driver_transport="UAS",
                    pci_addr="0000:00:14.0",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sda": "Intel"},
        ),
        patch.object(LinuxBackend, "_get_lsusb_details", return_value=lsusb_details),
        patch(
            "usb_tool.backend.linux.populate_device_version",
            return_value={
                "scbPartNumber": "12-3456",
                "hardwareVersion": "01",
                "modelID": "AA",
                "mcuFW": "1.2.3",
                "bridgeFW": "0502",
            },
        ),
    ):
        devices = backend.scan_devices()

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    for name in VERSION_FIELD_NAMES:
        assert name in serialized


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific path format")
def test_windows_read_buffer_uses_device_namespace_path(monkeypatch):
    captured = {}

    def _fake_create_file(path, *_args):
        captured["path"] = path
        return device_version.INVALID_HANDLE_VALUE

    monkeypatch.setattr("ctypes.windll.kernel32.CreateFileW", _fake_create_file)
    monkeypatch.setattr("ctypes.GetLastError", lambda: device_version.errno.EACCES)

    with pytest.raises(PermissionError):
        device_version._windows_read_buffer(4)

    assert captured["path"] == r"\\.\PhysicalDrive4"
