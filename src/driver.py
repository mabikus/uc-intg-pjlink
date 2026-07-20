#!/usr/bin/env python3
"""Unfolded Circle Remote Two/3 integration driver for PJLink projectors."""

import asyncio
import json
import logging
import os
from typing import Any

import ucapi
from ucapi import MediaPlayer, StatusCodes, media_player

import pjlink
from pjlink import PJLinkClient, PJLinkError

_LOG = logging.getLogger("pjlink-driver")

POLL_INTERVAL = 20  # seconds

SIMPLE_COMMANDS = [
    "AV_MUTE_ON",
    "AV_MUTE_OFF",
    "VIDEO_MUTE_ON",
    "VIDEO_MUTE_OFF",
    "AUDIO_MUTE_ON",
    "AUDIO_MUTE_OFF",
    "FREEZE_ON",
    "FREEZE_OFF",
]

loop = asyncio.new_event_loop()
api = ucapi.IntegrationAPI(loop)

# entity_id -> device dict: {"config": {...}, "client": PJLinkClient, "sources": [...]}
_devices: dict[str, dict[str, Any]] = {}
_subscribed: set[str] = set()
_poll_task: asyncio.Task | None = None
_standby = False


# ----------------------------------------------------------------------------
# Configuration persistence
# ----------------------------------------------------------------------------

def _config_path() -> str:
    return os.path.join(os.getenv("UC_CONFIG_HOME", "."), "pjlink_config.json")


