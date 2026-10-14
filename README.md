# PrintBridge

A small self-hosted web app for a **Creality Ender-3 V3 SE**. It takes a model
(3MF, STL or OBJ), slices it into printer-ready G-code with PrusaSlicer using a
built-in V3 SE profile, and sends it to the printer's print host. You can start
the print right away or just upload it.

## How it connects to the printer

The Ender-3 V3 SE has no Wi-Fi or Ethernet of its own. PrintBridge talks to
whatever is plugged into its USB port:

| Host | What PrintBridge uses |
| --- | --- |
| **Creality Wi-Fi Box 2.0** (default) | The box's local API on port 81 for status and control, and anonymous FTP to upload onto the box's microSD card. This is the same protocol Creality's own CrealityPrint slicer uses over LAN. |
| **OctoPrint** | REST API with an API key (OctoPrint → Settings → Application Keys) |
| **Moonraker** (Klipper) | Moonraker HTTP API; the API key is optional |

PrintBridge uploads the G-code to that host and can start, pause, resume or
cancel the print. It also shows nozzle and bed temperatures and print progress.

### Creality Wi-Fi Box 2.0 notes

- Enter the box's **IP address** in Settings, for example `192.168.1.50`. Give
  it a DHCP reservation in your router so the address doesn't change.
- The box needs a **microSD card** inserted. Uploaded files are stored there.
- The box must be on the same LAN as the PrintBridge server and reachable on
  TCP **81** (HTTP) and **21** (FTP, passive mode).
- Creality has never published this API, so a future box firmware update could change it.

## Install with Docker (Debian 13)

A ready-made image is published to GitHub's container registry for
`linux/amd64` and `linux/arm64` on every push to `main`:
`ghcr.io/doodersrage/printbridge:latest`.

You only need the compose file, not the whole repo:

```bash
mkdir printbridge && cd printbridge
curl -fsSLO https://raw.githubusercontent.com/doodersrage/printbridge/main/docker-compose.yml
docker compose up -d
```

Then open `http://<server>:8000` and enter your Wi-Fi Box's IP under **Settings**.
Settings and jobs are kept in the `printbridge-data` Docker volume.

To update to the latest version:

```bash
docker compose pull && docker compose up -d
```

### Versions

Each release is published with matching image tags, and `latest` follows `main`:

| Tag | Tracks |
| --- | --- |
| `latest` | every push to `main` |
| `0.1` | the newest 0.1.x release |
| `0.1.0` | exactly that release |

To stay on a release line instead of following `main`, set the image in
`docker-compose.yml` to `ghcr.io/doodersrage/printbridge:0.1`. The running
version is shown next to the printer name in the header. See
[CHANGELOG.md](CHANGELOG.md) for what changed in each release.

To build the image yourself, clone the repo and replace the `image:` line in
`docker-compose.yml` with `build: .`.

The image is built on `debian:trixie-slim` and installs PrusaSlicer from the
Debian archive. You can also set the first-run defaults with environment
variables (for example in a `.env` file next to `docker-compose.yml`):

```env
PRINTBRIDGE_PRINTER_KIND=creality_box   # or octoprint / moonraker
PRINTBRIDGE_PRINTER_URL=192.168.1.50
PRINTBRIDGE_PRINTER_API_KEY=            # OctoPrint only
```

These only apply until the settings are first saved in the UI.

## Slicing

The built-in profile targets the stock V3 SE: a 220 × 220 × 250 mm bed, a
0.4 mm nozzle, the direct-drive extruder with 0.8 mm retraction, Marlin 2
G-code and a Creality-style purge line. The bed heats fully before homing, so
the probe measures it hot. It ships with:

- **Quality:** Draft (0.28 mm), Standard (0.20 mm), Fine (0.12 mm)
- **Material:** PLA, PETG, TPU
- **Per-job options:** infill, supports, brim, temperature overrides, **scale**, **rotation**, **copies**, auto-arrange, and an optional fresh `G29` bed probe. Without the probe it loads the saved mesh with `M420 S1`.
- **Progress:** `M73` progress and remaining-time lines, so the Wi-Fi Box, the printer screen and PrintBridge show how far along a print is.
- **Presets:** save the current options under a name, for example "PETG functional", and load them with one click.

Settings saved inside a 3MF from another slicer (Bambu Studio, Orca, Cura,
PrusaSlicer) are ignored. Only the geometry is used, so the G-code always
matches this printer. **Multi-plate 3MFs** from Bambu Studio and OrcaSlicer get
a plate picker on the job. Plate 1 is sliced by default, and choosing another
plate re-slices that plate only. If you upload `.gcode`, slicing is skipped.

### Editing profiles

Click **Profiles** to edit the machine, quality and material profiles in the
browser, or to add your own, such as an ASA material or a 0.16 mm quality.
Edits are saved in the data volume under `profiles/`, so they survive image
updates. **Reset to default** brings back the built-in version. The files use
PrusaSlicer's config keys, and the first comment line is the name shown in
menus.

## Previews

Each job's **3D view** has two tabs:

- **Model:** the model on the 220 × 220 mm bed with the same placement
  PrusaSlicer will use. Anything outside the build volume turns red.
- **Toolpaths:** the sliced G-code, coloured by feature (perimeters, infill,
  supports, brim and so on), with a layer slider and a single-layer mode.

three.js is bundled with the app, so the viewer works without internet access.

## Calibration

