"""Home Assistant integration over MQTT.

Publishes the printer as a Home Assistant device using MQTT discovery: state,
temperatures, progress, time left and the current job, plus buttons for pause,
resume, cancel and the light. State is one retained JSON document that each
entity reads with a value template; buttons publish to a command topic that
PrintBridge subscribes to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable

import aiomqtt

from . import __version__
from .config import MqttConfig
from .jobs import Job
from .printers import PrinterStatus

log = logging.getLogger("printbridge.mqtt")

CommandHandler = Callable[[str], Awaitable[None]]
COMMANDS = ("pause", "resume", "cancel", "light_on", "light_off", "cooldown")

# (key, name, unit, device_class, state_class, icon)
SENSORS = [
    ("state", "Status", None, None, None, "mdi:printer-3d"),
    ("progress", "Progress", "%", None, "measurement", "mdi:progress-clock"),
    ("nozzle", "Nozzle temperature", "°C", "temperature", "measurement", None),
    ("nozzle_target", "Nozzle target", "°C", "temperature", "measurement", None),
    ("bed", "Bed temperature", "°C", "temperature", "measurement", None),
    ("bed_target", "Bed target", "°C", "temperature", "measurement", None),
    ("remaining", "Time remaining", "s", "duration", None, "mdi:timer-sand"),
    ("elapsed", "Print time", "s", "duration", None, "mdi:timer-outline"),
    ("file", "Current file", None, None, None, "mdi:file-outline"),
    ("job", "Current job", None, None, None, "mdi:cube-outline"),
    ("spool_remaining", "Spool remaining", "g", "weight", "measurement", "mdi:printer-3d-nozzle"),
]
BUTTONS = [
    ("pause", "Pause print", "mdi:pause"),
    ("resume", "Resume print", "mdi:play"),
    ("cancel", "Cancel print", "mdi:stop"),
    ("cooldown", "Cool down", "mdi:snowflake"),
    ("light_on", "Light on", "mdi:lightbulb-on"),
    ("light_off", "Light off", "mdi:lightbulb-off"),
]


def topics(cfg: MqttConfig) -> dict[str, str]:
    base = cfg.topic_prefix
    return {"state": f"{base}/state", "availability": f"{base}/availability", "command": f"{base}/command"}


def state_payload(st: PrinterStatus, job: Job | None) -> dict:
    printing = st.online and st.state in ("Printing", "Paused")
    return {
        "online": st.online,
        "printing": printing,
        "state": st.state,
        "progress": round(st.progress, 1) if printing and st.progress is not None else 0,
        "nozzle": st.nozzle.actual,
        "nozzle_target": st.nozzle.target,
        "bed": st.bed.actual,
        "bed_target": st.bed.target,
        "remaining": int(st.remaining) if printing and st.remaining else 0,
        "elapsed": int(st.elapsed) if printing and st.elapsed else 0,
        "file": (st.file or "").rsplit("/", 1)[-1] if printing else "",
        "job": job.name if printing and job else "",
        "message": st.message or "",
        "spool_remaining": _spool_remaining(),
    }


def _spool_remaining() -> float | None:
    from . import spools  # local import: spools reads the data directory at call time

    spool = spools.loaded()
    return round(spool.remaining_g) if spool else None


def discovery_messages(cfg: MqttConfig, supports_light: bool, url: str = "") -> list[tuple[str, dict]]:
    t = topics(cfg)
    node = cfg.topic_prefix
    device = {
        "identifiers": [f"printbridge_{node}"],
        "name": "Ender-3 V3 SE",
        "manufacturer": "Creality",
        "model": "Ender-3 V3 SE (via PrintBridge)",
        "sw_version": __version__,
    }
    if url:
        device["configuration_url"] = url
    common = {"availability_topic": t["availability"], "device": device}
    out = []
    for key, name, unit, dclass, sclass, icon in SENSORS:
        cfg_msg = {**common, "name": name, "unique_id": f"{node}_{key}", "object_id": f"{node}_{key}",
                   "state_topic": t["state"], "value_template": f"{{{{ value_json.{key} }}}}"}
        if unit:
            cfg_msg["unit_of_measurement"] = unit
        if dclass:
            cfg_msg["device_class"] = dclass
        if sclass:
            cfg_msg["state_class"] = sclass
        if icon:
            cfg_msg["icon"] = icon
        out.append((f"{cfg.discovery_prefix}/sensor/{node}/{key}/config", cfg_msg))
    for key, name, dclass in (("printing", "Printing", "running"), ("online", "Printer online", "connectivity")):
        out.append((f"{cfg.discovery_prefix}/binary_sensor/{node}/{key}/config", {
            **common, "name": name, "unique_id": f"{node}_{key}", "object_id": f"{node}_{key}",
            "state_topic": t["state"], "device_class": dclass,
            "value_template": f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
        }))
    for key, name, icon in BUTTONS:
        topic = f"{cfg.discovery_prefix}/button/{node}/{key}/config"
        if key.startswith("light") and not supports_light:
            out.append((topic, {}))  # an empty retained config removes the entity
            continue
        out.append((topic, {**common, "name": name, "unique_id": f"{node}_{key}", "object_id": f"{node}_{key}",
                            "command_topic": t["command"], "payload_press": key, "icon": icon}))
    return out


class MqttBridge:
    def __init__(self, get_cfg: Callable[[], MqttConfig], on_command: CommandHandler,
                 supports_light: Callable[[], bool], get_url: Callable[[], str]):
        self.get_cfg = get_cfg
        self.on_command = on_command
        self.supports_light = supports_light
        self.get_url = get_url
        self.client: aiomqtt.Client | None = None
        self.connected = False
        self.last_error: str | None = None
        self._last_state: str | None = None
        self._restart = asyncio.Event()

    def restart(self) -> None:
        """Reconnect with new settings."""
        self._restart.set()

    async def publish_state(self, st: PrinterStatus, job: Job | None) -> None:
        if not (self.client and self.connected):
            return
        payload = json.dumps(state_payload(st, job))
        if payload == self._last_state:
            return
        try:
            await self.client.publish(topics(self.get_cfg())["state"], payload, qos=0, retain=True)
            self._last_state = payload
        except aiomqtt.MqttError as e:
            self.last_error = str(e)

    async def run(self) -> None:
        backoff = 5
        while True:
            self._restart.clear()
            cfg = self.get_cfg()
            if not (cfg.enabled and cfg.host):
                self.connected, self.last_error = False, None
                await self._restart.wait()
                continue
            try:
                await self._session(cfg)
                backoff = 5
            except aiomqtt.MqttError as e:
                self.connected, self.client = False, None
                self.last_error = str(e)
                log.warning("MQTT connection failed: %s", e)
                try:
                    await asyncio.wait_for(self._restart.wait(), backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 300)

    async def _session(self, cfg: MqttConfig) -> None:
        t = topics(cfg)
        tls = ssl.create_default_context() if cfg.tls else None
        will = aiomqtt.Will(t["availability"], "offline", qos=1, retain=True)
        async with aiomqtt.Client(
            cfg.host, port=cfg.port, username=cfg.username or None, password=cfg.password or None,
            identifier=f"printbridge-{cfg.topic_prefix}", will=will, tls_context=tls,
        ) as client:
            self.client, self.connected, self.last_error, self._last_state = client, True, None, None
            for topic, payload in discovery_messages(cfg, self.supports_light(), self.get_url()):
                await client.publish(topic, json.dumps(payload) if payload else "", qos=1, retain=True)
            await client.publish(t["availability"], "online", qos=1, retain=True)
            await client.subscribe(t["command"])
            listener = asyncio.create_task(self._listen(client))
            restart = asyncio.create_task(self._restart.wait())
            try:
                done, _ = await asyncio.wait({listener, restart}, return_when=asyncio.FIRST_COMPLETED)
                if listener in done and listener.exception():
                    raise listener.exception()  # connection lost: reconnect via run()
            finally:
                listener.cancel()
                restart.cancel()
                try:
                    await client.publish(t["availability"], "offline", qos=1, retain=True)
                except aiomqtt.MqttError:
                    pass
                self.client, self.connected = None, False

    async def _listen(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            command = message.payload.decode(errors="replace").strip() if isinstance(message.payload, bytes) else str(message.payload)
            if command not in COMMANDS:
                log.warning("Ignoring unknown MQTT command %r", command)
                continue
            try:
                await self.on_command(command)
            except Exception as e:  # report and keep listening
                log.warning("MQTT command %s failed: %s", command, e)
