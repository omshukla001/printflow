#!/bin/bash
# Wait for the kiosk server to be ready
for i in $(seq 1 30); do
    if curl -s http://localhost:5000/kiosk > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Launch Chromium in kiosk mode
exec chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --enable-features=OverlayScrollbar \
    --start-fullscreen \
    --autoplay-policy=no-user-gesture-required \
    --disable-session-crashed-bubble \
    --disable-component-update \
    --password-store=basic \
    --ozone-platform=wayland \
    http://localhost:5000/kiosk
