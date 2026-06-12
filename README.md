# KeyUp

**Web-based DMR client. Install Docker, open browser, transmit.**

KeyUp connects directly to BrandMeister via the Homebrew protocol — no Pi-Star, no hardware hotspot, no app to install. A modern browser is all you need.

![Status](https://img.shields.io/badge/status-early%20development-orange)
![License](https://img.shields.io/badge/license-MIT-blue)

## What it does

- Full DMR TX/RX via BrandMeister, directly from the browser
- Rich contact display: name, QTH, city, country from RadioID — in real time
- Live call history with per-contact details
- TalkGroup management
- Clean, mobile-friendly UI

## What it does NOT do (yet)

- D-Star, YSF, P25, NXDN, M17 — multi-network is on the roadmap, DMR first
- Private calls (group calls only in v1)

## Quick start

```bash
git clone https://github.com/camammoli/keyup
cd keyup
cp config.example.yml config.yml
# edit config.yml with your callsign, DMR ID and BrandMeister password
docker compose up
```

Open `http://localhost:8080` in your browser.

## Configuration

```yaml
# config.yml
callsign: LU2MCA
dmr_id: 7221084
essid: 1                        # 1-99, differentiates this client from your radio
bm_password: your_password_here
bm_server: bm2201.brandmeister.network   # closest BrandMeister server
bm_port: 62031
talkgroup: 7229
```

Find your closest BrandMeister server at [brandmeister.network](https://brandmeister.network).

## Architecture

```
Browser  ──WebRTC audio──┐
Browser  ──WebSocket─────┤
                         │
                    FastAPI server
                         │
              ┌──────────┴──────────┐
         DMR engine            Audio bridge
    (Homebrew protocol)     (AMBE ↔ PCM via mbelib)
              │
         BrandMeister
```

## Stack

- **Backend:** Python 3.11, FastAPI, asyncio
- **Audio codec:** mbelib (AMBE+2 software decoder)
- **Browser audio:** WebRTC (aiortc)
- **Contact info:** RadioID API, BrandMeister API
- **Deployment:** Docker

## Roadmap

- [x] Project structure
- [x] Homebrew protocol (BrandMeister connection)
- [ ] AMBE decode/encode via mbelib
- [ ] WebRTC audio bridge (browser TX/RX)
- [ ] RadioID contact display
- [ ] TalkGroup management
- [ ] Call history
- [ ] Multi-network support (TGIF, FreeDMR) — **future**
- [ ] D-Star, YSF, M17 — **future**

## Requirements

- Docker + Docker Compose
- A valid DMR ID ([radioid.net](https://radioid.net))
- BrandMeister account + hotspot password

## Legal note

KeyUp uses [mbelib](https://github.com/szechyjs/mbelib) for AMBE+2 decoding.
AMBE is a proprietary codec by DVSI. mbelib is a reverse-engineered implementation
used by the amateur radio community. Use is your responsibility.

## License

MIT — do what you want, keep the attribution.

## Contributing

PRs welcome. Open an issue before large changes.

---

*LU2MCA — Mendoza, Argentina*
