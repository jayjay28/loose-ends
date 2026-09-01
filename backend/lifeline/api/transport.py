"""§v3 workstream 4 — how a phone finds this engine.

The decided shape: LAN-first, Tailscale for away-from-home, never a data
relay of ours. This module is the engine's side of that bargain — it knows
every door the phone could use (the LAN address, the tailnet name when
Tailscale is up) and it advertises itself over Bonjour so the app can find
the engine without anyone typing an IP address.

The auth gate is what makes advertising safe: a stranger who finds the
service gets 401s on everything but `/pair/claim`, and a claim needs a code
minted on this Mac's screen.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SERVICE_TYPE = "_loose-ends._tcp.local."

_zeroconf = None
_service_info = None


def port() -> int:
    """The port uvicorn bound. The installer exports LIFELINE_PORT alongside
    --port so the process can name its own door; 8000 is the dev default."""
    try:
        return int(os.environ.get("LIFELINE_PORT", "8000"))
    except ValueError:
        return 8000


def lan_ip() -> Optional[str]:
    """This Mac's LAN address. The UDP-connect trick asks the routing table
    which interface reaches out — no packet is actually sent — and works on
    Wi-Fi or Ethernet alike, where `ipconfig getifaddr en0` only knows one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(("192.0.2.1", 9))  # TEST-NET; never actually reached
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        out = subprocess.run(["ipconfig", "getifaddr", "en0"],
                             capture_output=True, text=True, timeout=5)
        ip = out.stdout.strip()
        return ip or None
    except (subprocess.SubprocessError, OSError):
        return None


# Where the Tailscale CLI actually lives. The Mac App Store build ships it
# inside the app bundle and puts nothing on PATH, and a launchd job gets the
# bare system PATH — so `shutil.which` alone reported "no tailnet" on a Mac
# that was actively serving one, and the QR offered only a LAN address that
# stops working the moment you leave the house.
_TAILSCALE_HOMES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    str(Path.home() / "Applications" / "Tailscale.app" / "Contents" / "MacOS" / "Tailscale"),
)


def _tailscale() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_HOMES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def tailnet_url() -> Optional[str]:
    """The HTTPS door Tailscale serve terminates, when the tailnet is up."""
    binary = _tailscale()
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "status", "--json"],
                             capture_output=True, text=True, timeout=5)
        dns = json.loads(out.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
        if dns:
            return f"https://{dns}"
    except Exception:
        pass
    return None


def urls() -> List[str]:
    """Every door, most reliable first. The tailnet name outlives DHCP; the
    LAN address is the fast path at home."""
    doors: List[str] = []
    ts = tailnet_url()
    ip = lan_ip()
    if ip:
        doors.append(f"http://{ip}:{port()}")
    if ts:
        doors.append(ts)
    return doors


def reachable_url() -> str:
    """One best guess, for the QR and the wizard's copy. Best effort — the
    manual code path never depends on this being right."""
    doors = urls()
    return doors[0] if doors else f"http://localhost:{port()}"


def describe() -> Dict[str, Any]:
    return {"urls": urls(), "port": port(),
            "service_type": SERVICE_TYPE.removesuffix(".local.")}


# ---------------------------------------------------------------- bonjour
def advertise() -> bool:
    """Register `_loose-ends._tcp` on the local network. Failure is never
    fatal — an engine that can't advertise is still reachable by URL."""
    global _zeroconf, _service_info
    if os.environ.get("LIFELINE_NO_BONJOUR"):
        return False
    if _zeroconf is not None:
        return True
    ip = lan_ip()
    if ip is None:
        log.info("bonjour: no LAN address — not advertising")
        return False
    try:
        from zeroconf import ServiceInfo, Zeroconf

        host = socket.gethostname().removesuffix(".local")
        _service_info = ServiceInfo(
            SERVICE_TYPE,
            f"Loose Ends on {host}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=port(),
            properties={"v": "1"},
        )
        _zeroconf = Zeroconf()
        _zeroconf.register_service(_service_info)
        log.info("bonjour: advertising %s at %s:%s", SERVICE_TYPE, ip, port())
        return True
    except Exception as exc:  # missing dep, port squatting, weird networks
        log.warning("bonjour: not advertising (%s)", exc)
        _zeroconf, _service_info = None, None
        return False


def withdraw() -> None:
    global _zeroconf, _service_info
    if _zeroconf is not None:
        try:
            if _service_info is not None:
                _zeroconf.unregister_service(_service_info)
            _zeroconf.close()
        except Exception:
            pass
    _zeroconf, _service_info = None, None
