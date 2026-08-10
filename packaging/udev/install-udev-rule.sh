#!/bin/bash
# Install USB access for Epson TM-T20II photobooth printing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RULE_SRC="$ROOT/packaging/udev/99-epson-tm-t20ii.rules"
RULE_DST="/etc/udev/rules.d/99-epson-tm-t20ii.rules"

if [ ! -f "$RULE_SRC" ]; then
  echo "Missing rule file: $RULE_SRC" >&2
  exit 1
fi

sudo cp "$RULE_SRC" "$RULE_DST"
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "Installed $RULE_DST"
echo "Unplug/replug the TM-T20II (or reboot), then re-test printing."
