# USB GNSS and PPS timing

## Installed architecture

BeaglePTP connects only to the local GPSD service on `127.0.0.1:2947`.
GPSD automatically discovers supported receivers exposed as `/dev/ttyACM*` or
`/dev/ttyUSB*`. Chrony receives two local reference clocks from GPSD:

- `GNSS` (SHM 0): NMEA/receiver time-of-day, usable for coarse UTC.
- `PPS` (SHM 1): pulse-per-second phase, preferred and locked to `GNSS`.

There are deliberately no public NTP `pool` or `server` entries. With no GNSS
receiver, Chrony stays `Not synchronised` and BeaglePTP stays `UNTRUSTED`.

## Connecting a USB receiver

1. Use a receiver supported by GPSD and place its antenna where it has a clear
   sky view.
2. Connect it to the BeagleBone USB host port, preferably through a powered hub
   if its current consumption is significant.
3. Check device discovery:

   ```sh
   lsusb
   ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
   systemctl status gpsd.socket gpsd chrony beagleptp
   chronyc sources -v
   ```

4. Open the dashboard's **Time Integrity** view. A receiver with valid
   time-of-day but no PPS moves the system to `DEGRADED`, never `TRUSTED`.

## Accuracy boundary

USB NMEA is suitable for establishing UTC and correcting errors of seconds or
milliseconds. It is not a calibrated sub-microsecond phase reference. USB adds
scheduling and polling latency; even receivers that tunnel PPS over USB can
have substantial jitter.

For higher accuracy, choose a timing receiver that exposes a separate 3.3 V
logic-level 1 PPS output. Connect that PPS to a kernel-supported PPS capture
input through the correct level protection and pin mux. Never connect an RS-232
voltage-level signal directly to a BeagleBone header.

The running kernel has LinuxPPS and the `pps-gpio` client available. When a
board-specific device-tree overlay has created `/dev/pps0`, verify it with:

```sh
ls -l /dev/pps*
sudo ppstest /dev/pps0
chronyc sources -v
chronyc tracking
```

The exact header pin and overlay must be selected from the receiver voltage,
edge polarity and BeagleBone revision. Do not wire PPS until those details are
known. GPIO PPS is normally a microsecond-class path; a calibrated
sub-microsecond or nanosecond system needs a dedicated hardware capture/timing
cape or FPGA close to the connector.

## Source authorization

Enter each permitted PTP `clockIdentity` in **Setup → Allowed Grandmasters**,
one per line. An empty list is intentionally fail-closed: a live PTP source can
produce measurements, but overall time integrity cannot become `TRUSTED`.

The trust decision is:

- `TRUSTED`: fresh 3D GNSS fix, fresh PPS, current PTP data and authorized GM.
- `DEGRADED`: only USB GNSS or only a current but incompletely trusted source.
- `HOLDOVER`: live sources were lost inside the configured bounded interval.
- `UNTRUSTED`: no current source or the holdover interval has expired.

## Recovery

The original Debian Chrony configuration is retained as:

```text
/etc/chrony/chrony.conf.pre-beagleptp
```

The active GPS-only policy is:

```text
/etc/chrony/chrony.conf
```
