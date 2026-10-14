# Changelog

All notable changes to PrintBridge are listed here. Versions follow
[Semantic Versioning](https://semver.org/); while the version is 0.x, minor
releases may include breaking changes.

## [Unreleased]

### Security
- Cross-site write requests (including form posts) are refused, so a web page cannot drive the printer or restore a backup through your browser. This matters most when login is off.
- Settings no longer send saved passwords, tokens or API keys back to the browser.
- The slicer path can only be set by `PRINTBRIDGE_SLICER` (or auto-detection), not through settings or a restored backup.
- "Import from link" refuses loopback, link-local (cloud metadata) and similar addresses, checked on every redirect.
- Zip imports are limited to 512 MB unpacked in total.
- OctoPrint and Moonraker file deletion escapes file names and rejects `..` paths.
- Added `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy` headers.

## [0.2.0] - 2026-10-14

### Added
- **Email notifications** over SMTP (STARTTLS, SSL or plain), with a picture of the model on finished and stopped prints.
- **Home Assistant integration** over MQTT with discovery: status, temperature, progress and time-left sensors; pause, resume, cancel, cool-down and light buttons.
- **Calibration prints**: temperature tower, retraction test, 20 mm size check and first-layer test, with "Save to profile" for the chosen band.
- **Live Z** babystepping (`M290`) during a print.
- **Print history** with monthly and all-time totals, success rate and reprint.
- **Filament prices** and a cost for each job and print.
- **Thumbnails** for jobs, also used in emails.
- **Import from a link**, and **zip uploads** that add every model inside.
- **Combine** several jobs onto one bed.
- **Webcam** relay (any MJPEG or snapshot URL).
- **Backup and restore** of settings, presets, profiles and history.
- Web app manifest and icons for adding PrintBridge to a phone's home screen.
- **Detection of 3MFs made for another printer**: shows the target printer and bed, any scale factor and the units stored in the file, with **Use original size** to undo a stored scale.
- **Spool tracking**: remaining filament on the loaded spool, deducted after each print, with a warning before printing if it won't be enough or the material differs.
- **Resize by dimensions** in the 3D view, and the printed size (W × D × H) on every job.

### Fixed
- The Wi-Fi Box's `err` field is no longer reported as a printer error. It reads 1 during every normal print, which showed "Printer reported error 1" and could trigger false error notifications.
- The Light button shows the box's real light state.

### Verified on hardware
Tested on a Creality Wi-Fi Box 2.0 (firmware V3.04b06) with an Ender-3 V3 SE: status, preheat and cool down, home, light on/off, FTP upload and delete, and finish detection (state 1 → 2 with progress at 100%). Live Z during a print has not been tested on hardware yet.

## [0.1.0] - 2026-10-14

The first versioned release.

### Slicing
- Slices 3MF, STL and OBJ files with PrusaSlicer, using a built-in Creality Ender-3 V3 SE profile.
- Includes Draft, Standard and Fine quality profiles and PLA, PETG and TPU material profiles.
- Per-job options: infill, supports, brim, temperature overrides, scale, rotation, copies, auto-arrange and an optional bed probe.
- Settings saved inside 3MF files from other slicers are ignored, so the G-code always matches this printer. Multi-plate 3MFs from Bambu Studio and OrcaSlicer get a plate picker.
- Emits `M73` progress lines so the Wi-Fi Box and the printer screen can show progress.
- Profiles can be edited in the browser, and custom profiles can be added. Edits are kept in the data volume.
- Named presets for slicing options.

### Printing
- Works with three print hosts: the Creality Wi-Fi Box 2.0 (port-81 API plus FTP upload), OctoPrint and Moonraker.
- Upload only, or upload and start, with a busy guard that refuses to start a print while the printer is printing or offline.
- Pause, resume and cancel; preheat, cool down, home, light and a G-code console.
- A background monitor tracks each job (printing, printed, stopped) and sends ntfy and webhook notifications.
- A printer storage view lists files on the print host and deletes the ones you choose. Old jobs can be deleted automatically.

### Interface
- A single-page web UI with light and dark themes that works on phones.
- 3D view with two tabs: the model as placed on the bed, and the sliced toolpaths with a layer slider.
- An upload progress bar.
- Optional single-user login, configured through environment variables.

### Deployment
- Docker image for `linux/amd64` and `linux/arm64`, published to `ghcr.io/doodersrage/printbridge`.
- Before the image is published, CI runs the tests and slices a test model inside the container.

[0.2.0]: https://github.com/doodersrage/printbridge/releases/tag/v0.2.0
[0.1.0]: https://github.com/doodersrage/printbridge/releases/tag/v0.1.0
