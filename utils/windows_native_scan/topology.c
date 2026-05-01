#include "topology.h"

#include "devnode.h"

static const GUID GUID_DEVINTERFACE_USB_HUB_LOCAL = {0xf18a0e88,
                                                     0xc30c,
                                                     0x11d0,
                                                     {0x88, 0x15, 0x00, 0xa0, 0xc9, 0x06, 0xbe, 0xd8}};

static bool parse_port_from_location_info(const wchar_t* location_info, int* port_out) {
    const wchar_t* p;
    int port = 0;
    if (location_info == NULL) {
        return false;
    }
    p = wcsstr(location_info, L"Port_#");
    if (p == NULL) {
        return false;
    }
    p += 6;
    while (*p >= L'0' && *p <= L'9') {
        port = (port * 10) + (int)(*p - L'0');
        ++p;
    }
    if (port <= 0) {
        return false;
    }
    *port_out = port;
    return true;
}

static bool get_hub_interface_path_for_devinst(DEVINST hub_devinst,
                                               wchar_t* path_out,
                                               DWORD path_out_cch) {
    HDEVINFO hubs;
    SP_DEVICE_INTERFACE_DATA if_data;
    DWORD index = 0;
    wchar_t wanted_id[1024];

    if (CM_Get_Device_IDW(hub_devinst, wanted_id, ARRAYSIZE(wanted_id), 0) != CR_SUCCESS) {
        return false;
    }

    hubs = SetupDiGetClassDevsW(
        &GUID_DEVINTERFACE_USB_HUB_LOCAL, NULL, NULL, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (hubs == INVALID_HANDLE_VALUE) {
        return false;
    }

    ZeroMemory(&if_data, sizeof(if_data));
    if_data.cbSize = sizeof(if_data);

    while (SetupDiEnumDeviceInterfaces(hubs, NULL, &GUID_DEVINTERFACE_USB_HUB_LOCAL, index, &if_data)) {
        DWORD required = 0;
        PSP_DEVICE_INTERFACE_DETAIL_DATA_W detail = NULL;
        SP_DEVINFO_DATA devinfo;
        wchar_t instance_id[1024];
        BOOL ok = FALSE;

        ZeroMemory(&devinfo, sizeof(devinfo));
        devinfo.cbSize = sizeof(devinfo);
        SetupDiGetDeviceInterfaceDetailW(hubs, &if_data, NULL, 0, &required, &devinfo);
        detail = (PSP_DEVICE_INTERFACE_DETAIL_DATA_W)malloc(required);
        if (detail == NULL) {
            break;
        }
        detail->cbSize = sizeof(*detail);
        ok = SetupDiGetDeviceInterfaceDetailW(hubs, &if_data, detail, required, NULL, &devinfo);
        if (!ok) {
            free(detail);
            ++index;
            continue;
        }

        ZeroMemory(instance_id, sizeof(instance_id));
        if (SetupDiGetDeviceInstanceIdW(hubs, &devinfo, instance_id, ARRAYSIZE(instance_id), NULL) &&
            _wcsicmp(instance_id, wanted_id) == 0) {
            StringCchCopyW(path_out, path_out_cch, detail->DevicePath);
            free(detail);
            SetupDiDestroyDeviceInfoList(hubs);
            return true;
        }

        free(detail);
        ++index;
    }

    SetupDiDestroyDeviceInfoList(hubs);
    return false;
}

static double usb_bcd_to_float(USHORT bcd_usb) {
    int major = (bcd_usb >> 8) & 0xFF;
    int minor = (bcd_usb >> 4) & 0x0F;
    return (double)major + ((double)minor / 10.0);
}

static void classify_pci_usb_controller_vendor(const wchar_t* instance_id, char out[32]) {
    const wchar_t* vendor = NULL;
    if (instance_id == NULL) {
        return;
    }

    vendor = wcsstr(instance_id, L"PCI\\VEN_");
    if (vendor == NULL) {
        return;
    }
    vendor += 8;

    if (_wcsnicmp(vendor, L"8086", 4) == 0) {
        StringCchCopyA(out, 32, "Intel");
    } else if (_wcsnicmp(vendor, L"1B21", 4) == 0) {
        StringCchCopyA(out, 32, "ASMedia");
    } else if (_wcsnicmp(vendor, L"1912", 4) == 0 || _wcsnicmp(vendor, L"1033", 4) == 0) {
        StringCchCopyA(out, 32, "Renesas");
    } else if (_wcsnicmp(vendor, L"1022", 4) == 0) {
        StringCchCopyA(out, 32, "AMD");
    } else if (_wcsnicmp(vendor, L"1B73", 4) == 0) {
        StringCchCopyA(out, 32, "Fresco Logic");
    } else if (_wcsnicmp(vendor, L"1106", 4) == 0) {
        StringCchCopyA(out, 32, "VIA");
    } else if (_wcsnicmp(vendor, L"10DE", 4) == 0) {
        StringCchCopyA(out, 32, "NVIDIA");
    } else if (_wcsnicmp(vendor, L"104C", 4) == 0) {
        StringCchCopyA(out, 32, "Texas Instruments");
    } else if (_wcsnicmp(vendor, L"106B", 4) == 0) {
        StringCchCopyA(out, 32, "Apple");
    }
}

void classify_transport(const wchar_t* instance_id, const wchar_t* service, char out[16]) {
    out[0] = '\0';
    if (instance_id != NULL) {
        if (_wcsnicmp(instance_id, L"SCSI\\", 5) == 0) {
            StringCchCopyA(out, 16, "UAS");
            return;
        }
        if (_wcsnicmp(instance_id, L"USBSTOR\\", 8) == 0) {
            StringCchCopyA(out, 16, "BOT");
            return;
        }
    }
    if (service != NULL) {
        if (_wcsicmp(service, L"uaspstor") == 0 || _wcsicmp(service, L"uaspstor.sys") == 0) {
            StringCchCopyA(out, 16, "UAS");
            return;
        }
        if (_wcsicmp(service, L"USBSTOR") == 0 || _wcsicmp(service, L"usbstor.sys") == 0) {
            StringCchCopyA(out, 16, "BOT");
            return;
        }
    }
    StringCchCopyA(out, 16, "Unknown");
}

bool query_connection_metrics_from_parent_hub(DEVINST usb_devinst,
                                              const char* expected_vid,
                                              const char* expected_pid,
                                              double* bcd_usb_out,
                                              int* device_address_out,
                                              char bcd_device_out[5]) {
    DEVINST parent = 0;
    BYTE* location_buf = NULL;
    ULONG location_type = 0;
    ULONG location_size = 0;
    wchar_t hub_path[1024];
    HANDLE h_hub = INVALID_HANDLE_VALUE;
    USB_NODE_CONNECTION_INFORMATION_EX conn;
    DWORD out_bytes = 0;
    int port = 0;
    bool ok = false;
    char vid[5];
    char pid[5];
    char bcd[5];

    if (CM_Get_Parent(&parent, usb_devinst, 0) != CR_SUCCESS) {
        return false;
    }
    if (!get_devnode_reg_property(
            usb_devinst, CM_DRP_LOCATION_INFORMATION, &location_type, &location_buf, &location_size)) {
        return false;
    }
    if (location_type != REG_SZ && location_type != REG_MULTI_SZ) {
        free(location_buf);
        return false;
    }
    if (!parse_port_from_location_info((const wchar_t*)location_buf, &port)) {
        free(location_buf);
        return false;
    }
    free(location_buf);

    if (!get_hub_interface_path_for_devinst(parent, hub_path, ARRAYSIZE(hub_path))) {
        return false;
    }

    h_hub = CreateFileW(
        hub_path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (h_hub == INVALID_HANDLE_VALUE) {
        h_hub = CreateFileW(hub_path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    }
    if (h_hub == INVALID_HANDLE_VALUE) {
        return false;
    }

    ZeroMemory(&conn, sizeof(conn));
    conn.ConnectionIndex = (ULONG)port;
    if (!DeviceIoControl(h_hub,
                         IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX,
                         &conn,
                         (DWORD)sizeof(conn),
                         &conn,
                         (DWORD)sizeof(conn),
                         &out_bytes,
                         NULL)) {
        CloseHandle(h_hub);
        return false;
    }
    CloseHandle(h_hub);

    StringCchPrintfA(vid, ARRAYSIZE(vid), "%04X", conn.DeviceDescriptor.idVendor);
    StringCchPrintfA(pid, ARRAYSIZE(pid), "%04X", conn.DeviceDescriptor.idProduct);
    StringCchPrintfA(bcd, ARRAYSIZE(bcd), "%04X", conn.DeviceDescriptor.bcdDevice);
    if (_stricmp(expected_vid, vid) != 0 || _stricmp(expected_pid, pid) != 0) {
        return false;
    }

    *bcd_usb_out = usb_bcd_to_float(conn.DeviceDescriptor.bcdUSB);
    *device_address_out = (int)conn.DeviceAddress;
    if (bcd_device_out != NULL) {
        StringCchCopyA(bcd_device_out, 5, bcd);
    }
    ok = true;
    return ok;
}

void classify_usb_controller(DEVINST usb_devinst, char out[32]) {
    DEVINST cur = usb_devinst;
    int depth = 0;
    wchar_t instance_id[1024];
    StringCchCopyA(out, 32, "N/A");
    for (depth = 0; depth < 20; ++depth) {
        DEVINST parent = 0;
        if (CM_Get_Device_IDW(cur, instance_id, ARRAYSIZE(instance_id), 0) != CR_SUCCESS) {
            break;
        }
        if (wcsstr(instance_id, L"PCI\\VEN_") != NULL) {
            classify_pci_usb_controller_vendor(instance_id, out);
            return;
        }
        if (CM_Get_Parent(&parent, cur, 0) != CR_SUCCESS) {
            break;
        }
        cur = parent;
    }
}

bool find_usb_identity(DEVINST start_devinst,
                       char vid_out[5],
                       char pid_out[5],
                       char bcd_out[5],
                       char serial_out[128],
                       DEVINST* usb_devinst_out) {
    DEVINST cur = start_devinst;
    int depth = 0;
    wchar_t instance_id[1024];

    vid_out[0] = '\0';
    pid_out[0] = '\0';
    bcd_out[0] = '\0';
    serial_out[0] = '\0';
    if (usb_devinst_out != NULL) {
        *usb_devinst_out = 0;
    }

    for (depth = 0; depth < 10; ++depth) {
        CONFIGRET cr = CM_Get_Device_IDW(cur, instance_id, ARRAYSIZE(instance_id), 0);
        DEVINST parent = 0;
        if (cr != CR_SUCCESS) {
            break;
        }

        if (serial_out[0] == '\0') {
            extract_serial_segment(instance_id, serial_out);
        }
        if (parse_hex4_after_token(instance_id, L"VID_", vid_out) &&
            parse_hex4_after_token(instance_id, L"PID_", pid_out)) {
            if (!parse_hex4_after_token(instance_id, L"REV_", bcd_out)) {
                parse_rev_from_hardware_ids(cur, bcd_out);
            }
            if (bcd_out[0] == '\0') {
                StringCchCopyA(bcd_out, 5, "0000");
            }
            if (usb_devinst_out != NULL) {
                *usb_devinst_out = cur;
            }
            return _stricmp(vid_out, "0984") == 0;
        }

        cr = CM_Get_Parent(&parent, cur, 0);
        if (cr != CR_SUCCESS) {
            break;
        }
        cur = parent;
    }

    return false;
}
