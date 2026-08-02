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
    gpsd chrony pps-tools

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
"$install_dir/venv/bin/pip" install --no-build-isolation --no-deps "$project_dir"

install -d -o beagleptp -g beagleptp -m 0750 /var/lib/beagleptp
install -d -o root -g beagleptp -m 0750 /etc/beagleptp
if [ ! -e /etc/beagleptp/beagleptp.env ]; then
    token=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    printf 'BEAGLEPTP_API_TOKEN=%s\n' "$token" > /etc/beagleptp/beagleptp.env
    chmod 0640 /etc/beagleptp/beagleptp.env
    chown root:beagleptp /etc/beagleptp/beagleptp.env
    echo "API token written to /etc/beagleptp/beagleptp.env"
fi

install -m 0644 "$project_dir/deploy/99-beagleptp.rules" /etc/udev/rules.d/99-beagleptp.rules
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
systemctl enable --now beagleptp.service

echo "BeaglePTP installed. Run: systemctl status beagleptp"
