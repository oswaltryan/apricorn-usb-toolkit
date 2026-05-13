"""Unit tests for windows_usb module."""

import json
import sys

import pytest

# Skip this entire module if not on Windows
if sys.platform != "win32":
    pytest.skip("Windows only tests", allow_module_level=True)

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from usb_tool.backend.windows import (
    WindowsBackend,
    _classify_usb_controller_name,
    _derive_media_type_from_drive_letters,
    _normalize_disk_media_type,
    _normalize_logical_disk_identifier,
)
from usb_tool.utils import bytes_to_gb


def _extract_profile_json(stderr_text: str, prefix: str) -> dict[str, object]:
    marker = f"{prefix}: "
    lines = stderr_text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith(marker):
            json_lines = [line[len(marker) :]]
            brace_balance = json_lines[0].count("{") - json_lines[0].count("}")
            cursor = idx + 1
            while brace_balance > 0 and cursor < len(lines):
                next_line = lines[cursor]
                json_lines.append(next_line)
                brace_balance += next_line.count("{") - next_line.count("}")
                cursor += 1
            payload = json.loads("\n".join(json_lines))
            if isinstance(payload, dict):
                return payload
            raise AssertionError(f"profile payload is not a JSON object for {prefix}")
    raise AssertionError(f"missing profile json line for {prefix}")


def test_get_drive_letter_via_ps_handles_invalid_index():
    with patch("usb_tool.backend.windows.win32com.client.Dispatch"):
        backend = WindowsBackend()
        assert backend.get_drive_letter_via_ps(-1) == "Not Formatted"


def test_get_drive_letter_via_ps_parses_output():
    mock_result = SimpleNamespace(stdout="E:\n", returncode=0)
    with (
        patch("usb_tool.backend.windows.win32com.client.Dispatch"),
        patch("usb_tool.backend.windows.subprocess.run", return_value=mock_result),
    ):
        backend = WindowsBackend()
        assert backend.get_drive_letter_via_ps(1) == "E:"


def test_get_wmi_usb_devices_skips_excluded_pids():
    class DummyDevice:
        def __init__(self, device_id, description="USB Mass Storage Device"):
            self.DeviceID = device_id
            self.Description = description

    devices = [
        DummyDevice(r"USB\\VID_0984&PID_0221&REV_0000\\SER_BAD1"),
        DummyDevice(r"USB\\VID_0984&PID_0301\\SER_BAD2"),
        DummyDevice(r"USB\\VID_0984&PID_1234&REV_0000\\SER_GOOD"),
    ]

    mock_service = MagicMock()
    mock_service.ExecQuery.return_value = devices

    with patch("usb_tool.backend.windows.win32com.client.Dispatch") as mock_dispatch:
        mock_dispatch.return_value.ConnectServer.return_value = mock_service

        backend = WindowsBackend()
        # Ensure our mock service is used (it should be via __init__)
        # backend.service is set in __init__

        result = backend._get_wmi_usb_devices()

    assert len(result) == 1
    assert result[0]["pid"] == "1234"
    assert result[0]["serial"] == "SER_GOOD"


def test_should_retry_scan_detects_partial_lists():
    with patch("usb_tool.backend.windows.win32com.client.Dispatch"):
        backend = WindowsBackend()
        assert backend._should_retry_scan([0, 1, 1, 0]) is True
        assert backend._should_retry_scan([0, 0, 0, 0]) is False
        assert backend._should_retry_scan([2, 2, 2, 2]) is False


def test_classify_usb_controller_name_detects_renesas_from_name_or_pci_vendor():
    assert _classify_usb_controller_name("Renesas USB 3.0 eXtensible Host Controller") == "Renesas"
    assert (
        _classify_usb_controller_name("USB xHCI Controller", r"PCI\VEN_1912&DEV_0015") == "Renesas"
    )
    assert (
        _classify_usb_controller_name("USB xHCI Controller", r"PCI\VEN_1B21&DEV_1142") == "ASMedia"
    )
    assert (
        _classify_usb_controller_name("USB xHCI Controller", r"PCI\VEN_1B73&DEV_1100")
        == "Fresco Logic"
    )


def test_find_apricorn_device_retries_once_on_partial_scan():
    fake_device = SimpleNamespace()
    scan_results = [
        (None, [0, 1, 1, 0]),
        ([fake_device], [1, 1, 1, 1]),
    ]

    with (
        patch("usb_tool.backend.windows.win32com.client.Dispatch"),
        patch.object(WindowsBackend, "_perform_scan_pass", side_effect=scan_results) as scan_mock,
        patch("time.sleep"),
    ):
        backend = WindowsBackend()
        backend._native_scan_enabled = False
        devices = backend.scan_devices()

    assert devices == [fake_device]
    assert scan_mock.call_count == 2


