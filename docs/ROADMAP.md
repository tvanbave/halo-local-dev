# Remaining work

- Native scenes: creation, editing, removal, recovery and optional MQTT discovery
  are implemented, with set aside and forget for operations that cannot finish.
  Continue power-cycle/restart and larger-scene qualification, confirm on hardware
  how lights answer after losing power, add a way to release reserved scene slots,
  investigate the remaining pause from Off, and design keypad scene assignment.
  See the [scene guide](SCENES.md).

- Reduce manual wake presses during battery-keypad setup, based on captured app
  timing and preserved verification/recovery rules.
- Verify HA automations and long sleep/wake behavior for the battery keypad.
- Complete Anyplace slider, native control, sleep/wake and restart acceptance.
- Test existing-controller group changes and HA/native mode switches on more units.
- Compare group timing with native keypad transmissions.
- Broaden physical removal, HA cleanup and re-pair checks across lights and
  controller models. Removal is available in the development build; enabling it
  does not qualify every model.
- Validate longer Bluetooth/MQTT outages, power loss, recovery and soak operation.
- Accept the dev44 ESP-style account import on a second Pi: clean GitHub/Compose
  install, location preview, original key/inventory, local group control and restart.
  Subsequent sync must stay on the imported mesh. Synthetic coverage is complete;
  the real-account Pi path still needs acceptance.
- Keep passive scene research separate from cloud import, which enables active
  local control. Broader scene-model and capacity qualification remains open.
- Complete physical acceptance of dev69 full-backup restore: replacement Pi without
  re-pairing, existing Home Assistant entity identity, native group control,
  controller events, scene readback and restart. The encrypted UI backup,
  reviewed replacement/current-Pi restore and failure guards are offline-tested.
  See [Backup & restore](BACKUP.md).
- Add scheduled backups, off-Pi destinations and a final-transfer migration action.
  Current full backups are manual browser downloads; older-file restore records
  an explicit compatibility policy rather than claiming verified later radio history.
- Continue physical iPhone Safari checks, including OS pickers and the keyboard.

The source contains additional capability-gated work. Capability flags describe
available code, not hardware qualification. Keep incomplete operation recovery
visible and never use a reset or automatic setter retry as an error-clearing step.
