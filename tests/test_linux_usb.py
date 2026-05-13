"""Unit tests for linux_usb module."""

import sys

import pytest

# Skip this entire module if not on Linux
if sys.platform != "linux":
    pytest.skip("Linux only tests", allow_module_level=True)

from types import SimpleNamespace
from unittest.mock import Mock, patch

from usb_tool.backend.linux import LinuxBackend, _LinuxBlockDeviceProbe


def test_parse_lsblk_size_parses_various_units():
    """parse_lsblk_size must support several unit suffixes."""
    backend = LinuxBackend()
    assert backend.parse_lsblk_size("1G") == 1.0
    assert backend.parse_lsblk_size("1024M") == 1.0
    assert backend.parse_lsblk_size("1T") == 1024.0
    assert backend.parse_lsblk_size("1024K") == 1.0 / 1024
    assert backend.parse_lsblk_size("1E") == 1024**2
    assert backend.parse_lsblk_size("bogus") == 0.0


def test_list_usb_drives_parses_lsblk_output():
    """list_usb_drives should parse lsblk output into structured data."""
    lsblk_output = "/dev/sda SERIAL123 465G 1 0\n/dev/sdb - 14T 0 1\n"
    mock_result = SimpleNamespace(returncode=0, stdout=lsblk_output, stderr="")

    with patch("subprocess.run", return_value=mock_result):
        backend = LinuxBackend()
        drives = backend.list_usb_drives()

    assert drives[0]["name"] == "/dev/sda"
    assert drives[0]["serial"] == "SERIAL123"
    assert drives[0]["mediaType"] == "Removable Media"
    assert drives[0]["readOnly"] is False
    assert drives[1]["serial"] == ""
    assert drives[1]["mediaType"] == "Basic Disk"
    assert drives[1]["size_gb"] == 14 * 1024
    assert drives[1]["readOnly"] is True


def test_probe_block_device_context_prefers_sysfs_when_available():
    backend = LinuxBackend()
    udev_mock = Mock(return_value={})

    with (
        patch.object(
            LinuxBackend,
            "_get_block_device_sysfs_path",
            return_value="/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0/host0/target0:0:0/0:0:0:0/block/sdb",
        ),
        patch.object(LinuxBackend, "_find_usb_driver_name_in_sysfs", return_value="uas"),
        patch.object(LinuxBackend, "_find_usb_serial_in_sysfs", return_value="SERIAL123"),
        patch.object(LinuxBackend, "_get_udev_info", udev_mock),
    ):
        probe = backend._probe_block_device_context("/dev/sdb", {"serial": ""})

    assert probe.serial == "SERIAL123"
    assert probe.driver_name == "uas"
    assert probe.driver_transport == "UAS"
    assert probe.pci_addr == "0000:00:14.0"
    udev_mock.assert_not_called()


def test_probe_block_device_context_uses_udev_fallbacks():
    backend = LinuxBackend()

    with (
        patch.object(
            LinuxBackend,
            "_get_block_device_sysfs_path",
            return_value="/sys/devices/virtual/block/sdb",
        ),
        patch.object(LinuxBackend, "_find_usb_driver_name_in_sysfs", return_value=""),
        patch.object(LinuxBackend, "_find_usb_serial_in_sysfs", return_value=""),
        patch.object(
            LinuxBackend,
            "_get_udev_info",
            return_value={
                "ID_USB_DRIVER": "usb-storage",
                "ID_PATH": "pci-0000:00:1d.0-usb-0:4:1.0-scsi-0:0:0:0",
                "ID_SERIAL_SHORT": "SERIAL456",
            },
        ),
    ):
        probe = backend._probe_block_device_context("/dev/sdb", {"serial": ""})

    assert probe.serial == "SERIAL456"
    assert probe.driver_name == "usb-storage"
    assert probe.driver_transport == "BOT"
    assert probe.pci_addr == "0000:00:1d.0"