def test_instantiate_devices_sets_drive_letter_from_map():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    wmi_usb_devices = [
        {
            "pid": "1407",
            "vid": "0984",
            "serial": "SER123",
            "manufacturer": "Apricorn",
            "usbDriverProvider": "Apricorn",
            "usbDriverVersion": "1.2.3.4",
            "usbDriverInf": "oem17.inf",
        }
    ]
    wmi_usb_drives = [
        {
            "size_gb": 15.8,
            "iProduct": "Secure Key 3.0",
            "mediaType": "Basic Disk",
            "pnpdeviceid": r"USBSTOR\\DISK&VEN_APRICORN&PROD_KEY\\SER123&0",
            "diskDriverInfo": {
                "provider": "Microsoft",
                "version": "10.0.1",
                "inf": "disk.inf",
            },
        }
    ]
    usb_controllers = [{"ControllerName": "Intel"}]
    libusb_data = [{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 16}]
    physical_drives = {"SER123": 3}
    readonly_map = {3: False}
    drive_letters_map = {3: "F:"}

    with patch("usb_tool.backend.windows.populate_device_version", return_value={}):
        devices = backend._instantiate_devices(
            wmi_usb_devices,
            wmi_usb_drives,
            usb_controllers,
            libusb_data,
            physical_drives,
            readonly_map,
            drive_letters_map,
            include_controller=True,
            include_drive_letter=True,
        )

    assert devices and devices[0].to_dict()["driveLetter"] == "F:"
    serialized = devices[0].to_dict()
    assert serialized["driverTransport"] == "BOT"
    assert serialized["usbDriverProvider"] == "Apricorn"
    assert serialized["diskDriverProvider"] == "Microsoft"


def test_instantiate_devices_falls_back_to_powershell_for_drive_letter():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    wmi_usb_devices = [
        {
            "pid": "1407",
            "vid": "0984",
            "serial": "SER123",
            "manufacturer": "Apricorn",
            "usbDriverProvider": "Apricorn",
            "usbDriverVersion": "1.2.3.4",
            "usbDriverInf": "oem17.inf",
        }
    ]
    wmi_usb_drives = [
        {
            "size_gb": 15.8,
            "iProduct": "Secure Key 3.0",
            "mediaType": "Basic Disk",
            "pnpdeviceid": r"USBSTOR\\DISK&VEN_APRICORN&PROD_KEY\\SER123&0",
            "diskDriverInfo": {
                "provider": "Microsoft",
                "version": "10.0.1",
                "inf": "disk.inf",
            },
        }
    ]
    usb_controllers = [{"ControllerName": "Intel"}]
    libusb_data = [{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 16}]
    physical_drives = {"SER123": 3}
    readonly_map = {3: False}
    drive_letters_map = {}

    with (
        patch("usb_tool.backend.windows.populate_device_version", return_value={}),
        patch.object(WindowsBackend, "get_drive_letter_via_ps", return_value="G:"),
    ):
        devices = backend._instantiate_devices(
            wmi_usb_devices,
            wmi_usb_drives,
            usb_controllers,
            libusb_data,
            physical_drives,
            readonly_map,
            drive_letters_map,
            include_controller=True,
            include_drive_letter=True,
        )

    assert devices and devices[0].to_dict()["driveLetter"] == "G:"


def test_instantiate_devices_treats_500kb_media_size_as_oob():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    wmi_usb_devices = [
        {
            "pid": "0310",
            "vid": "0984",
            "serial": "SER123",
            "manufacturer": "Apricorn",
            "usbDriverProvider": "Apricorn",
            "usbDriverVersion": "1.2.3.4",
            "usbDriverInf": "oem17.inf",
        }
    ]
    wmi_usb_drives = [
        {
            "size_gb": bytes_to_gb(500 * 1024),
            "iProduct": "Aegis Padlock 3.0",
            "mediaType": "Basic Disk",
            "pnpdeviceid": r"USBSTOR\\DISK&VEN_APRICORN&PROD_PADLOCK\\SER123&0",
            "diskDriverInfo": {
                "provider": "Microsoft",
                "version": "10.0.1",
                "inf": "disk.inf",
            },
        }
    ]
    usb_controllers = [{"ControllerName": "Intel"}]
    libusb_data = [{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 16}]
    physical_drives = {"SER123": 3}

    with (
        patch("usb_tool.backend.windows.populate_device_version", return_value={}),
        patch.object(WindowsBackend, "get_drive_letter_via_ps", return_value="G:") as fallback,
    ):
        devices = backend._instantiate_devices(
            wmi_usb_devices,
            wmi_usb_drives,
            usb_controllers,
            libusb_data,
            physical_drives,
            readonly_map={3: False},
            drive_letters_map={},
            include_controller=True,
            include_drive_letter=True,
        )

    assert len(devices) == 1
    assert devices[0].driveSizeGB == "N/A (OOB Mode)"
    assert devices[0].driveLetter == "Not Formatted"
    fallback.assert_not_called()