**Calibrate** in the header creates test prints for tuning this printer:

| Test | What to look for | Saves |
| --- | --- | --- |
| **Temperature tower** | The band with the least stringing between pillars and the cleanest bridge. Each 8 mm band prints at a different temperature. | Nozzle temperature of the material profile |
| **Retraction test** | The band with the fewest strings between the two pillars. The length changes every 5 mm. | Retraction length of the material profile |
| **20 mm size check** | Each side should measure 20.0 mm (±0.2). The bump marks the X side. | – |
| **First-layer test** | Five single-layer squares across the bed. Use **Live Z** while it prints until the lines are smooth and touching. | – (enter the final offset on the printer's screen) |

Calibration jobs list their bands under the job. Pick the best one and click
**Save to profile**.

**Live Z** buttons appear on the Printer card during a print. Each press moves
the nozzle 0.05 mm with `M290` babystepping. ▼ gives more squish and ▲ gives
less.

## Printer controls, storage and notifications

- **Controls:** preheat for any material profile, cool down, home, the printer
  light (Wi-Fi Box) and a G-code console. Temperature, homing and G-code
  commands are refused while a print is running.
- **Busy guard:** **Print** is disabled while the printer is printing or
  offline, and the server refuses it too.
- **Notifications** (Settings → Notifications), on start, finish, stop or error:
  - **ntfy** push notifications to your phone
  - a **webhook**, for example Home Assistant
  - **email** over SMTP. Gmail and Outlook need an app password. Finished and stopped emails include a picture of the model.
- **Home Assistant** (Settings → Home Assistant): publishes to your MQTT broker
  with discovery, so an *Ender-3 V3 SE* device appears with status,
  temperature, progress and time-left sensors and pause, resume, cancel,
  cool-down and light buttons.
- **Webcam**: point Settings → Webcam at any MJPEG stream or JPEG snapshot URL.
  PrintBridge relays it to the Printer card.
- **Print history** (header → History): every finished or stopped print, with
  time, filament and cost, monthly and all-time totals, and one-click reprint.
- **Filament prices**: set a price per kg for each material under Settings.
  Newly sliced jobs then show a cost.
- **Printer storage**: list and delete files on the Wi-Fi Box's card (or on
  OctoPrint or Moonraker). The file being printed is protected.
- **Job cleanup**: finished jobs can be deleted automatically after a number of
  days.
- **Backup**: Settings → Backup downloads your settings (including passwords),
  presets, profile edits and history as a zip, and restores one.

## Adding models

- Drag in **3MF, STL, OBJ or G-code**, or a **zip**. A zip creates one job per
  model inside, which suits Thingiverse's "Download all files".
- **Paste a direct download link** to import a model. Sites such as
  Thingiverse, Printables and MakerWorld block downloads from apps, so for
  those, download in your browser and drop the file in.
- **Combine**: tick two or more jobs and click **Combine selected** to lay them
  out on one bed. Each keeps its own scale, rotation and copies.
- Each job gets a **thumbnail**, rendered once in your browser.

## Install as an app

PrintBridge has a web app manifest and icons, so phones can add it to the home
screen: in Safari use Share → Add to Home Screen, and in Chrome use ⋮ → Add to
Home screen. Full standalone installation in Chrome needs HTTPS (a reverse
proxy). Over plain `http://` on your LAN it is added as a shortcut.

## Local development

You need [uv](https://docs.astral.sh/uv/) and PrusaSlicer (`apt install prusa-slicer`).

```bash
uv sync
uv run printbridge            # http://localhost:8000
uv run pytest
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `PRINTBRIDGE_DATA` | `./data` | settings + job storage |
| `PRINTBRIDGE_SLICER` | auto-detect | path to the PrusaSlicer executable |
| `PRINTBRIDGE_HOST` / `PRINTBRIDGE_PORT` | `0.0.0.0` / `8000` | listen address |

## Login

Authentication is off by default. To turn it on, set a password, for example
in a `.env` file next to `docker-compose.yml`:

```env
PRINTBRIDGE_USERNAME=admin
PRINTBRIDGE_PASSWORD=choose-something-long
```

Then run `docker compose up -d` again to apply it. The browser goes to a
sign-in page, and the session lasts 30 days (`PRINTBRIDGE_SESSION_DAYS`).
Scripts can use HTTP Basic auth with the same credentials:

```bash
curl -u admin:choose-something-long http://<server>:8000/api/printer
```

| Variable | Purpose |
| --- | --- |
| `PRINTBRIDGE_USERNAME` | login name, default `admin` |
| `PRINTBRIDGE_PASSWORD` | turns authentication on when set |
| `PRINTBRIDGE_PASSWORD_FILE` | read the password from a file instead (Docker secrets) |
| `PRINTBRIDGE_SESSION_DAYS` | how long a sign-in lasts, default 30 |
| `PRINTBRIDGE_COOKIE_SECURE` | `true` when served over HTTPS |
| `PRINTBRIDGE_SECRET_KEY` | key that signs session cookies; generated and saved in the data volume if unset |

Changing the password signs everyone out. After 5 wrong passwords from one IP
address, that address is locked out for 5 minutes. `/healthz` stays public for
the container health check.

This is a single shared login meant for a home LAN. Don't expose PrintBridge
directly to the internet. Put it behind a VPN or a reverse proxy with HTTPS,
and set `PRINTBRIDGE_COOKIE_SECURE=true` there.
