# Calibration pictures

38 RGBA PNG pictures (10 monkeys, 24 foods, 3 other animals, 1 tree) that an
eye-tracker calibration can show instead of the standard target
(`eyetracker.calibration_target`, docs/eye-tracker.md).

They are the originals from `sh4r11f/realtime-rdk` at commit
`0fe02e1361c9a0293450934443358102c6efa6eb`, directory `assets/calibration`,
copied byte for byte: no resizing, recompression or edits. `manifest.json`
lists each file's SHA-256, size, pixel dimensions and its path and git blob in
that repository; the loader (`alhazen.config.calibration_images`) refuses a
file whose bytes no longer match it.

Provenance beyond that repository (who drew them, under what licence) has not
been established. These files are the private repository's; decide on that
before this directory is published.