def test_instantiate_devices_omits_drive_letter_in_minimal_mode():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    wmi_usb_devices = [
        {
            "pid": "1407",
            "vid": "0984",
            "serial": "SER123",
            "manufacturer": "Apricorn",
            "usbDriverProvider": "Apricorn",
            "usbDriverVersion": "1.2.3.4",
            "usbDriverInf": "oem17.inf",
        }
    ]
    wmi_usb_drives = [
        {
            "size_gb": 15.8,
            "iProduct": "Secure Key 3.0",
            "mediaType": "Basic Disk",
            "pnpdeviceid": r"SCSI\\DISK&VEN_APRICORN&PROD_KEY\\MSFT30SER123&0",
            "diskDriverInfo": {
                "provider": "Microsoft",
                "version": "10.0.1",
                "inf": "disk.inf",
            },
        }
    ]
    usb_controllers = [{"ControllerName": "Intel"}]
    libusb_data = [{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 16}]
    physical_drives = {"SER123": 3}
    readonly_map = {3: False}
    drive_letters_map = {3: "F:"}

    with patch("usb_tool.backend.windows.populate_device_version", return_value={}):
        devices = backend._instantiate_devices(
            wmi_usb_devices,
            wmi_usb_drives,
            usb_controllers,
            libusb_data,
            physical_drives,
            readonly_map,
            drive_letters_map,
            include_controller=False,
            include_drive_letter=False,
        )

    assert devices
    serialized = devices[0].to_dict()
    assert "driveLetter" not in serialized
    assert "usbController" not in serialized
    assert serialized["driverTransport"] == "UAS"


def test_get_signed_driver_info_returns_default_on_query_failure():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    backend.service = MagicMock()
    backend.service.ExecQuery.side_effect = RuntimeError("boom")

    result = backend._get_signed_driver_info(r"USB\\VID_0984&PID_1407\\SER123")

    assert result == {"provider": "N/A", "version": "N/A", "inf": "N/A"}


def test_get_signed_driver_info_map_builds_bulk_lookup():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1

    class DummyDriverRecord:
        DeviceID = r"USB\\VID_0984&PID_1407&REV_0300\\SER123"
        DriverProviderName = "Apricorn"
        DriverVersion = "21.46.5.13"
        InfName = "oem17.inf"

    backend.service = MagicMock()
    backend.service.ExecQuery.return_value = [DummyDriverRecord()]

    result = backend._get_signed_driver_info_map({DummyDriverRecord.DeviceID})

    assert result[DummyDriverRecord.DeviceID]["provider"] == "Apricorn"
    assert result[DummyDriverRecord.DeviceID]["version"] == "21.46.5.13"
    assert result[DummyDriverRecord.DeviceID]["inf"] == "oem17.inf"


def test_apply_usb_driver_info_populates_usb_driver_fields():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    devices = [
        {
            "device_id": r"USB\\VID_0984&PID_1407&REV_0300\\SER123",
            "usbDriverProvider": "N/A",
            "usbDriverVersion": "N/A",
            "usbDriverInf": "N/A",
        }
    ]

    backend._apply_usb_driver_info(
        devices,
        {
            r"USB\\VID_0984&PID_1407&REV_0300\\SER123": {
                "provider": "Apricorn",
                "version": "21.46.5.13",
                "inf": "oem17.inf",
            }
        },
    )

    assert devices[0]["usbDriverProvider"] == "Apricorn"
    assert devices[0]["usbDriverVersion"] == "21.46.5.13"
    assert devices[0]["usbDriverInf"] == "oem17.inf"


def test_perform_scan_pass_batches_usb_driver_lookup_only_for_default_mode():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    backend._get_wmi_usb_devices = MagicMock(
        return_value=[
            {
                "vid": "0984",
                "pid": "1407",
                "serial": "SER123",
                "device_id": r"USB\\VID_0984&PID_1407\\SER123",
                "description": "Apricorn USB",
                "usbDriverProvider": "N/A",
                "usbDriverVersion": "N/A",
                "usbDriverInf": "N/A",
            }
        ]
    )
    backend._get_wmi_diskdrives = MagicMock(return_value=[])
    backend._get_wmi_usb_drives = MagicMock(
        return_value=[
            {
                "size_gb": 15.8,
                "iProduct": "Secure Key 3.0",
                "mediaType": "Basic Disk",
                "pnpdeviceid": r"USBSTOR\\DISK&VEN_APRICORN&PROD_KEY\\SER123&0",
                "diskDriverInfo": {
                    "provider": "N/A",
                    "version": "N/A",
                    "inf": "N/A",
                },
            }
        ]
    )
    backend._get_apricorn_libusb_data = MagicMock(
        return_value=[{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 2}]
    )
    backend._get_physical_drive_number = MagicMock(return_value={})
    backend._sort_wmi_drives = MagicMock(side_effect=lambda devices, drives: drives)
    backend._sort_libusb_data = MagicMock(side_effect=lambda devices, data: data)
    backend._get_usb_readonly_status_map_wmi = MagicMock(return_value={})
    backend._get_drive_letters_map_wmi = MagicMock(return_value={})
    backend._apply_usb_driver_info = MagicMock()
    backend._apply_disk_driver_info = MagicMock()
    backend._instantiate_devices = MagicMock(return_value=[])
    backend._get_signed_driver_info_map = MagicMock(return_value={})

    backend._perform_scan_pass(minimal=False, expanded=False)

    backend._get_signed_driver_info_map.assert_not_called()
    backend._apply_usb_driver_info.assert_not_called()
    backend._apply_disk_driver_info.assert_not_called()


