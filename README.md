# PJLink Projector Integration for Unfolded Circle Remote Two/3

Control network projectors via the standardized [PJLink protocol](https://pjlink.jbmia.or.jp/english/) from your [Unfolded Circle Remote Two or Remote 3](https://www.unfoldedcircle.com/).

Works with projectors from **Acer, Epson, BenQ, NEC, Sony, Panasonic, Optoma, ViewSonic, Christie, Hitachi, Canon** and many more — any projector that speaks PJLink Class 1 or Class 2 over the network (TCP port 4352).

## Features

The projector appears as a **media player entity** on the remote:

- Power **on / off / toggle** (with warming/cooling state awareness)
- **Source selection** — the input list is read from the projector (incl. Class 2 input names)
- **AV mute** (mute / unmute / toggle)
- State polling every 20 seconds (state, source, mute)
- Extra **simple commands** for activities and macros:
  - `AV_MUTE_ON` / `AV_MUTE_OFF`
  - `VIDEO_MUTE_ON` / `VIDEO_MUTE_OFF`
  - `AUDIO_MUTE_ON` / `AUDIO_MUTE_OFF`
  - `FREEZE_ON` / `FREEZE_OFF` (PJLink Class 2)
- Optional **PJLink password** authentication (MD5)

## Requirements

- A PJLink-capable projector with LAN control / network standby enabled
- A host on your network that runs the integration 24/7 (Raspberry Pi, NAS, home server, …)
- The remote and the integration host must be on the same network (mDNS discovery)

## Installation

### Docker (recommended)

```bash
git clone https://github.com/mabikus/uc-intg-pjlink.git
cd uc-intg-pjlink
docker compose up -d --build
```

### Manual (Python 3.11+)

```bash
git clone https://github.com/mabikus/uc-intg-pjlink.git
cd uc-intg-pjlink
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/driver.py
```

To run it permanently, create a systemd service:

```ini
# /etc/systemd/system/uc-intg-pjlink.service
[Unit]
Description=Unfolded Circle PJLink integration
After=network-online.target

[Service]
WorkingDirectory=/opt/uc-intg-pjlink
ExecStart=/opt/uc-intg-pjlink/.venv/bin/python src/driver.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Setup on the remote

1. Start the integration (see above).
2. On the remote / web configurator: **Settings → Integrations → Add new / Discover**.
   The "PJLink Projector" integration is discovered automatically via mDNS.
3. Enter the projector's IP address (and PJLink password, if you set one).
4. Add the projector entity to your profile and use it in activities.

> Tip: give the projector a fixed IP address (DHCP reservation in your router).

## Example activity (movie night)

In an activity's *On* sequence:

1. `Power On` (media player command)
2. Wait ~30 s (warm-up)
3. `Select Source` → HDMI 1
4. Turn on your streaming box, dim the lights, …

## Related

- [homey-pjlink](https://github.com/mabikus/homey-pjlink) — the same PJLink control as a Homey app

## License

[MIT](LICENSE)