def test_scan_devices_populates_driver_transport_from_probe_result():
    with (
        patch.object(
            LinuxBackend,
            "_list_usb_drives",
            return_value=[
                {
                    "name": "/dev/sdb",
                    "serial": "SERIAL123",
                    "size_gb": 64.0,
                    "mediaType": "Removable Media",
                    "readOnly": False,
                }
            ],
        ),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sdb": _LinuxBlockDeviceProbe(
                    block_device="/dev/sdb",
                    serial="SERIAL123",
                    driver_name="uas",
                    driver_transport="UAS",
                    pci_addr="0000:00:14.0",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sdb": "Intel"},
        ),
        patch.object(
            LinuxBackend,
            "_get_lsusb_details",
            return_value={
                "SERIAL123": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdUSB": "3.0",
                    "bcdDevice": "0300",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Secure Key 3.0",
                }
            },
        ),
        patch("usb_tool.backend.linux.populate_device_version", return_value={}),
    ):
        backend = LinuxBackend()
        devices = backend.scan_devices()

    assert len(devices) == 1
    serialized = devices[0].to_dict()
    assert serialized["driverTransport"] == "UAS"
    assert serialized["usbController"] == "Intel"
    assert serialized["readOnly"] is False


def test_scan_devices_treats_500k_media_size_as_oob():
    backend = LinuxBackend()
    with (
        patch.object(
            LinuxBackend,
            "_list_usb_drives",
            return_value=[
                {
                    "name": "/dev/sdb",
                    "serial": "SERIAL123",
                    "size_gb": backend.parse_lsblk_size("500K"),
                    "mediaType": "Basic Disk",
                    "readOnly": False,
                }
            ],
        ),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sdb": _LinuxBlockDeviceProbe(
                    block_device="/dev/sdb",
                    serial="SERIAL123",
                    driver_transport="BOT",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sdb": "Intel"},
        ),
        patch.object(
            LinuxBackend,
            "_get_lsusb_details",
            return_value={
                "SERIAL123": {
                    "idVendor": "0984",
                    "idProduct": "0310",
                    "bcdUSB": "3.0",
                    "bcdDevice": "0300",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Aegis Padlock 3.0",
                }
            },
        ),
        patch("usb_tool.backend.linux.populate_device_version", return_value={}),
    ):
        devices = backend.scan_devices()

    assert len(devices) == 1
    assert devices[0].driveSizeGB == "N/A (OOB Mode)"


def test_scan_devices_uses_sysfs_descriptors_when_lsusb_details_missing():
    backend = LinuxBackend()
    with (
        patch.object(
            LinuxBackend,
            "_list_usb_drives",
            return_value=[
                {
                    "name": "/dev/sdb",
                    "serial": "SERIAL123",
                    "size_gb": backend.parse_lsblk_size("500K"),
                    "mediaType": "Basic Disk",
                    "readOnly": False,
                }
            ],
        ),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sdb": _LinuxBlockDeviceProbe(
                    block_device="/dev/sdb",
                    serial="SERIAL123",
                    driver_transport="BOT",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sdb": "Intel"},
        ),
        patch.object(LinuxBackend, "_get_lsusb_details", return_value={}),
        patch.object(
            LinuxBackend,
            "_get_sysfs_usb_details",
            return_value={
                "idVendor": "0984",
                "idProduct": "1408",
                "bcdUSB": "2.10",
                "bcdDevice": "0902",
                "iManufacturer": "Apricorn",
                "iProduct": "Fortress L3",
                "iSerial": "SERIAL123",
            },
        ) as sysfs_details,
        patch("usb_tool.backend.linux.populate_device_version", return_value={}),
    ):
        devices = backend.scan_devices()

    assert len(devices) == 1
    assert devices[0].idProduct == "1408"
    assert devices[0].driveSizeGB == "N/A (OOB Mode)"
    sysfs_details.assert_called_once_with("/dev/sdb")