def test_perform_scan_pass_includes_disk_driver_lookup_for_json_mode():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    backend._get_wmi_usb_devices = MagicMock(
        return_value=[
            {
                "vid": "0984",
                "pid": "1407",
                "serial": "SER123",
                "device_id": r"USB\\VID_0984&PID_1407\\SER123",
                "description": "Apricorn USB",
                "usbDriverProvider": "N/A",
                "usbDriverVersion": "N/A",
                "usbDriverInf": "N/A",
            }
        ]
    )
    backend._get_wmi_diskdrives = MagicMock(return_value=[])
    backend._get_wmi_usb_drives = MagicMock(
        return_value=[
            {
                "size_gb": 15.8,
                "iProduct": "Secure Key 3.0",
                "mediaType": "Basic Disk",
                "pnpdeviceid": r"USBSTOR\\DISK&VEN_APRICORN&PROD_KEY\\SER123&0",
                "diskDriverInfo": {
                    "provider": "N/A",
                    "version": "N/A",
                    "inf": "N/A",
                },
            }
        ]
    )
    backend._get_apricorn_libusb_data = MagicMock(
        return_value=[{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 2}]
    )
    backend._get_physical_drive_number = MagicMock(return_value={})
    backend._sort_wmi_drives = MagicMock(side_effect=lambda devices, drives: drives)
    backend._sort_libusb_data = MagicMock(side_effect=lambda devices, data: data)
    backend._get_usb_readonly_status_map_wmi = MagicMock(return_value={})
    backend._get_drive_letters_map_wmi = MagicMock(return_value={})
    backend._apply_usb_driver_info = MagicMock()
    backend._apply_disk_driver_info = MagicMock()
    backend._instantiate_devices = MagicMock(return_value=[])
    backend._get_signed_driver_info_map = MagicMock(return_value={})

    backend._perform_scan_pass(minimal=False, expanded=True)

    backend._get_signed_driver_info_map.assert_called_once_with(
        {
            r"USB\\VID_0984&PID_1407\\SER123",
            r"USBSTOR\\DISK&VEN_APRICORN&PROD_KEY\\SER123&0",
        }
    )
    backend._apply_disk_driver_info.assert_called_once()


def test_perform_scan_pass_emits_profile_output_when_enabled(monkeypatch, capsys):
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = True
    backend._scan_pass_index = 1
    backend._get_wmi_usb_devices = MagicMock(return_value=[])
    backend._get_wmi_diskdrives = MagicMock(return_value=[])
    backend._get_wmi_usb_drives = MagicMock(return_value=[])
    backend._get_apricorn_libusb_data = MagicMock(return_value=[])
    backend._get_physical_drive_number = MagicMock(return_value={})
    backend._get_signed_driver_info_map = MagicMock(return_value={})
    backend._apply_usb_driver_info = MagicMock()
    backend._apply_disk_driver_info = MagicMock()
    backend._sort_wmi_drives = MagicMock(side_effect=lambda devices, drives: drives)
    backend._get_usb_controllers_wmi = MagicMock(return_value=[])
    backend._sort_usb_controllers = MagicMock(side_effect=lambda devices, controllers: controllers)
    backend._sort_libusb_data = MagicMock(side_effect=lambda devices, data: data)
    backend._get_usb_readonly_status_map_wmi = MagicMock(return_value={})
    backend._get_drive_letters_map_wmi = MagicMock(return_value={})
    backend._instantiate_devices = MagicMock(return_value=[])

    backend._perform_scan_pass(minimal=False, expanded=False)

    captured = capsys.readouterr()
    assert "windows-scan-profile" in captured.err
    assert "pass=1" in captured.err
    assert "wmi_usb_devices=" in captured.err
    assert "instantiate_devices=" in captured.err


