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

## Run with Docker (Debian 13)

```bash
git clone https://github.com/doodersrage/printbridge.git
cd printbridge
docker compose up -d --build
```

Then open `http://<server>:8000` and enter your Wi-Fi Box's IP under **Settings**.
Settings and jobs are kept in `./data`.

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

The profile in `src/printbridge/profiles/` targets the stock V3 SE: a
220 × 220 × 250 mm bed, a 0.4 mm nozzle, the direct-drive extruder with 0.8 mm
retraction, Marlin 2 G-code and the Creality-style purge line. It ships with:

- **Quality:** Draft (0.28 mm), Standard (0.20 mm), Fine (0.12 mm)
- **Material:** PLA, PETG, TPU
- **Per-job options:** infill, supports (none, touching the build plate, or everywhere), brim, nozzle and bed temperature overrides, auto-arrange and centering, and an optional fresh `G29` bed probe. Without the probe it loads the saved mesh with `M420 S1`.

Settings saved inside a 3MF from another slicer (Bambu Studio, Orca, Cura,
PrusaSlicer) are ignored. Only the geometry is used, so the G-code always
matches this printer. Objects placed for a different bed are re-centered on the
220 mm plate, unless you turn off **Arrange & center**. If you upload `.gcode`,
slicing is skipped and the file can be sent as is.

To tune the output, edit the `.ini` files. They use normal PrusaSlicer config
keys, and you can drop in a new quality or material by adding a file and its
entry in `slicer.py`.

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

## Security note

PrintBridge has **no login**. Anyone who can reach it can start prints, so keep
it on your LAN or put it behind a reverse proxy that requires authentication.
