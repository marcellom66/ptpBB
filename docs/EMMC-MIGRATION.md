# BeagleBone Black SD to eMMC migration

## Current board state (verified 2026-08-02)

- Running root: `/dev/mmcblk0p3`, a 32 GB microSD (`type=SD`).
- Used root data: about 1.6 GB, so the files can fit in a 4 GB eMMC.
- eMMC controller node: present in the device tree but `status=disabled`.
- `/dev/mmcblk1`: not currently available. Do **not** run the flasher yet.
- Installed flasher: `bb-beagle-flasher` version shipped with the 2026 image.
- Flasher defaults already say `source=/dev/mmcblk0` and
  `destination=/dev/mmcblk1`.

A raw `dd` clone is not suitable: the source card is 32 GB and the destination
is 4 GB. Use the filesystem-aware Beagle flasher, which creates destination
partitions and copies the used filesystem.

## Required physical boot check

The likely reason the SD root is running while the eMMC controller is disabled
is that an older U-Boot from eMMC started the kernel from SD. To force the SD's
bootloader:

1. Shut down cleanly with `sudo poweroff`.
2. Remove power; a warm reset is not sufficient.
3. Keep the S2/BOOT button pressed.
4. Reapply stable 5 V power and release S2 after the user LEDs start.
5. Reconnect over USB SSH and run:

   ```sh
   lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS
   cat /sys/block/mmcblk0/device/type
   cat /sys/block/mmcblk1/device/type
   ```

Proceed only if `mmcblk0` is the SD and `mmcblk1` is the eMMC/MMC device.

## Pre-flash safety gate

Before enabling the flasher, save a copy of the acquisition database and verify
the exact devices:

```sh
sudo systemctl stop beagleptp
sudo cp -a /var/lib/beagleptp /home/beagle/beagleptp-data-backup
findmnt -no SOURCE /
lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS
grep -vE '^[[:space:]]*(#|$)' /etc/default/beagle-flasher
```

Expected flasher mapping:

```text
source=/dev/mmcblk0
destination=/dev/mmcblk1
```

If the mapping is reversed or `mmcblk1` is absent, stop.

## Flash sequence (destructive to eMMC)

Only after the safety gate passes:

```sh
sudo enable-beagle-flasher
sudo poweroff
```

Then cold-boot from the SD while holding S2/BOOT. The flasher runs during boot
and uses the four user LEDs as a progress pattern. Keep power connected until
the flasher reports completion/powers down according to the image behavior.
Remove the SD, cold-boot normally, and verify:

```sh
findmnt -no SOURCE /
systemctl is-enabled beagleptp
systemctl is-active beagleptp
```

The root device must now be an eMMC partition, normally `/dev/mmcblk1p3` or the
device numbering chosen when eMMC is the boot source.

## Recovery

The SD remains the recovery medium. If eMMC boot fails, power off, insert the
SD, hold S2/BOOT while applying power, and repair or reflash from the SD system.
