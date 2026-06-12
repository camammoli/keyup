"""
Homebrew / MMDVM protocol — BrandMeister connection.

Packet reference:
  RPTL   → login request
  RPTK   → key (challenge response)
  RPTC   → repeater config
  RPTP   → ping
  DMRD   → DMR data frame (voice/data)
  RPTCL  → close
"""

import asyncio
import hashlib
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

from server.config import Config

log = logging.getLogger('keyup.homebrew')

PING_INTERVAL = 5      # seconds
LOGIN_TIMEOUT = 10     # seconds
MAX_MISSED_PINGS = 3


class Slot(IntEnum):
    TS1 = 0
    TS2 = 1


class FrameType(IntEnum):
    VOICE      = 0
    VOICE_SYNC = 1
    DATA_SYNC  = 2


@dataclass
class DMRFrame:
    sequence:  int
    src_id:    int
    dst_id:    int
    rpt_id:    int
    slot:      Slot
    call_type: int        # 0 = group, 1 = unit-to-unit
    frame_type: FrameType
    stream_id: bytes      # 4 bytes, unique per call
    data:      bytes      # 33 bytes DMR payload (BPTC encoded)

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'DMRFrame':
        # DMRD packet: 53 bytes total
        # [0-3]  "DMRD"
        # [4]    sequence
        # [5-7]  src_id
        # [8-10] dst_id
        # [11-14] rpt_id
        # [15]   flags
        # [16-19] stream_id
        # [20-52] 33 bytes DMR data
        seq       = raw[4]
        src_id    = int.from_bytes(raw[5:8],  'big')
        dst_id    = int.from_bytes(raw[8:11], 'big')
        rpt_id    = int.from_bytes(raw[11:15], 'big')
        flags     = raw[15]
        stream_id = raw[16:20]
        data      = raw[20:53]

        call_type  = (flags >> 7) & 0x01
        frame_type = FrameType((flags >> 4) & 0x03)
        slot       = Slot((flags >> 0) & 0x01)

        return cls(seq, src_id, dst_id, rpt_id, slot, call_type, frame_type, stream_id, data)

    def to_bytes(self, rpt_id_bytes: bytes) -> bytes:
        flags = (self.call_type << 7) | (self.frame_type << 4) | int(self.slot)
        return (
            b'DMRD' +
            bytes([self.sequence]) +
            self.src_id.to_bytes(3, 'big') +
            self.dst_id.to_bytes(3, 'big') +
            rpt_id_bytes +
            bytes([flags]) +
            self.stream_id +
            self.data
        )


class HomebrewState:
    DISCONNECTED   = 'disconnected'
    CONNECTING     = 'connecting'      # sent RPTL, waiting RPTACK #1
    AUTHENTICATING = 'authenticating'  # sent RPTK, waiting RPTACK #2
    CONFIGURING    = 'configuring'     # sent RPTC, waiting RPTACK #3
    CONNECTED      = 'connected'
    CLOSING        = 'closing'


