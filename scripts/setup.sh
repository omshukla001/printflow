#!/bin/bash
# PrintFlow Setup Script
# Detects and configures USB printer in CUPS

set -e

echo "=== PrintFlow Printer Setup ==="
echo ""

# Check CUPS is running
if ! systemctl is-active --quiet cups; then
    echo "Starting CUPS..."
    sudo systemctl start cups
    sudo systemctl enable cups
fi

echo "CUPS is running."
echo ""

# List connected printers
echo "Detecting printers..."
PRINTERS=$(lpinfo -v 2>/dev/null | grep -E "usb://|direct" || true)

if [ -z "$PRINTERS" ]; then
    echo "No USB printers detected."
    echo "Make sure your printer is connected and powered on."
    echo ""
    echo "Available backends:"
    lpinfo -v 2>/dev/null || true
    exit 1
fi

echo "Found printers:"
echo "$PRINTERS"
echo ""

# Get the first USB printer URI
PRINTER_URI=$(echo "$PRINTERS" | grep "usb://" | head -1 | awk '{print $2}')

if [ -z "$PRINTER_URI" ]; then
    echo "No USB printer found. Using first available:"
    PRINTER_URI=$(echo "$PRINTERS" | head -1 | awk '{print $2}')
fi

echo "Using printer: $PRINTER_URI"
echo ""

# Get printer driver
echo "Available drivers for this printer:"
DRIVER=$(lpinfo -m 2>/dev/null | grep -i "$(echo $PRINTER_URI | sed 's|usb://||;s|/.*||')" | head -1 | awk '{print $1}')

if [ -z "$DRIVER" ]; then
    DRIVER="everywhere"
    echo "Using driverless printing (IPP Everywhere)"
fi

echo "Driver: $DRIVER"
echo ""

# Add printer
PRINTER_NAME="PrintFlow"
echo "Adding printer as '$PRINTER_NAME'..."
sudo lpadmin -p "$PRINTER_NAME" -E -v "$PRINTER_URI" -m "$DRIVER" 2>/dev/null || {
    echo "Failed to add with driver, trying driverless..."
    sudo lpadmin -p "$PRINTER_NAME" -E -v "$PRINTER_URI" -m everywhere
}

# Set as default
sudo lpoptions -d "$PRINTER_NAME"
echo "Set '$PRINTER_NAME' as default printer."
echo ""

# Verify
echo "Printer status:"
lpstat -p "$PRINTER_NAME" 2>/dev/null || true
echo ""

# Offer test page
read -p "Print a test page? (y/n): " TEST
if [ "$TEST" = "y" ]; then
    echo "Printing test page..."
    echo "PrintFlow Test Page - $(date)" | lp -d "$PRINTER_NAME"
    echo "Test page sent."
fi

echo ""
echo "=== Setup Complete ==="
echo "Printer name: $PRINTER_NAME"
echo "Set DEFAULT_PRINTER=$PRINTER_NAME in your environment if needed."
