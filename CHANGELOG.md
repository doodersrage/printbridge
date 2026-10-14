# Changelog

All notable changes to PrintBridge are listed here. Versions follow
[Semantic Versioning](https://semver.org/); while the version is 0.x, minor
releases may include breaking changes.

## [Unreleased]

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
- **Resize by dimensions** in the 3D view, and the printed size (W × D × H) on every job.

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

[0.1.0]: https://github.com/doodersrage/printbridge/releases/tag/v0.1.0
