# Epson TM-T20II USB access for CamUI

CamUI prints photobooth photos with `python-escpos` over USB. The `pi` user must be able to open the printer device.

## Install packages

```bash
sudo apt-get install -y python3-usb
pip3 install --break-system-packages python-escpos
```

## Install udev rule

```bash
chmod +x /home/pi/CamUI/packaging/udev/install-udev-rule.sh
/home/pi/CamUI/packaging/udev/install-udev-rule.sh
```

Or manually:

```bash
sudo cp /home/pi/CamUI/packaging/udev/99-epson-tm-t20ii.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug and replug the printer (or reboot), then confirm:

```bash
lsusb -d 04b8:0e15
python3 -c "from thermal_print import is_printer_available; print(is_printer_available())"
```

`pi` is already in the `plugdev` group on Raspberry Pi OS.

## Enable in CamUI

System Settings → **Photobooth Print** → enable auto-print and set the brand title.