class HomebrewProtocol(asyncio.DatagramProtocol):
    """
    Async UDP client for the BrandMeister Homebrew protocol.

    Usage:
        hb = HomebrewProtocol(config)
        hb.on_frame = my_dmr_handler      # called with DMRFrame on each received voice frame
        hb.on_state = my_state_handler    # called with HomebrewState on state changes
        await hb.connect()
    """

    def __init__(self, cfg: Config):
        self.cfg   = cfg
        self.state = HomebrewState.DISCONNECTED
        self.transport: Optional[asyncio.DatagramTransport] = None

        self._salt: bytes = b''
        self._last_ping = 0.0
        self._missed_pings = 0
        self._ping_task: Optional[asyncio.Task] = None
        self._active_streams: dict[bytes, dict] = {}

        # Callbacks — wire these up before calling connect()
        self.on_frame: Optional[Callable[[DMRFrame], None]] = None
        self.on_state: Optional[Callable[[str], None]]      = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def connect(self):
        loop = asyncio.get_event_loop()
        self._set_state(HomebrewState.CONNECTING)
        log.info('Connecting to %s:%d (repeater ID %d)',
                 self.cfg.bm_server, self.cfg.bm_port, self.cfg.repeater_id)
        await loop.create_datagram_endpoint(
            lambda: self,
            remote_addr=(self.cfg.bm_server, self.cfg.bm_port),
        )
        self._send_login()
        try:
            await asyncio.wait_for(self._wait_connected(), LOGIN_TIMEOUT)
        except asyncio.TimeoutError:
            log.error('Login timeout')
            self._set_state(HomebrewState.DISCONNECTED)

    async def disconnect(self):
        self._set_state(HomebrewState.CLOSING)
        if self._ping_task:
            self._ping_task.cancel()
        self._send(b'RPTCL' + self.cfg.repeater_id_bytes)
        if self.transport:
            self.transport.close()
        self._set_state(HomebrewState.DISCONNECTED)

    def transmit(self, frame: DMRFrame):
        if self.state != HomebrewState.CONNECTED:
            return
        self._send(frame.to_bytes(self.cfg.repeater_id_bytes))

    # ── asyncio.DatagramProtocol ──────────────────────────────────────────────

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if len(data) < 4:
            return
        log.debug('RX %d bytes: %s', len(data), data.hex())

        if data[:6] == b'RPTACK':
            self._handle_rptack(data)
        elif data[:6] == b'MSTNAK':
            log.error('Login rejected by BrandMeister (MSTNAK)')
            self._set_state(HomebrewState.DISCONNECTED)
        elif data[:7] == b'MSTPONG':
            self._handle_pong(data)
        elif data[:4] == b'DMRD':
            self._handle_dmrd(data)
        else:
            log.debug('Unknown packet: %s', data[:6])

    def error_received(self, exc):
        log.error('UDP error: %s', exc)

    def connection_lost(self, exc):
        log.warning('Connection lost: %s', exc)
        self._set_state(HomebrewState.DISCONNECTED)

    # ── Login sequence ────────────────────────────────────────────────────────

    def _send_login(self):
        self._send(b'RPTL' + self.cfg.repeater_id_bytes)

    def _handle_rptack(self, data: bytes):
        # Three-phase handshake: RPTL → RPTACK(salt) → RPTK → RPTACK → RPTC → RPTACK → CONNECTED
        if self.state == HomebrewState.CONNECTING:
            self._salt = data[6:10]
            log.debug('Got salt: %s', self._salt.hex())
            self._set_state(HomebrewState.AUTHENTICATING)
            self._send_key()
        elif self.state == HomebrewState.AUTHENTICATING:
            log.debug('Auth accepted, sending config')
            self._set_state(HomebrewState.CONFIGURING)
            self._send_config()
        elif self.state == HomebrewState.CONFIGURING:
            log.info('Config accepted — connected to BrandMeister')
            self._set_state(HomebrewState.CONNECTED)
            self._ping_task = asyncio.ensure_future(self._ping_loop())
            self._send_rpto()

    def subscribe_tg(self, talkgroup: int):
        """Re-subscribe to a new talkgroup without reconnecting."""
        self.cfg.talkgroup = talkgroup
        if self.state == HomebrewState.CONNECTED:
            self._send_rpto()

    def _send_rpto(self):
        # RPTO tells BrandMeister which TG to push traffic for.
        # Without this, BM routes nothing to a freshly-connected hotspot.
        tg  = self.cfg.talkgroup
        freq = self.cfg.frequency
        opts = (
            f'StartPause=4;TXFrequency={freq};RXFrequency={freq};'
            f'ColorCode=1;TalkGroup={tg};TimeSlot=2;'
        )
        log.info('Subscribing to TG %d via RPTO', tg)
        self._send(b'RPTO' + self.cfg.repeater_id_bytes + opts.encode())

    def _send_key(self):
        # RPTK = "RPTK" (4) + repeater_id (4) + SHA256_raw_bytes (32) = 40 bytes total
        digest = hashlib.sha256(self._salt + self.cfg.bm_password.encode()).digest()
        self._send(b'RPTK' + self.cfg.repeater_id_bytes + digest)

    def _send_config(self):
        # RPTC = "RPTC" (4) + repeater_id (4) + ASCII config (294 bytes) = 302 bytes
        # Format mirrors DroidStar / MMDVM Homebrew spec
        freq = self.cfg.frequency
        cfg_str = '%-8.8s%09u%09u%02u%02u%8.8s%9.9s%03d%-20.20s%-19.19s%c%-124.124s%-40.40s%-40.40s' % (
            self.cfg.callsign,
            freq, freq,                             # RX/TX freq
            1, 1,                                   # TX power, color code
            '0.000000', '00.000000',                # lat, lon
            0,                                      # height
            'KeyUp',                                # location
            'KeyUp web DMR client',                 # description
            '4',                                    # hotspot type (DroidStar standard)
            'https://github.com/camammoli/keyup',   # URL
            '20200922',                             # software ID (matches DroidStar default)
            'MMDVM_MMDVM_HS_Hat',                   # package ID
        )
        self._send(b'RPTC' + self.cfg.repeater_id_bytes + cfg_str.encode())

    # ── Ping / keepalive ──────────────────────────────────────────────────────

    async def _ping_loop(self):
        while self.state == HomebrewState.CONNECTED:
            await asyncio.sleep(PING_INTERVAL)
            self._send(b'RPTPING' + self.cfg.repeater_id_bytes)
            self._last_ping = time.monotonic()
            self._missed_pings += 1
            if self._missed_pings >= MAX_MISSED_PINGS:
                log.warning('BrandMeister not responding to pings, reconnecting')
                await self.disconnect()
                await self.connect()
                return

    def _handle_pong(self, data: bytes):
        self._missed_pings = 0

    # ── DMR data ──────────────────────────────────────────────────────────────

    def _handle_dmrd(self, data: bytes):
        if len(data) < 53:
            return
        frame = DMRFrame.from_bytes(data)

        # Register any new stream (handles mid-call joins too)
        if frame.stream_id not in self._active_streams:
            self._active_streams[frame.stream_id] = {
                'src_id': frame.src_id,
                'dst_id': frame.dst_id,
                'start':  time.time(),
            }

        if self.on_frame:
            self.on_frame(frame)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _send(self, data: bytes):
        if self.transport:
            log.debug('TX %d bytes tag=%s', len(data), data[:7])
            self.transport.sendto(data)

    def _set_state(self, state: str):
        self.state = state
        log.info('State → %s', state)
        if self.on_state:
            self.on_state(state)

    async def _wait_connected(self):
        while self.state not in (HomebrewState.CONNECTED, HomebrewState.DISCONNECTED):
            await asyncio.sleep(0.1)
