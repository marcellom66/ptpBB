#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root." >&2
    exit 1
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install_dir=/opt/beagleptp

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    linuxptp ethtool python3 python3-venv python3-fastapi python3-uvicorn \
    gpsd chrony pps-tools polkitd

if ! getent group beagleptp >/dev/null; then
    groupadd --system beagleptp
fi
if ! id beagleptp >/dev/null 2>&1; then
    useradd --system --gid beagleptp --home-dir /var/lib/beagleptp --shell /usr/sbin/nologin beagleptp
fi

install -d -m 0755 "$install_dir"
if [ ! -x "$install_dir/venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$install_dir/venv"
fi
# Recreate an earlier isolated venv so Debian's validated ARMHF packages are visible.
if ! "$install_dir/venv/bin/python" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    mv "$install_dir/venv" "$install_dir/venv.incomplete.$(date +%s)"
    python3 -m venv --system-site-packages "$install_dir/venv"
fi
# Setuptools can otherwise reuse stale build/lib files when an existing device
# installation is upgraded with a package that keeps the same version number.
if [ -d "$project_dir/build" ]; then
    find "$project_dir/build" -depth -delete
fi
"$install_dir/venv/bin/pip" install --no-build-isolation --no-deps "$project_dir"

install -d -o beagleptp -g beagleptp -m 0750 /var/lib/beagleptp
install -d -o root -g beagleptp -m 0750 /etc/beagleptp
if [ ! -e /etc/beagleptp/beagleptp.env ]; then
    printf 'BEAGLEPTP_API_TOKEN=\n' > /etc/beagleptp/beagleptp.env
    chmod 0640 /etc/beagleptp/beagleptp.env
    chown root:beagleptp /etc/beagleptp/beagleptp.env
    echo "API authentication disabled for the USB-only dashboard"
fi
if grep -q '^BEAGLEPTP_ALLOW_POWEROFF=' /etc/beagleptp/beagleptp.env; then
    sed -i 's/^BEAGLEPTP_ALLOW_POWEROFF=.*/BEAGLEPTP_ALLOW_POWEROFF=1/' \
        /etc/beagleptp/beagleptp.env
else
    printf 'BEAGLEPTP_ALLOW_POWEROFF=1\n' >> /etc/beagleptp/beagleptp.env
fi

install -m 0644 "$project_dir/deploy/99-beagleptp.rules" /etc/udev/rules.d/99-beagleptp.rules
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$project_dir/deploy/60-beagleptp-poweroff.rules" \
    /etc/polkit-1/rules.d/60-beagleptp-poweroff.rules
install -m 0644 "$project_dir/deploy/gpsd.default" /etc/default/gpsd
if [ -e /etc/chrony/chrony.conf ] && [ ! -e /etc/chrony/chrony.conf.pre-beagleptp ]; then
    cp -a /etc/chrony/chrony.conf /etc/chrony/chrony.conf.pre-beagleptp
fi
install -m 0644 "$project_dir/deploy/chrony-gps.conf" /etc/chrony/chrony.conf
install -m 0644 "$project_dir/deploy/beagleptp.service" /etc/systemd/system/beagleptp.service
udevadm control --reload-rules
udevadm trigger --subsystem-match=ptp
systemctl daemon-reload
systemctl enable --now gpsd.socket chrony.service
systemctl enable beagleptp.service
# `enable --now` does not restart an already-running service during upgrades.
systemctl restart beagleptp.service

echo "BeaglePTP installed. Run: systemctl status beagleptp"