def test_get_drive_letters_map_wmi_emits_partition_diagnostics(capsys):
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = True
    backend._scan_pass_index = 1
    backend.service = MagicMock()

    class DummyDisk:
        Index = 3
        DeviceID = r"\\.\PHYSICALDRIVE3"

    class DummyAssoc:
        def __init__(self, antecedent, dependent):
            self.Antecedent = antecedent
            self.Dependent = dependent

    backend.service.ExecQuery.side_effect = [
        [
            DummyAssoc(
                'Win32_DiskDrive.DeviceID="\\\\\\\\.\\\\PHYSICALDRIVE3"',
                "Disk #3, Partition #0",
            )
        ],
        [DummyAssoc("Disk #3, Partition #0", "D:")],
    ]
    result = backend._get_drive_letters_map_wmi([DummyDisk()], {3})

    captured = capsys.readouterr()
    assert result == {3: "D:"}
    assert (
        "windows-drive-letter-profile: pass=1 disk_index=3 stage=bulk_partitions count=1"
        in captured.err
    )
    assert (
        "windows-drive-letter-profile: pass=1 disk_index=3 "
        "stage=bulk_partition_result partition=Disk #3, Partition #0 "
        "letters=D:" in captured.err
    )


def test_instantiate_devices_emits_fallback_diagnostics(capsys):
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = True
    backend._scan_pass_index = 1
    wmi_usb_devices = [
        {
            "pid": "1407",
            "vid": "0984",
            "serial": "SER123",
            "manufacturer": "Apricorn",
            "usbDriverProvider": "N/A",
            "usbDriverVersion": "N/A",
            "usbDriverInf": "N/A",
        }
    ]
    wmi_usb_drives = [
        {
            "size_gb": 15.8,
            "iProduct": "Secure Key 3.0",
            "mediaType": "Basic Disk",
            "pnpdeviceid": r"USBSTOR\\DISK&VEN_APRICORN&PROD_KEY\\SER123&0",
            "diskDriverInfo": {
                "provider": "N/A",
                "version": "N/A",
                "inf": "N/A",
            },
        }
    ]
    usb_controllers = [{"ControllerName": "Intel"}]
    libusb_data = [{"bcdUSB": 3.2, "bcdDevice": "0502", "bus_number": 1, "dev_address": 16}]
    with (
        patch("usb_tool.backend.windows.populate_device_version", return_value={}),
        patch.object(WindowsBackend, "get_drive_letter_via_ps", return_value="D:"),
    ):
        devices = backend._instantiate_devices(
            wmi_usb_devices,
            wmi_usb_drives,
            usb_controllers,
            libusb_data,
            {"SER123": 3},
            {3: False},
            {},
            include_controller=True,
            include_drive_letter=True,
        )

    captured = capsys.readouterr()
    assert devices[0].driveLetter == "D:"
    assert (
        "windows-drive-letter-profile: pass=1 disk_index=3 "
        "stage=fallback_triggered serial=SER123 size_raw=15.8" in captured.err
    )
    assert (
        "windows-drive-letter-profile: pass=1 disk_index=3 stage=fallback_result letter=D:"
        in captured.err
    )
    assert "windows-scan-profile-details:" in captured.err
    profile_json = _extract_profile_json(captured.err, "windows-scan-profile-details")
    assert profile_json["pass"] == 1
    assert profile_json["device_count"] == 1


def test_get_drive_letters_map_wmi_uses_bulk_associations_for_drive_letter(capsys):
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = True
    backend._scan_pass_index = 1
    backend.service = MagicMock()

    class DummyDisk:
        Index = 1
        DeviceID = r"\\.\PHYSICALDRIVE1"

    class DummyAssoc:
        def __init__(self, antecedent, dependent):
            self.Antecedent = antecedent
            self.Dependent = dependent

    backend.service.ExecQuery.side_effect = [
        [
            DummyAssoc(
                'Win32_DiskDrive.DeviceID="\\\\\\\\.\\\\PHYSICALDRIVE1"',
                '\\\\DESKTOP-NF3343M\\root\\cimv2:Win32_DiskPartition.DeviceID="Disk #1, '
                'Partition #0"',
            )
        ],
        [
            DummyAssoc(
                '\\\\DESKTOP-NF3343M\\root\\cimv2:Win32_DiskPartition.DeviceID="Disk #1, '
                'Partition #0"',
                '\\\\DESKTOP-NF3343M\\root\\cimv2:Win32_LogicalDisk.DeviceID="D:"',
            )
        ],
    ]
    result = backend._get_drive_letters_map_wmi([DummyDisk()], {1})

    captured = capsys.readouterr()
    assert result == {1: "D:"}
    assert (
        "windows-drive-letter-profile: pass=1 disk_index=1 stage=bulk_partitions count=1"
        in captured.err
    )
    assert (
        "windows-drive-letter-profile: pass=1 disk_index=1 stage=bulk_partition_result "
        'partition=\\\\DESKTOP-NF3343M\\root\\cimv2:Win32_DiskPartition.DeviceID="Disk #1, '
        'Partition #0" '
        "letters=D:" in captured.err
    )


