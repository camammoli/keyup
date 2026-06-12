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
    DISCONNECTED = 'disconnected'
    CONNECTING   = 'connecting'
    CONNECTED    = 'connected'
    CLOSING      = 'closing'


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
        tag = data[:4]

        if tag == b'RPTA':
            self._handle_rptack(data)
        elif tag == b'MSTA':
            self._handle_mstack(data)
        elif tag == b'MSTN':
            log.error('Login rejected by BrandMeister')
            self._set_state(HomebrewState.DISCONNECTED)
        elif tag == b'MSTP':
            self._handle_pong(data)
        elif tag == b'DMRD':
            self._handle_dmrd(data)
        else:
            log.debug('Unknown packet: %s', tag)

    def error_received(self, exc):
        log.error('UDP error: %s', exc)

    def connection_lost(self, exc):
        log.warning('Connection lost: %s', exc)
        self._set_state(HomebrewState.DISCONNECTED)

    # ── Login sequence ────────────────────────────────────────────────────────

    def _send_login(self):
        self._send(b'RPTL' + self.cfg.repeater_id_bytes)

    def _handle_rptack(self, data: bytes):
        if self.state == HomebrewState.CONNECTING:
            # First ACK: contains the 4-byte salt for challenge-response
            self._salt = data[6:10]
            log.debug('Got salt: %s', self._salt.hex())
            self._send_key()
        elif self.state == HomebrewState.CONNECTED:
            # ACK after config — all good
            log.debug('Config acknowledged')

    def _send_key(self):
        digest = hashlib.sha256(self._salt + self.cfg.bm_password.encode()).hexdigest()
        self._send(b'RPTK' + self.cfg.repeater_id_bytes + digest.encode())

    def _handle_mstack(self, data: bytes):
        log.info('Login accepted by BrandMeister')
        self._set_state(HomebrewState.CONNECTED)
        self._send_config()
        self._ping_task = asyncio.ensure_future(self._ping_loop())

    def _send_config(self):
        # RPTC config packet: callsign + frequency + TX power + color code + lat/lon + height + location + desc + url + software + package
        cs = self.cfg.callsign.ljust(8).encode()
        payload = (
            b'RPTC' +
            self.cfg.repeater_id_bytes +
            cs +
            b'\x00' * 4 +   # RX freq (not used for softclient)
            b'\x00' * 4 +   # TX freq
            b'\x00' * 4 +   # TX power
            b'\x01' +       # color code 1
            b'\x00' * 8 +   # lat
            b'\x00' * 9 +   # lon
            b'\x00' * 3 +   # height
            b'KeyUp   ' +   # location (8 bytes)
            b'KeyUp web client' + b'\x00' * (20 - 16) +   # description (20 bytes)
            b'https://github.com/camammoli/keyup' + b'\x00' * (124 - 35) +  # URL
            b'KeyUp/1.0' + b'\x00' * (40 - 9) +           # software (40 bytes)
            b'MMDVM_MMDVM_HS_Hat' + b'\x00' * (40 - 18)  # package (40 bytes)
        )
        self._send(payload)

    # ── Ping / keepalive ──────────────────────────────────────────────────────

    async def _ping_loop(self):
        while self.state == HomebrewState.CONNECTED:
            await asyncio.sleep(PING_INTERVAL)
            self._send(b'RPTP' + b'MMDVM' + self.cfg.repeater_id_bytes)
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

        # Track active streams for call start/end detection
        if frame.frame_type == FrameType.DATA_SYNC:
            # Voice call header — new stream starting
            self._active_streams[frame.stream_id] = {
                'src_id': frame.src_id,
                'dst_id': frame.dst_id,
                'start':  time.time(),
            }
        elif frame.stream_id not in self._active_streams:
            # Audio frame without a known stream — ignore
            return

        if self.on_frame:
            self.on_frame(frame)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _send(self, data: bytes):
        if self.transport:
            self.transport.sendto(data)

    def _set_state(self, state: str):
        self.state = state
        log.info('State → %s', state)
        if self.on_state:
            self.on_state(state)

    async def _wait_connected(self):
        while self.state != HomebrewState.CONNECTED:
            await asyncio.sleep(0.1)
