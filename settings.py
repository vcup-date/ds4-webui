"""Persistent settings for ds4-web.

Loaded once at startup and re-saved on every POST. The agent process is
restarted when any restart-required key changes.
"""

from __future__ import annotations
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Set


# By default, look for the ds4-agent binary, model, and MTP file in the parent
# directory of this wrapper (i.e. the repo root). Override via env var if you
# keep the binaries elsewhere.
_HERE = Path(__file__).resolve().parent
DS4_DIR = Path(os.environ.get("DS4_DIR") or _HERE.parent)
SETTINGS_PATH = _HERE / "settings.json"

# Keys that require the agent to be fully restarted when changed. Everything
# the agent reads only at process start (CLI flags) lives here; UI-only keys
# (theme, autosave) apply instantly without a restart.
RESTART_KEYS: Set[str] = {
    "agent_path",
    "model",
    "ctx_size",
    "max_tokens",
    "think_mode",
    "mtp_path",
    "mtp_enabled",
    "mtp_draft",
    "mtp_margin",
    "system_extra",
    "temp",
    "top_p",
    "min_p",
    "seed",
    "backend",
    "threads",
    "quality",
    "warm_weights",
    "power",
}


@dataclass
class Settings:
    agent_path: str = str(DS4_DIR / "ds4-agent")
    model: str = str(DS4_DIR / "ds4flash.gguf")
    ctx_size: int = 200_000
    # Max tokens generated per turn before the agent stops (ds4-agent -n).
    max_tokens: int = 50_000
    think_mode: str = "normal"  # off | normal | max

    mtp_enabled: bool = True
    mtp_path: str = str(DS4_DIR / "gguf" / "DeepSeek-V4-Flash-MTP-Q4K-Q8_0-F32.gguf")
    mtp_draft: int = 1
    mtp_margin: float = 3.0  # --mtp-margin verifier margin

    system_extra: str = ""

    # Sampling. NOTE: ds4-agent only supports these four knobs — there is no
    # top-k / repeat-penalty / mirostat / grammar like llama.cpp, so we do not
    # expose controls that the agent would silently ignore.
    temp: float = 1.0
    top_p: float = 1.0
    min_p: float = 0.05
    # Optional sampling seed; 0 means engine default (random each run).
    seed: int = 0

    # Performance / backend (advanced). backend "auto" = let the agent detect.
    backend: str = "auto"  # auto | metal | cuda | cpu
    threads: int = 0       # CPU helper threads; 0 = agent default
    quality: bool = False  # --quality: prefer exact kernels
    warm_weights: bool = False  # --warm-weights: pre-touch tensor pages
    # Target GPU duty cycle percentage (--power, 1..100). 100 = full speed;
    # lower throttles the GPU for cooler/quieter runs. 100 = agent default.
    power: int = 100

    ui_theme: str = "dark"

    # Auto-save the session after each turn ends so it shows up in the
    # sessions sidebar without having to click /save manually.
    autosave: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Settings":
        # Only accept known fields; ignore extras.
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    def validate(self) -> None:
        if self.ctx_size < 4096 or self.ctx_size > 1_048_576:
            raise ValueError("ctx_size must be in [4096, 1048576]")
        if self.max_tokens < 256 or self.max_tokens > 1_048_576:
            raise ValueError("max_tokens must be in [256, 1048576]")
        if self.think_mode not in ("off", "normal", "max"):
            raise ValueError("think_mode must be off|normal|max")
        if self.mtp_draft < 1 or self.mtp_draft > 4:
            raise ValueError("mtp_draft must be in [1, 4]")
        if self.mtp_margin < 0 or self.mtp_margin > 64:
            raise ValueError("mtp_margin must be in [0, 64]")
        if self.temp < 0 or self.temp > 5:
            raise ValueError("temp must be in [0, 5]")
        if not (0 < self.top_p <= 1):
            raise ValueError("top_p must be in (0, 1]")
        if not (0 <= self.min_p <= 1):
            raise ValueError("min_p must be in [0, 1]")
        if self.backend not in ("auto", "metal", "cuda", "cpu"):
            raise ValueError("backend must be auto|metal|cuda|cpu")
        if self.threads < 0 or self.threads > 256:
            raise ValueError("threads must be in [0, 256]")
        if self.power < 1 or self.power > 100:
            raise ValueError("power must be in [1, 100]")
        if self.ui_theme not in ("dark", "light"):
            raise ValueError("ui_theme must be dark|light")

    def agent_args(self) -> list[str]:
        """Build the argv (after the agent_path) for spawning ds4-agent."""
        args = [
            "-m", self.model,
            "-c", str(self.ctx_size),
            "-n", str(self.max_tokens),
        ]
        if self.think_mode == "off":
            args.append("--nothink")
        elif self.think_mode == "max":
            args.append("--think-max")
        else:
            args.append("--think")

        if self.mtp_enabled and self.mtp_path:
            args += ["--mtp", self.mtp_path,
                     "--mtp-draft", str(self.mtp_draft),
                     "--mtp-margin", str(self.mtp_margin)]

        if self.system_extra:
            args += ["-sys", self.system_extra]

        # Sampling (the only four knobs ds4-agent exposes).
        args += [
            "--temp", str(self.temp),
            "--top-p", str(self.top_p),
            "--min-p", str(self.min_p),
        ]
        if self.seed:
            args += ["--seed", str(self.seed)]

        # Performance / backend.
        if self.backend and self.backend != "auto":
            args += ["--backend", self.backend]
        if self.threads and self.threads > 0:
            args += ["-t", str(self.threads)]
        if self.quality:
            args.append("--quality")
        if self.warm_weights:
            args.append("--warm-weights")
        if self.power and self.power != 100:
            args += ["--power", str(self.power)]
        return args


def load() -> Settings:
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
            return Settings.from_dict(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            # Corrupt file; fall back to defaults but back the file up.
            try:
                SETTINGS_PATH.rename(SETTINGS_PATH.with_suffix(".json.bak"))
            except OSError:
                pass
    return Settings()


def save(s: Settings) -> None:
    s.validate()
    payload = json.dumps(s.to_dict(), indent=2, sort_keys=True)
    # Atomic write: tmp + fsync + rename. Prevents a half-written file if the
    # process is killed mid-write.
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(SETTINGS_PATH)


def diff_restart(old: Settings, new: Settings) -> bool:
    """True if the difference between old and new requires an agent restart."""
    old_d, new_d = old.to_dict(), new.to_dict()
    for k in RESTART_KEYS:
        if old_d.get(k) != new_d.get(k):
            return True
    return False