def test_normalize_logical_disk_identifier_extracts_drive_letter():
    assert (
        _normalize_logical_disk_identifier(
            '\\\\DESKTOP-NF3343M\\root\\cimv2:Win32_LogicalDisk.DeviceID="D:"'
        )
        == "D:"
    )


def test_normalize_disk_media_type_maps_removable_keyword():
    assert _normalize_disk_media_type("Removable Media") == "Removable Media"
    assert _normalize_disk_media_type("Fixed hard disk media") == "Basic Disk"
    assert _normalize_disk_media_type("") == "Basic Disk"


def test_derive_media_type_from_drive_letters_prefers_removable(monkeypatch):
    class _Kernel32:
        @staticmethod
        def GetDriveTypeW(path):
            if path == "E:\\":
                return 2
            return 3

    monkeypatch.setattr("usb_tool.backend.windows.kernel32", _Kernel32())
    assert _derive_media_type_from_drive_letters("C:, E:", "Basic Disk") == "Removable Media"


def test_build_usb_drives_from_interfaces_uses_disk_media_type_map():
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = False
    backend._scan_pass_index = 1
    wmi_usb_devices = [
        {
            "serial": "SER123",
            "description": "Apricorn USB Device",
        }
    ]
    disk_interfaces = [
        {
            "normalized_path": r"\\?\usb#disk&ven_apricorn&prod_key#ser123&0#{53f56307-b6bf}",
            "device_path": r"\\?\usb#disk&ven_apricorn&prod_key#SER123&0#{53f56307-b6bf}",
            "device_number": 3,
            "product_hint": "Secure Key 3.0",
        }
    ]

    drives = backend._build_usb_drives_from_interfaces(
        wmi_usb_devices,
        disk_interfaces,
        storage_metrics_map={3: {"size_gb": 15.8}},
        media_type_map={3: "Removable Media"},
    )

    assert len(drives) == 1
    assert drives[0]["mediaType"] == "Removable Media"


def test_get_drive_letters_map_wmi_skips_logging_when_no_candidate_indices(capsys):
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = True
    backend._scan_pass_index = 2
    backend.service = MagicMock()

    class DummyDisk:
        Index = 0
        DeviceID = r"\\.\PHYSICALDRIVE0"

    result = backend._get_drive_letters_map_wmi([DummyDisk()], set())

    captured = capsys.readouterr()
    assert result == {}
    assert "windows-drive-letter-profile: pass=2 skipped=no_candidate_drive_indices" in captured.err


def test_native_payload_to_devices_parses_contract_shape(monkeypatch):
    backend = object.__new__(WindowsBackend)
    monkeypatch.setattr(
        "usb_tool.backend.windows._derive_media_type_from_drive_letters",
        lambda drive_letters, fallback="Basic Disk": "Basic Disk",
    )
    payload = {
        "devices": [
            {
                "1": {
                    "bcdUSB": 3.2,
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdDevice": "0502",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Secure Key 3.0",
                    "iSerial": "SER123",
                    "driverTransport": "BOT",
                    "driveSizeGB": 16,
                    "mediaType": "Basic Disk",
                    "usbDriverProvider": "N/A",
                    "usbDriverVersion": "N/A",
                    "usbDriverInf": "N/A",
                    "diskDriverProvider": "N/A",
                    "diskDriverVersion": "N/A",
                    "diskDriverInf": "N/A",
                    "usbController": "N/A",
                    "busNumber": 1,
                    "deviceAddress": 4,
                    "physicalDriveNum": 3,
                    "driveLetter": "F:",
                    "fileSystem": "NTFS",
                    "readOnly": False,
                }
            }
        ]
    }

    devices = backend._native_payload_to_devices(payload)

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    assert serialized["idVendor"] == "0984"
    assert serialized["idProduct"] == "1407"
    assert serialized["driveSizeGB"] == 16
    assert serialized["physicalDriveNum"] == 3
    assert serialized["driveLetter"] == "F:"
    assert serialized["fileSystem"] == "NTFS"


def test_correct_native_usb_controllers_uses_wmi_controller_relationship():
    backend = object.__new__(WindowsBackend)
    backend._ensure_wmi_ready = MagicMock()
    backend._get_usb_controllers_wmi = MagicMock(
        return_value=[
            {
                "DeviceID": r"USB\VID_0984&PID_0310\101600048177",
                "ControllerName": "Renesas",
            }
        ]
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "0310",
                    "iSerial": "101600048177",
                    "usbController": "ASMedia",
                }
            }
        ]
    }
    devices = backend._native_payload_to_devices(payload)

    backend._correct_native_usb_controllers(devices)

    assert len(devices) == 1
    assert devices[0].usbController == "Renesas"


