# tests/test_mac_usb.py

import sys

import pytest

# Skip this entire module if not on macOS
if sys.platform != "darwin":
    pytest.skip("macOS only tests", allow_module_level=True)

import json
from types import SimpleNamespace
from unittest.mock import patch

from usb_tool.backend.macos import MacOSBackend


def test_list_usb_drives_filters_apricorn_devices():
    profiler_data = {
        "SPUSBDataType": [
            {
                "_name": "Root",
                "host_controller": "Controller A",
                "_items": [
                    {
                        "manufacturer": "Apricorn",
                        "vendor_id": "0x0000",
                        "serial_num": "XYZ",
                    },
                    {
                        "manufacturer": "Generic",
                        "vendor_id": "0x0984",
                        "serial_num": "123",
                    },
                ],
            },
            {
                "_name": "Root",
                "_items": [
                    {
                        "manufacturer": "Other",
                        "vendor_id": "0x1111",
                        "serial_num": "ABC",
                    }
                ],
            },
        ]
    }
    mock_result = SimpleNamespace(returncode=0, stdout=json.dumps(profiler_data))

    with patch("subprocess.run", return_value=mock_result):
        backend = MacOSBackend()
        drives = backend.list_usb_drives()

    assert len(drives) == 2
    assert drives[0]["host_controller"] == "Controller A"
    assert drives[1]["host_controller"] == "Controller A"


def test_parse_uasp_info_builds_boolean_map():
    ioreg_out = (
        "+-o IOUSBMassStorageDriverNub  <class IOUSBMassStorageDriverNub>\n"
        "  | {\n"
        '  |   "IOClass" = "IOUSBMassStorageDriverNub"\n'
        '  |   "bInterfaceClass" = 8\n'
        '  |   "bInterfaceSubClass" = 6\n'
        '  |   "bInterfaceProtocol" = 98\n'
        '  |   "USB Device Info" = {"kUSBSerialNumberString"="SER123",'
        '"USB Product Name"="Drive One","bInterfaceProtocol"=98,'
        '"bInterfaceSubClass"=6,"bInterfaceClass"=8}\n'
        "  | }\n"
    )

    def mock_subprocess_run(cmd, **kwargs):
        if cmd[:3] == ["ioreg", "-r", "-c"]:
            return SimpleNamespace(returncode=0, stdout=ioreg_out)
        return SimpleNamespace(returncode=1, stdout="")

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        backend = MacOSBackend()
        uas_dict = backend.parse_uasp_info([])

    assert "Drive One" in uas_dict
    assert uas_dict["Drive One"] is True
    assert uas_dict["SER123"] is True


