import yaml
from pathlib import Path
from dataclasses import dataclass

@dataclass
class Config:
    callsign: str
    dmr_id: int
    essid: int
    bm_password: str
    bm_server: str
    bm_port: int
    talkgroup: int
    web_port: int
    frequency: int = 438800000

    @property
    def repeater_id(self) -> int:
        # ESSID shifts the ID so multiple clients can coexist on BrandMeister
        return self.dmr_id * 100 + self.essid

    @property
    def repeater_id_bytes(self) -> bytes:
        return self.repeater_id.to_bytes(4, 'big')

def load(path: str = 'config.yml') -> Config:
    with open(path) as f:
        d = yaml.safe_load(f)
    return Config(
        callsign  = d['callsign'].upper(),
        dmr_id    = int(d['dmr_id']),
        essid     = int(d.get('essid', 1)),
        bm_password = d['bm_password'],
        bm_server = d['bm_server'],
        bm_port   = int(d.get('bm_port', 62031)),
        talkgroup  = int(d.get('talkgroup', 91)),
        web_port   = int(d.get('web_port', 8080)),
        frequency  = int(d.get('frequency', 438800000)),
    )