def test_scan_devices_uses_probe_serial_when_lsblk_serial_missing():
    with (
        patch.object(
            LinuxBackend,
            "_list_usb_drives",
            return_value=[
                {
                    "name": "/dev/sdb",
                    "serial": "",
                    "size_gb": 64.0,
                    "mediaType": "Removable Media",
                    "readOnly": False,
                }
            ],
        ),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sdb": _LinuxBlockDeviceProbe(
                    block_device="/dev/sdb",
                    serial="SERIAL123",
                    driver_name="usb-storage",
                    driver_transport="BOT",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sdb": "Intel"},
        ),
        patch.object(
            LinuxBackend,
            "_get_lsusb_details",
            return_value={
                "SERIAL123": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdUSB": "3.0",
                    "bcdDevice": "0300",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Secure Key 3.0",
                }
            },
        ),
        patch("usb_tool.backend.linux.populate_device_version", return_value={}),
    ):
        backend = LinuxBackend()
        devices = backend.scan_devices()

    assert len(devices) == 1
    assert devices[0].to_dict()["iSerial"] == "SERIAL123"
    assert devices[0].to_dict()["driverTransport"] == "BOT"


def test_scan_devices_preserves_lsblk_order_after_parallel_probe_join():
    with (
        patch.object(
            LinuxBackend,
            "_list_usb_drives",
            return_value=[
                {
                    "name": "/dev/sdb",
                    "serial": "SERIAL_B",
                    "size_gb": 64.0,
                    "mediaType": "Removable Media",
                    "readOnly": False,
                },
                {
                    "name": "/dev/sda",
                    "serial": "SERIAL_A",
                    "size_gb": 32.0,
                    "mediaType": "Basic Disk",
                    "readOnly": False,
                },
            ],
        ),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sda": _LinuxBlockDeviceProbe(
                    block_device="/dev/sda",
                    serial="SERIAL_A",
                    driver_transport="BOT",
                ),
                "/dev/sdb": _LinuxBlockDeviceProbe(
                    block_device="/dev/sdb",
                    serial="SERIAL_B",
                    driver_transport="UAS",
                ),
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sda": "Intel", "/dev/sdb": "ASMedia"},
        ),
        patch.object(
            LinuxBackend,
            "_get_lsusb_details",
            return_value={
                "SERIAL_A": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdUSB": "3.0",
                    "bcdDevice": "0300",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Secure Key 3.0",
                },
                "SERIAL_B": {
                    "idVendor": "0984",
                    "idProduct": "1410",
                    "bcdUSB": "3.2",
                    "bcdDevice": "0502",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Aegis Padlock 3",
                },
            },
        ),
        patch("usb_tool.backend.linux.populate_device_version", return_value={}),
    ):
        backend = LinuxBackend()
        devices = backend.scan_devices()

    assert [device.to_dict()["blockDevice"] for device in devices] == [
        "/dev/sdb",
        "/dev/sda",
    ]


def test_scan_devices_emits_profile_output_when_enabled(capsys):
    with (
        patch.object(LinuxBackend, "_list_usb_drives", return_value=[]),
        patch.object(LinuxBackend, "_probe_block_devices", return_value={}),
        patch.object(LinuxBackend, "_resolve_probe_controllers", return_value={}),
        patch.object(LinuxBackend, "_get_lsusb_details", return_value={}),
    ):
        backend = LinuxBackend()
        devices = backend.scan_devices(profile_scan=True)

    captured = capsys.readouterr()
    assert devices == []
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "linux-scan-profile details:" in captured.err
    assert "populate_device_version_total=0.00ms" in captured.err
    assert "device_count=0" in captured.err
    assert "linux-scan-profile expanded=false" in captured.err
    assert "device_probe=" in captured.err
    assert "controller_lookup=" in captured.err
    assert "descriptor_lookup=" in captured.err
    assert "device_build=" in captured.err
    assert "total=" in captured.err
    assert "linux-lsblk-profile:" not in captured.err
    assert "linux-udev-profile:" not in captured.err
    assert "linux-lspci-profile:" not in captured.err
    assert "linux-lsusb-profile:" not in captured.err
    assert "linux-lsusb-verbose-profile:" not in captured.err
    assert "linux-version-profile:" not in captured.err


