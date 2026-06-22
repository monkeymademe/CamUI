# CamUI for PiCamera2 - Docker
# Must be built and run on a Raspberry Pi (ARM) with camera connected.
#
# Build:
#   docker build -t camui .
#
# Run (camera access requires --privileged or explicit device mapping):
#   docker run --rm -it --privileged \
#     -p 8080:8080 \
#     -v /run/udev:/run/udev:ro \
#     -v camui-gallery:/app/static/gallery \
#     camui
#

FROM debian:bookworm

# Add Raspberry Pi apt repository for libcamera / picamera2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg curl ca-certificates \
    && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
       | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] http://archive.raspberrypi.com/debian/ bookworm main" \
       > /etc/apt/sources.list.d/raspi.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-picamera2 \
    python3-libcamera \
    python3-flask \
    python3-pil \
    libcamera-tools \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY app.py .
COPY diagnostics.py .
COPY camera_controls_db.json .
COPY camera-module-info.json .
COPY gpio_map.json .
COPY camera-last-config.json .
COPY connected_cameras_config.json .
COPY templates/ templates/
COPY static/ static/

# Create gallery directory for captured images
RUN mkdir -p /app/static/gallery /app/static/camera_profiles

EXPOSE 8080

CMD ["python3", "app.py", "--ip", "0.0.0.0", "--port", "8080"]