def test_native_payload_to_devices_derives_removable_media_from_drive_letter(monkeypatch):
    backend = object.__new__(WindowsBackend)
    monkeypatch.setattr(
        "usb_tool.backend.windows._derive_media_type_from_drive_letters",
        lambda drive_letters, fallback="Basic Disk": "Removable Media",
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1410",
                    "iSerial": "SER1410",
                    "driveSizeGB": 16,
                    "mediaType": "Basic Disk",
                    "driveLetter": "E:",
                }
            }
        ]
    }

    devices = backend._native_payload_to_devices(payload)

    assert len(devices) == 1
    assert devices[0].mediaType == "Removable Media"


def test_native_payload_to_devices_accepts_raw_filesystem_from_native_payload(monkeypatch):
    backend = object.__new__(WindowsBackend)
    monkeypatch.setattr(
        "usb_tool.backend.windows._derive_media_type_from_drive_letters",
        lambda drive_letters, fallback="Basic Disk": "Basic Disk",
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1410",
                    "iSerial": "SER1410",
                    "driveSizeGB": 16,
                    "mediaType": "Basic Disk",
                    "driveLetter": "E:",
                    "fileSystem": "RAW",
                }
            }
        ]
    }

    devices = backend._native_payload_to_devices(payload)

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    assert serialized["fileSystem"] == "RAW"


def test_native_payload_to_devices_normalizes_raw_without_drive_letter_to_unallocated(
    monkeypatch,
):
    backend = object.__new__(WindowsBackend)
    monkeypatch.setattr(
        "usb_tool.backend.windows._derive_media_type_from_drive_letters",
        lambda drive_letters, fallback="Basic Disk": "Basic Disk",
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1410",
                    "iSerial": "SER1410",
                    "driveSizeGB": 16,
                    "mediaType": "Basic Disk",
                    "driveLetter": "Not Formatted",
                    "fileSystem": "RAW",
                }
            }
        ]
    }

    devices = backend._native_payload_to_devices(payload)

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    assert serialized["fileSystem"] == "Unallocated"


def test_native_payload_to_devices_accepts_unallocated_filesystem_from_native_payload(
    monkeypatch,
):
    backend = object.__new__(WindowsBackend)
    monkeypatch.setattr(
        "usb_tool.backend.windows._derive_media_type_from_drive_letters",
        lambda drive_letters, fallback="Basic Disk": "Basic Disk",
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "iSerial": "SER1407",
                    "driveSizeGB": 16,
                    "mediaType": "Basic Disk",
                    "driveLetter": "Not Formatted",
                    "fileSystem": "Unallocated",
                }
            }
        ]
    }

    devices = backend._native_payload_to_devices(payload)

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    assert serialized["fileSystem"] == "Unallocated"


def test_scan_devices_native_invokes_python_version_probe_only_for_na_drive_size():
    backend = object.__new__(WindowsBackend)
    backend._native_scan_binary = "windows_native_scan.exe"
    backend._native_scan_path_for_run = None
    backend._timed_populate_device_version = MagicMock(
        return_value={
            "scbPartNumber": "SCB-1",
            "hardwareVersion": "HW-1",
            "modelID": "MODEL-1",
            "mcuFW": "1.2.3",
            "bridgeFW": "0502",
            "_profile_ms": 8.5,
        }
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdDevice": "0502",
                    "iSerial": "SER123",
                    "driveSizeGB": "N/A",
                    "physicalDriveNum": 7,
                },
                "2": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdDevice": "0502",
                    "iSerial": "SER456",
                    "driveSizeGB": 16,
                    "physicalDriveNum": 8,
                },
            }
        ]
    }
    native_result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("usb_tool.backend.windows.subprocess.run", return_value=native_result):
        devices = backend._scan_devices_native(profile_scan=False)

    assert devices is not None
    assert len(devices) == 2
    backend._timed_populate_device_version.assert_called_once_with(
        "0984",
        "1407",
        "SER123",
        7,
    )


def test_scan_devices_native_attaches_version_fields_from_python_probe():
    backend = object.__new__(WindowsBackend)
    backend._native_scan_binary = "windows_native_scan.exe"
    backend._native_scan_path_for_run = None
    backend._timed_populate_device_version = MagicMock(
        return_value={
            "scbPartNumber": "SCB-123",
            "hardwareVersion": "HW-2",
            "modelID": "MODEL-2",
            "mcuFW": "2.3.4",
            "bridgeFW": "0502",
            "_profile_ms": 4.0,
            "_profile_open_error": "ignored",
            "_profile_payload_len": 64,
        }
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdDevice": "0502",
                    "iSerial": "SER999",
                    "driveSizeGB": "N/A",
                    "physicalDriveNum": 3,
                }
            }
        ]
    }
    native_result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("usb_tool.backend.windows.subprocess.run", return_value=native_result):
        devices = backend._scan_devices_native(profile_scan=False)

    assert devices is not None
    assert len(devices) == 1
    native_device = devices[0]
    assert native_device.scbPartNumber == "SCB-123"
    assert native_device.hardwareVersion == "HW-2"
    assert native_device.modelID == "MODEL-2"
    assert native_device.mcuFW == "2.3.4"
    assert native_device.bridgeFW == "0502"
    assert not hasattr(native_device, "_profile_open_error")
    assert not hasattr(native_device, "_profile_payload_len")


