from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_linux_debian_package_identity_stays_stable_for_upgrades():
    control = _read("installers/linux/debian/DEBIAN/control")
    build_script = _read("build/build_linux_installer.sh")

    assert "Package: usb-tool" in control
    assert 'mkdir -p "$STAGING_ROOT/usr/local/lib/apricorn-usb-toolkit"' in build_script
    assert (
        'install -m 755 "$binary_path" "$STAGING_ROOT/usr/local/lib/apricorn-usb-toolkit/usb"'
    ) in build_script


def test_macos_pkg_component_ids_stay_stable_for_upgrades():
    build_script = _read("build/build_macos_pkg.sh")
    distribution = _read("installers/macos/distribution.xml")

    for package_id in ("com.apricorn.usbtool.base", "com.apricorn.usbtool.nopasswd"):
        assert f'--identifier "{package_id}"' in build_script
        assert f'<pkg-ref id="{package_id}"' in distribution

    assert 'mkdir -p "$STAGING_ROOT/usr/local/lib/apricorn-usb-toolkit"' in build_script
    assert (
        'install -m 755 "$selected_binary" "$STAGING_ROOT/usr/local/lib/apricorn-usb-toolkit/usb"'
    ) in build_script