def test_scan_devices_profile_details_precede_summary(capsys):
    with (
        patch.object(
            LinuxBackend,
            "_list_usb_drives",
            return_value=[
                {
                    "name": "/dev/sdb",
                    "serial": "SERIAL123",
                    "size_gb": 64.0,
                    "mediaType": "Removable Media",
                    "readOnly": False,
                }
            ],
        ),
        patch.object(
            LinuxBackend,
            "_probe_block_devices",
            return_value={
                "/dev/sdb": _LinuxBlockDeviceProbe(
                    block_device="/dev/sdb",
                    serial="SERIAL123",
                    driver_transport="UAS",
                    pci_addr="0000:00:14.0",
                )
            },
        ),
        patch.object(
            LinuxBackend,
            "_resolve_probe_controllers",
            return_value={"/dev/sdb": "Intel"},
        ),
        patch.object(
            LinuxBackend,
            "_get_lsusb_details",
            return_value={
                "SERIAL123": {
                    "idVendor": "0984",
                    "idProduct": "1407",
                    "bcdUSB": "3.0",
                    "bcdDevice": "0300",
                    "iManufacturer": "Apricorn",
                    "iProduct": "Secure Key 3.0",
                }
            },
        ),
        patch.object(
            LinuxBackend,
            "_timed_populate_device_version",
            return_value={"_profile_ms": 12.34},
        ),
    ):
        backend = LinuxBackend()
        devices = backend.scan_devices(profile_scan=True)

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(devices) == 1
    assert len(lines) == 2
    assert lines[0].startswith("linux-scan-profile details:")
    assert "populate_device_version_total=12.34ms" in lines[0]
    assert "device_count=1" in lines[0]
    assert lines[1].startswith("linux-scan-profile expanded=false")
    assert "lsblk_drives=1" in lines[1]
    assert "probed_devices=1" in lines[1]
    assert "unique_pci_addrs=1" in lines[1]
    assert "lsusb_devices=1" in lines[1]
    assert "devices=1" in lines[1]
    assert "device_probe=" in lines[1]
    assert "controller_lookup=" in lines[1]
    assert "descriptor_lookup=" in lines[1]


def test_get_udev_info_parses_usb_storage_driver():
    mock_result = SimpleNamespace(
        returncode=0,
        stdout=(
            "E: ID_USB_DRIVER=usb-storage\nE: ID_PATH=pci-0000:00:14.0-usb-0:1:1.0-scsi-0:0:0:0\n"
        ),
        stderr="",
    )

    with patch("usb_tool.backend.linux.subprocess.run", return_value=mock_result):
        backend = LinuxBackend()
        info = backend._get_udev_info("/dev/sda")

    assert info["ID_USB_DRIVER"] == "usb-storage"
    assert info["ID_PATH"] == "pci-0000:00:14.0-usb-0:1:1.0-scsi-0:0:0:0"


def test_parse_udev_properties_extracts_environment_keys_only():
    backend = LinuxBackend()
    info = backend._parse_udev_properties(
        "P: /devices/pci0000:00\nE: ID_USB_DRIVER=uas\nE: ID_SERIAL_SHORT=SERIAL123\n"
    )

    assert info == {
        "ID_USB_DRIVER": "uas",
        "ID_SERIAL_SHORT": "SERIAL123",
    }


def test_extract_serial_from_udev_info_prefers_short_serial_keys():
    backend = LinuxBackend()
    serial = backend._extract_serial_from_udev_info(
        {
            "ID_SERIAL": "Apricorn_Secure_Key_3.0_IGNORED",
            "ID_SERIAL_SHORT": "SERIAL123",
            "ID_SCSI_SERIAL": "SERIAL456",
        }
    )

    assert serial == "SERIAL123"