def test_scan_devices_native_profile_logs_populate_device_version_total(capsys):
    backend = object.__new__(WindowsBackend)
    backend._native_scan_binary = "windows_native_scan.exe"
    backend._native_scan_path_for_run = None
    backend._timed_populate_device_version = MagicMock(
        return_value={
            "scbPartNumber": "SCB-777",
            "bridgeFW": "0502",
            "_profile_ms": 12.34,
            "_profile_create_file_ms": 10.0,
            "_profile_device_io_control_ms": 1.5,
            "_profile_parse_payload_ms": 0.25,
        }
    )
    payload = {
        "devices": [
            {
                "1": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdDevice": "0502",
                    "iSerial": "SER777",
                    "driveSizeGB": "N/A",
                    "physicalDriveNum": 2,
                }
            }
        ],
        "profile": {"totalMs": 1.1, "enumerationMs": 0.4, "driveLettersMs": 0.2},
    }
    native_result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("usb_tool.backend.windows.subprocess.run", return_value=native_result):
        backend._scan_devices_native(profile_scan=True)

    captured = capsys.readouterr()
    assert "windows-native-scan-profile:" in captured.err
    assert '\n  "device_count": 1' in captured.err
    profile_json = _extract_profile_json(captured.err, "windows-native-scan-profile")
    assert profile_json["device_count"] == 1
    version_totals = profile_json["populate_device_version_total"]
    assert version_totals["total_ms"] == 12.34
    assert version_totals["created_file_ms"] == 10.0
    assert version_totals["device_io_control_ms"] == 1.5
    assert version_totals["parse_payload_ms"] == 0.25
    assert "created_file_ms" not in profile_json
    assert "device_io_control_ms" not in profile_json
    assert "parse_payload_ms" not in profile_json
    assert profile_json["total_ms"] >= 12.34
    assert "helper" not in profile_json


def test_timed_populate_device_version_tracks_profile_metrics_without_emitting_log(
    capsys,
):
    backend = object.__new__(WindowsBackend)
    backend._profile_scan_enabled = True
    backend._scan_pass_index = 1

    def _fake_populate(
        vendor_id,
        product_id,
        serial_number,
        bsd_name=None,
        physical_drive_num=None,
        device_path=None,
        profile=None,
    ):
        if profile is not None:
            profile.update(
                {
                    "create_file_ms": 10.0,
                    "device_io_control_ms": 1.5,
                    "parse_payload_ms": 0.2,
                }
            )
        return {"scbPartNumber": "21-0010"}

    with patch("usb_tool.backend.windows.populate_device_version", side_effect=_fake_populate):
        result = backend._timed_populate_device_version("0984", "1407", "SERX", 2)

    captured = capsys.readouterr()
    assert "_profile_ms" in result
    assert result["_profile_create_file_ms"] == 10.0
    assert result["_profile_device_io_control_ms"] == 1.5
    assert result["_profile_parse_payload_ms"] == 0.2
    assert "windows-version-profile:" not in captured.err


def test_scan_devices_prefers_native_when_enabled():
    fake_native_device = SimpleNamespace(physicalDriveNum=1)
    with patch("usb_tool.backend.windows.win32com.client.Dispatch"):
        backend = WindowsBackend()

    backend._native_scan_enabled = True
    backend._scan_devices_native = MagicMock(return_value=[fake_native_device])
    backend._perform_scan_pass = MagicMock(return_value=([], [0, 0, 0]))

    devices = backend.scan_devices()

    assert devices == [fake_native_device]
    backend._scan_devices_native.assert_called_once()
    backend._perform_scan_pass.assert_not_called()


def test_scan_devices_falls_back_to_wmi_when_native_returns_none():
    fake_wmi_device = SimpleNamespace(physicalDriveNum=1)
    with patch("usb_tool.backend.windows.win32com.client.Dispatch"):
        backend = WindowsBackend()

    backend._native_scan_enabled = True
    backend._scan_devices_native = MagicMock(return_value=None)
    backend._perform_scan_pass = MagicMock(return_value=([fake_wmi_device], [1, 1, 1]))

    devices = backend.scan_devices()

    assert devices == [fake_wmi_device]
    backend._scan_devices_native.assert_called_once()
    backend._perform_scan_pass.assert_called_once()
