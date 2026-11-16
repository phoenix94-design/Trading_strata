import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .risk import RiskConfig

ROOT = Path(__file__).resolve().parent.parent


def read_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@dataclass
class Settings:
    platform: str
    mode: str
    cfg: dict
    env: dict
    risk: RiskConfig
    state_dir: Path
    log_dir: Path
    paper_cash: float
    symbols: list = field(default_factory=list)


def load_settings(platform: str, root: Path = ROOT) -> Settings:
    cfg = yaml.safe_load((root / "config" / f"{platform}.yaml").read_text())
    # systemd's EnvironmentFile already exported .env; reading it too makes manual runs work
    env = {**read_env_file(root / ".env"), **os.environ}
    mode = env.get("TRADING_MODE", "paper").lower()
    if mode not in ("paper", "live"):
        raise ValueError("TRADING_MODE must be paper or live")
    state_dir, log_dir = root / "state", root / "logs"
    state_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)
    return Settings(platform, mode, cfg, env, RiskConfig(**cfg.get("risk", {})), state_dir, log_dir,
                    float(cfg.get("paper_cash", 100000)), cfg["symbols"])
