"""
KeyUp — entry point.
Starts the Homebrew DMR connection and the FastAPI web server.
"""

import asyncio
import logging
import os

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server import config as cfg_module
from server.dmr.homebrew import HomebrewProtocol
from server.webrtc.bridge import AudioBridge
from server.api import routes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s  %(message)s',
)
log = logging.getLogger('keyup')


def build_app(cfg) -> FastAPI:
    app = FastAPI(title='KeyUp', version='1.0')

    homebrew = HomebrewProtocol(cfg)
    bridge   = AudioBridge(cfg, homebrew)

    routes.init(homebrew, bridge)
    app.include_router(routes.router)

    # Serve static assets (CSS, JS) — must come after routes
    web_dir = os.path.join(os.path.dirname(__file__), '..', 'web')
    app.mount('/static', StaticFiles(directory=web_dir), name='static')

    @app.on_event('startup')
    async def startup():
        asyncio.ensure_future(homebrew.connect())

    @app.on_event('shutdown')
    async def shutdown():
        await homebrew.disconnect()
        await bridge.close()

    return app


def main():
    cfg = cfg_module.load(os.environ.get('KEYUP_CONFIG', 'config.yml'))
    app = build_app(cfg)
    uvicorn.run(app, host='0.0.0.0', port=cfg.web_port, log_config=None)


if __name__ == '__main__':
    main()