def _load_config() -> list[dict]:
    try:
        with open(_config_path(), "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return []


def _save_config(configs: list[dict]) -> None:
    try:
        with open(_config_path(), "w", encoding="utf-8") as file:
            json.dump(configs, file, indent=2)
    except OSError as err:
        _LOG.error("Cannot write config file: %s", err)


# ----------------------------------------------------------------------------
# Entity handling
# ----------------------------------------------------------------------------

def _entity_id(config: dict) -> str:
    return f"pjlink-{config['address'].replace('.', '-')}"


def _power_to_state(power: str) -> media_player.States:
    return {
        "on": media_player.States.ON,
        "warming": media_player.States.ON,
        "cooling": media_player.States.STANDBY,
        "off": media_player.States.OFF,
    }.get(power, media_player.States.UNKNOWN)


def _register_device(config: dict) -> None:
    """Create the media player entity for a configured projector."""
    entity_id = _entity_id(config)
    client = PJLinkClient(
        config["address"],
        port=int(config.get("port", pjlink.DEFAULT_PORT)),
        password=config.get("password", ""),
    )
    _devices[entity_id] = {"config": config, "client": client, "sources": []}

    entity = MediaPlayer(
        entity_id,
        config.get("name") or f"Projector {config['address']}",
        features=[
            media_player.Features.ON_OFF,
            media_player.Features.TOGGLE,
            media_player.Features.SELECT_SOURCE,
            media_player.Features.MUTE_TOGGLE,
            media_player.Features.MUTE,
            media_player.Features.UNMUTE,
        ],
        attributes={
            media_player.Attributes.STATE: media_player.States.UNKNOWN,
            media_player.Attributes.MUTED: False,
            media_player.Attributes.SOURCE: "",
            media_player.Attributes.SOURCE_LIST: [],
        },
        device_class=media_player.DeviceClasses.TV,
        options={media_player.Options.SIMPLE_COMMANDS: SIMPLE_COMMANDS},
        cmd_handler=media_player_cmd_handler,
    )
    api.available_entities.add(entity)
    _LOG.info("Registered PJLink projector %s (%s)", entity_id, config["address"])


async def _update_device_attributes(entity_id: str) -> None:
    """Poll one projector and push attribute updates to the remote."""
    device = _devices.get(entity_id)
    if device is None:
        return
    client: PJLinkClient = device["client"]

    try:
        power, inpt, avmt = await client.transaction([
            ("POWR", "?"),
            ("INPT", "?"),
            ("AVMT", "?"),
        ])
    except (PJLinkError, OSError, ConnectionError, asyncio.TimeoutError) as err:
        _LOG.debug("Poll failed for %s: %s", entity_id, err)
        api.configured_entities.update_attributes(entity_id, {
            media_player.Attributes.STATE: media_player.States.UNAVAILABLE,
        })
        return

    attributes: dict[str, Any] = {}

    if isinstance(power, str):
        state_name = {"0": "off", "1": "on", "2": "cooling", "3": "warming"}.get(power, "off")
        attributes[media_player.Attributes.STATE] = _power_to_state(state_name)

    if isinstance(inpt, str):
        source = next(
            (s["name"] for s in device["sources"] if s["id"] == inpt),
            pjlink.input_name(inpt),
        )
        attributes[media_player.Attributes.SOURCE] = source

    if isinstance(avmt, str):
        attributes[media_player.Attributes.MUTED] = avmt != "30"

    if not device["sources"]:
        try:
            device["sources"] = await client.get_input_list()
        except (PJLinkError, OSError, ConnectionError, asyncio.TimeoutError):
            pass
    if device["sources"]:
        attributes[media_player.Attributes.SOURCE_LIST] = [s["name"] for s in device["sources"]]

    api.configured_entities.update_attributes(entity_id, attributes)


async def _poll_loop() -> None:
    while True:
        try:
            if not _standby:
                for entity_id in list(_subscribed):
                    await _update_device_attributes(entity_id)
        except Exception as err:  # pylint: disable=broad-except
            _LOG.error("Poll loop error: %s", err)
        await asyncio.sleep(POLL_INTERVAL)


def _ensure_poll_task() -> None:
    global _poll_task  # pylint: disable=global-statement
    if _poll_task is None or _poll_task.done():
        _poll_task = loop.create_task(_poll_loop())


# ----------------------------------------------------------------------------
# Command handler
# ----------------------------------------------------------------------------

async def media_player_cmd_handler(
    entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None, *_: Any
) -> StatusCodes:
    """Handle a media player command from the remote."""
    device = _devices.get(entity.id)
    if device is None:
        return StatusCodes.NOT_FOUND
    client: PJLinkClient = device["client"]

    _LOG.info("Command %s for %s (params=%s)", cmd_id, entity.id, params)

    try:
        if cmd_id == media_player.Commands.ON:
            await client.command("POWR", "1")
        elif cmd_id == media_player.Commands.OFF:
            await client.command("POWR", "0")
        elif cmd_id == media_player.Commands.TOGGLE:
            power = await client.get_power()
            await client.command("POWR", "0" if power in ("on", "warming") else "1")
        elif cmd_id == media_player.Commands.SELECT_SOURCE:
            source_name = (params or {}).get("source", "")
            code = next(
                (s["id"] for s in device["sources"] if s["name"] == source_name),
                None,
            )
            if code is None:
                return StatusCodes.BAD_REQUEST
            await client.command("INPT", code)
        elif cmd_id == media_player.Commands.MUTE_TOGGLE:
            avmt = await client.command("AVMT", "?")
            await client.command("AVMT", "30" if avmt != "30" else "31")
        elif cmd_id == media_player.Commands.MUTE:
            await client.command("AVMT", "31")
        elif cmd_id == media_player.Commands.UNMUTE:
            await client.command("AVMT", "30")
        elif cmd_id == "AV_MUTE_ON":
            await client.command("AVMT", "31")
        elif cmd_id == "AV_MUTE_OFF":
            await client.command("AVMT", "30")
        elif cmd_id == "VIDEO_MUTE_ON":
            await client.command("AVMT", "11")
        elif cmd_id == "VIDEO_MUTE_OFF":
            await client.command("AVMT", "10")
        elif cmd_id == "AUDIO_MUTE_ON":
            await client.command("AVMT", "21")
        elif cmd_id == "AUDIO_MUTE_OFF":
            await client.command("AVMT", "20")
        elif cmd_id == "FREEZE_ON":
            await client.command("FREZ", "1", 2)
        elif cmd_id == "FREEZE_OFF":
            await client.command("FREZ", "0", 2)
        else:
            return StatusCodes.NOT_IMPLEMENTED
    except PJLinkError as err:
        _LOG.warning("PJLink error for %s / %s: %s", entity.id, cmd_id, err)
        return StatusCodes.BAD_REQUEST
    except (OSError, ConnectionError, asyncio.TimeoutError) as err:
        _LOG.error("Connection error for %s: %s", entity.id, err)
        return StatusCodes.SERVICE_UNAVAILABLE

    # Push a state refresh shortly after the command
    loop.call_later(2, lambda: loop.create_task(_update_device_attributes(entity.id)))
    return StatusCodes.OK


# ----------------------------------------------------------------------------
# Setup flow
# ----------------------------------------------------------------------------

async def driver_setup_handler(msg: ucapi.SetupDriver) -> ucapi.SetupAction:
    """Handle the driver setup process started by the remote."""
    if isinstance(msg, ucapi.DriverSetupRequest):
        address = str(msg.setup_data.get("address", "")).strip()
        if not address:
            _LOG.warning("Setup: no address entered")
            return ucapi.SetupError(ucapi.IntegrationSetupError.OTHER)

        try:
            port = int(msg.setup_data.get("port", pjlink.DEFAULT_PORT))
        except (TypeError, ValueError):
            port = pjlink.DEFAULT_PORT
        password = str(msg.setup_data.get("password", ""))

        client = PJLinkClient(address, port=port, password=password, timeout=6)
        try:
            results = await client.transaction([
                ("POWR", "?"),
                ("NAME", "?"),
                ("INF1", "?"),
                ("INF2", "?"),
            ])
        except PJLinkError as err:
            _LOG.error("Setup: PJLink error: %s", err)
            code = (
                ucapi.IntegrationSetupError.AUTHORIZATION_ERROR
                if err.code == "ERRA"
                else ucapi.IntegrationSetupError.OTHER
            )
            return ucapi.SetupError(code)
        except (OSError, ConnectionError, asyncio.TimeoutError) as err:
            _LOG.error("Setup: cannot connect to %s:%s: %s", address, port, err)
            return ucapi.SetupError(ucapi.IntegrationSetupError.CONNECTION_REFUSED)

        _power, name, inf1, inf2 = results
        parts = [p for p in (inf1, inf2) if isinstance(p, str) and p]
        device_name = (
            (name if isinstance(name, str) and name else None)
            or " ".join(parts)
            or f"Projector {address}"
        )

        config = {
            "address": address,
            "port": port,
            "password": password,
            "name": device_name,
        }

        configs = [c for c in _load_config() if c.get("address") != address]
        configs.append(config)
        _save_config(configs)

        entity_id = _entity_id(config)
        if api.available_entities.contains(entity_id):
            api.available_entities.remove(entity_id)
        _register_device(config)

        _LOG.info("Setup complete for %s (%s)", device_name, address)
        return ucapi.SetupComplete()

    if isinstance(msg, ucapi.AbortDriverSetup):
        _LOG.info("Setup aborted: %s", msg.error)
        return ucapi.SetupError()

    return ucapi.SetupError()


# ----------------------------------------------------------------------------
# Remote events
# ----------------------------------------------------------------------------

@api.listens_to(ucapi.Events.CONNECT)
async def on_connect() -> None:
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_disconnect() -> None:
    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    for entity_id in entity_ids:
        if entity_id in _devices:
            _subscribed.add(entity_id)
            loop.create_task(_update_device_attributes(entity_id))
    _ensure_poll_task()


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    for entity_id in entity_ids:
        _subscribed.discard(entity_id)


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_enter_standby() -> None:
    global _standby  # pylint: disable=global-statement
    _standby = True


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_exit_standby() -> None:
    global _standby  # pylint: disable=global-statement
    _standby = False
    for entity_id in list(_subscribed):
        loop.create_task(_update_device_attributes(entity_id))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(level=os.getenv("UC_LOG_LEVEL", "INFO").upper())

    for config in _load_config():
        _register_device(config)

    driver_json = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "driver.json")
    await api.init(driver_json, driver_setup_handler)


if __name__ == "__main__":
    loop.run_until_complete(main())
    loop.run_forever()