def test_find_apricorn_device_skips_excluded_pids():
    drives = [
        {
            "_name": "Bad Apricorn",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x0221",
            "serial_num": "BAD1",
            "Media": [{"size_in_bytes": 100 * 1024**3, "bsd_name": "disk3s1"}],
        },
        {
            "_name": "Good Apricorn",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1234",
            "serial_num": "GOOD1",
            "Media": [{"size_in_bytes": 100 * 1024**3, "bsd_name": "disk4s1"}],
        },
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert result and len(result) == 1
    assert result[0].idProduct == "1234"


def test_scan_devices_populates_driver_transport():
    drives = [
        {
            "_name": "Good Apricorn",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1234",
            "serial_num": "GOOD1",
            "bus_power": "900",
            "bcd_device": "3.00",
            "Media": [{"size_in_bytes": 100 * 1024**3, "bsd_name": "disk4s1"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(
            MacOSBackend,
            "_get_mass_storage_info_map",
            return_value={"GOOD1": {"driverTransport": "UAS"}},
        ),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert len(result) == 1
    serialized = result[0].to_dict()
    assert serialized["driverTransport"] == "UAS"


def test_scan_devices_populates_usb_controller():
    drives = [
        {
            "_name": "Secure Key 3.0",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1407",
            "serial_num": "GOOD1",
            "bcd_device": "4.63",
            "host_controller": "AppleT8103USBXHCI",
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_get_mass_storage_info_map", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert len(result) == 1
    assert result[0].to_dict()["usbController"] == "AppleT8103USBXHCI"


def test_scan_devices_populates_read_only_from_mass_storage_info():
    drives = [
        {
            "_name": "Secure Key 3.0",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1407",
            "serial_num": "147250002822",
            "bcd_device": "4.63",
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(
            MacOSBackend,
            "_get_mass_storage_info_map",
            return_value={
                "147250002822": {
                    "driverTransport": "UAS",
                    "readOnly": True,
                    "blockDevice": "/dev/disk4",
                }
            },
        ),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert len(result) == 1
    serialized = result[0].to_dict()
    assert serialized["readOnly"] is True
    assert serialized["blockDevice"] == "/dev/disk4"


def test_get_mass_storage_info_map_parses_transport_and_read_only():
    ioreg_out = (
        "+-o IOUSBMassStorageDriverNub  <class IOUSBMassStorageDriverNub>\n"
        "  | {\n"
        '  |   "IOClass" = "IOUSBMassStorageDriverNub"\n'
        '  |   "bInterfaceClass" = 8\n'
        '  |   "bInterfaceSubClass" = 6\n'
        '  |   "bInterfaceProtocol" = 98\n'
        '  |   "USB Device Info" = {"kUSBSerialNumberString"="147250002822",'
        '"USB Product Name"="Secure Key 3.0","bInterfaceProtocol"=98,'
        '"bInterfaceSubClass"=6,"bInterfaceClass"=8}\n'
        "  | }\n"
        "  |\n"
        "  +-o Apricorn Secure Key 3.0 Media  <class IOMedia>\n"
        "    | {\n"
        '    |   "BSD Name" = "disk4"\n'
        '    |   "Writable" = No\n'
        "    | }\n"
    )

    with patch(
        "usb_tool.backend.macos.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout=ioreg_out),
    ):
        backend = MacOSBackend()
        storage_info_map = backend._get_mass_storage_info_map()

    assert storage_info_map["147250002822"]["driverTransport"] == "UAS"
    assert storage_info_map["Secure Key 3.0"]["driverTransport"] == "UAS"
    assert storage_info_map["147250002822"]["readOnly"] is True
    assert storage_info_map["147250002822"]["blockDevice"] == "/dev/disk4"


def test_scan_devices_normalizes_partition_to_whole_disk_path():
    drives = [
        {
            "_name": "Good Apricorn",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1234",
            "serial_num": "GOOD1",
            "bcd_device": "3.00",
            "Media": [{"size_in_bytes": 100 * 1024**3, "bsd_name": "disk4s1"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert len(result) == 1
    assert result[0].to_dict()["blockDevice"] == "/dev/disk4"


def test_scan_devices_treats_500kb_media_size_as_oob():
    drives = [
        {
            "_name": "Aegis Padlock 3.0",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x0310",
            "serial_num": "GOOD1",
            "bcd_device": "3.00",
            "Media": [{"size_in_bytes": 500 * 1024, "bsd_name": "disk4"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        result = MacOSBackend().scan_devices()

    assert len(result) == 1
    assert result[0].driveSizeGB == "N/A (OOB Mode)"


def test_scan_devices_treats_zero_media_size_as_oob():
    drives = [
        {
            "_name": "Fortress L3",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1408",
            "serial_num": "GOOD1",
            "bcd_device": "9.02",
            "Media": [{"size_in_bytes": 0, "bsd_name": "disk5"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        result = MacOSBackend().scan_devices()

    assert len(result) == 1
    assert result[0].driveSizeGB == "N/A (OOB Mode)"


def test_scan_devices_treats_missing_media_size_as_oob():
    drives = [
        {
            "_name": "Fortress L3",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1408",
            "serial_num": "GOOD1",
            "bcd_device": "9.02",
            "Media": [{"bsd_name": "disk5"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        result = MacOSBackend().scan_devices()

    assert len(result) == 1
    assert result[0].driveSizeGB == "N/A (OOB Mode)"


def test_scan_devices_treats_formatted_500kb_media_size_as_oob():
    drives = [
        {
            "_name": "Aegis Padlock 3.0",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x0310",
            "serial_num": "GOOD1",
            "bcd_device": "3.00",
            "Media": [{"size": "500 KB", "bsd_name": "disk4"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        result = MacOSBackend().scan_devices()

    assert len(result) == 1
    assert result[0].driveSizeGB == "N/A (OOB Mode)"


def test_scan_devices_treats_parenthesized_500kb_media_size_as_oob():
    drives = [
        {
            "_name": "Aegis Padlock 3.0",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x0310",
            "serial_num": "GOOD1",
            "bcd_device": "3.00",
            "Media": [{"size": "500 KB (500,000 bytes)", "bsd_name": "disk4"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        result = MacOSBackend().scan_devices()

    assert len(result) == 1
    assert result[0].driveSizeGB == "N/A (OOB Mode)"


def test_scan_devices_uses_diskutil_media_type_when_profiler_omits_it():
    drives = [
        {
            "_name": "Good Apricorn",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1234",
            "serial_num": "GOOD1",
            "bcd_device": "3.00",
            "Media": [{"size_in_bytes": 100 * 1024**3, "bsd_name": "disk4"}],
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch.object(MacOSBackend, "_get_media_type_from_diskutil", return_value="Basic Disk"),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert len(result) == 1
    assert result[0].to_dict()["mediaType"] == "Basic Disk"


def test_scan_devices_falls_back_to_known_product_media_type():
    drives = [
        {
            "_name": "Secure Key 3.0",
            "manufacturer": "Apricorn",
            "vendor_id": "0x0984",
            "product_id": "0x1407",
            "serial_num": "GOOD1",
            "bcd_device": "4.63",
        }
    ]

    with (
        patch.object(MacOSBackend, "_list_usb_drives", return_value=drives),
        patch.object(MacOSBackend, "_parse_uasp_info", return_value={}),
        patch("usb_tool.backend.macos.populate_device_version", return_value={}),
    ):
        backend = MacOSBackend()
        result = backend.scan_devices()

    assert len(result) == 1
    assert result[0].to_dict()["mediaType"] == "Basic Disk"


def test_poke_device_raises_not_supported():
    backend = MacOSBackend()
    with pytest.raises(RuntimeError, match="not currently supported"):
        backend.poke_device("/dev/disk4")