def test_get_transport_map_classifies_udev_driver():
    backend = LinuxBackend()
    transport_map = backend._get_transport_map(
        {
            "/dev/sda": {"ID_USB_DRIVER": "usb-storage"},
            "/dev/sdb": {"ID_USB_DRIVER": "uas"},
            "/dev/sdc": {},
        }
    )

    assert transport_map == {
        "/dev/sda": "BOT",
        "/dev/sdb": "UAS",
        "/dev/sdc": "Unknown",
    }


def test_get_controller_map_resolves_controller_name_from_pci_address():
    with patch.object(
        LinuxBackend,
        "_get_pci_controller_name",
        return_value="Intel",
    ):
        backend = LinuxBackend()
        controller_map = backend._get_controller_map(
            {"/dev/sda": {"ID_PATH": "pci-0000:00:14.0-usb-0:1:1.0-scsi-0:0:0:0"}}
        )

    assert controller_map == {"/dev/sda": "Intel"}


def test_resolve_probe_controllers_deduplicates_pci_lookups():
    backend = LinuxBackend()
    probe_map = {
        "/dev/sda": _LinuxBlockDeviceProbe(block_device="/dev/sda", pci_addr="0000:00:14.0"),
        "/dev/sdb": _LinuxBlockDeviceProbe(block_device="/dev/sdb", pci_addr="0000:00:14.0"),
        "/dev/sdc": _LinuxBlockDeviceProbe(block_device="/dev/sdc", pci_addr="0000:00:1d.0"),
    }

    with patch.object(
        LinuxBackend,
        "_get_pci_controller_name",
        side_effect=["Intel", "ASMedia"],
    ) as controller_name_mock:
        controller_map = backend._resolve_probe_controllers(probe_map)

    assert controller_name_mock.call_count == 2
    assert controller_map == {
        "/dev/sda": "Intel",
        "/dev/sdb": "Intel",
        "/dev/sdc": "ASMedia",
    }


def test_get_pci_controller_name_returns_manufacturer_only():
    mock_result = SimpleNamespace(
        returncode=0,
        stdout=(
            "00:14.0 USB controller: "
            "Intel Corporation Alder Lake PCH USB 3.2 xHCI Host Controller (rev 01)\n"
        ),
        stderr="",
    )

    with patch("usb_tool.backend.linux.subprocess.run", return_value=mock_result):
        backend = LinuxBackend()
        controller_name = backend._get_pci_controller_name("0000:00:14.0")

    assert controller_name == "Intel"


def test_extract_pci_controller_address_accepts_sysfs_or_udev_style_paths():
    backend = LinuxBackend()

    assert (
        backend._extract_pci_address_from_text("/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1")
        == "0000:00:14.0"
    )
    assert (
        backend._extract_pci_controller_address(
            {"ID_PATH": "pci-0000:00:1d.0-usb-0:4:1.0-scsi-0:0:0:0"}
        )
        == "0000:00:1d.0"
    )


def test_get_transport_map_by_serial_parses_usb_devices_output():
    usb_devices_output = """
T:  Bus=04 Lev=01 Prnt=01 Port=00 Cnt=01 Dev#=  8 Spd=5000 MxCh= 0
S:  Manufacturer=Apricorn
S:  Product=Secure Key 3.0
S:  SerialNumber=000000000001
I:  If#= 0 Alt= 1 #EPs= 4 Cls=08(stor.) Sub=06 Prot=62 Driver=uas

T:  Bus=04 Lev=01 Prnt=01 Port=00 Cnt=01 Dev#=  7 Spd=5000 MxCh= 0
S:  Manufacturer=Apricorn
S:  Product=Fortress
S:  SerialNumber=101300032245
I:  If#= 0 Alt= 0 #EPs= 2 Cls=08(stor.) Sub=06 Prot=50 Driver=usb-storage
"""
    mock_result = SimpleNamespace(returncode=0, stdout=usb_devices_output, stderr="")

    with patch("usb_tool.backend.linux.subprocess.run", return_value=mock_result):
        backend = LinuxBackend()
        transport_map = backend._get_transport_map_by_serial()

    assert transport_map == {
        "000000000001": "UAS",
        "101300032245": "BOT",
    }
