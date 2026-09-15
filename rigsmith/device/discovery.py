"""Find a HeadRush device on the local network.

Three strategies, tried in order of decreasing confidence:

1. **mDNS hostname** - the documented path. The device answers to
   ``headrushcore.local`` once Global Settings > Wi-Fi > HeadRush Remote is On.
2. **Zeroconf service browse** - catches the device when the ``.local`` name
   does not resolve (common on Windows without Bonjour installed) by walking
   advertised ``_http._tcp`` services and matching on name.
3. **Subnet sweep** - last resort. Probes each host on the local /24 and
   fingerprints the response. Slower, but it works when mDNS is blocked
   entirely, which happens on mesh networks and guest VLANs.

An explicit ``RIGSMITH_DEVICE`` setting short-circuits all three.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field

import httpx

from ..config import ASSUMED_HTTP_PORT, KNOWN_HOSTNAMES, get_settings

#: Substrings that identify the Remote Editor in an HTTP response body or
#: headers. Deliberately broad: the exact markers in the served bundle are
#: unknown until Phase 1 capture runs against real hardware, so we match on
#: anything HeadRush-ish rather than on a guessed exact string.
FINGERPRINT_MARKERS = (
    "headrush",
    "head rush",
    "inmusic",
    "remote editor",
)

#: How a candidate was found, ordered most to least trustworthy.
SOURCE_RANK = {"configured": 0, "hostname": 1, "zeroconf": 2, "sweep": 3}


@dataclass
class DeviceInfo:
    """A located HeadRush device, or a strong candidate for one."""

    host: str
    port: int = ASSUMED_HTTP_PORT
    hostname: str | None = None
    address: str | None = None
    source: str = "sweep"
    scheme: str = "http"
    confirmed: bool = False
    """True when the HTTP response actually fingerprinted as HeadRush."""
    title: str | None = None
    server: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        if (self.scheme, self.port) in (("http", 80), ("https", 443)):
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"

    def describe(self) -> str:
        bits = [self.base_url]
        if self.address and self.address != self.host:
            bits.append(f"({self.address})")
        bits.append(f"via {self.source}")
        bits.append("confirmed" if self.confirmed else "unconfirmed")
        if self.title:
            bits.append(f'title="{self.title}"')
        return " ".join(bits)


def _fingerprint(text: str, headers: dict[str, str]) -> list[str]:
    """Return the markers that matched, so callers can show their working."""
    haystack = text[:20000].lower()
    header_blob = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    return [m for m in FINGERPRINT_MARKERS if m in haystack or m in header_blob]


def _extract_title(text: str) -> str | None:
    low = text.lower()
    start = low.find("<title")
    if start == -1:
        return None
    start = low.find(">", start)
    end = low.find("</title>", start)
    if start == -1 or end == -1:
        return None
    return text[start + 1 : end].strip() or None


async def probe_host(
    host: str,
    *,
    port: int = ASSUMED_HTTP_PORT,
    scheme: str = "http",
    timeout: float = 2.0,
    client: httpx.AsyncClient | None = None,
) -> DeviceInfo | None:
    """Fetch ``/`` from a candidate and decide whether it looks like a Core.

    Returns a :class:`DeviceInfo` for anything that answers HTTP, with
    ``confirmed`` set only when the response fingerprints as HeadRush. An
    unconfirmed result is still worth returning - it means *something* is
    serving there - so the caller can report near-misses rather than a bare
    "not found".
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    info = DeviceInfo(host=host, port=port, scheme=scheme)
    try:
        resp = await client.get(info.base_url + "/")
        headers = dict(resp.headers)
        info.evidence = _fingerprint(resp.text, headers)
        info.title = _extract_title(resp.text)
        info.server = headers.get("server")
        info.confirmed = bool(info.evidence)
        return info
    except (httpx.HTTPError, OSError):
        return None
    finally:
        if owns_client:
            await client.aclose()


def resolve_hostnames(
    hostnames: tuple[str, ...] = KNOWN_HOSTNAMES,
) -> list[tuple[str, str]]:
    """Resolve known ``.local`` names to addresses via the OS resolver.

    Returns ``(hostname, address)`` pairs for whatever resolved. Windows
    resolves ``.local`` through its own mDNS stack, so this often works with
    no extra dependency; when it does not, zeroconf browsing picks up the slack.
    """
    found: list[tuple[str, str]] = []
    for name in hostnames:
        try:
            infos = socket.getaddrinfo(name, None, family=socket.AF_INET)
        except (socket.gaierror, OSError):
            continue
        for entry in infos:
            pair = (name, entry[4][0])
            if pair not in found:
                found.append(pair)
    return found


def browse_zeroconf(timeout: float = 4.0) -> list[tuple[str, str, int]]:
    """Browse mDNS for HTTP services whose name mentions HeadRush.

    Returns ``(name, address, port)`` tuples. Returns an empty list if
    ``zeroconf`` is not installed, so discovery degrades to hostname
    resolution and sweeping rather than failing outright.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    results: list[tuple[str, str, int]] = []

    class _Listener(ServiceListener):
        def _record(self, zc, type_, name) -> None:
            try:
                info = zc.get_service_info(type_, name, timeout=1500)
            except Exception:
                return
            if not info:
                return
            label = f"{name} {info.server or ''}".lower()
            if not any(m in label for m in ("headrush", "inmusic")):
                return
            for packed in info.addresses:
                try:
                    address = socket.inet_ntoa(packed)
                except OSError:
                    continue
                entry = (name, address, info.port or ASSUMED_HTTP_PORT)
                if entry not in results:
                    results.append(entry)

        def add_service(self, zc, type_, name) -> None:
            self._record(zc, type_, name)

        def update_service(self, zc, type_, name) -> None:
            self._record(zc, type_, name)

        def remove_service(self, zc, type_, name) -> None:
            pass

    zc = Zeroconf()
    try:
        listener = _Listener()
        browsers = [
            ServiceBrowser(zc, "_http._tcp.local.", listener),
            ServiceBrowser(zc, "_https._tcp.local.", listener),
        ]
        time.sleep(timeout)
        for browser in browsers:
            browser.cancel()
    finally:
        zc.close()
    return results


def local_subnets() -> list[ipaddress.IPv4Network]:
    """Best-effort list of the /24 networks this machine sits on.

    Loopback, link-local, public, and the WSL/Hyper-V ranges are skipped -
    sweeping those is pure latency.
    """
    nets: list[ipaddress.IPv4Network] = []
    addresses: set[str] = set()

    try:
        hostname = socket.gethostname()
        addresses.update(
            entry[4][0]
            for entry in socket.getaddrinfo(hostname, None, family=socket.AF_INET)
        )
    except (socket.gaierror, OSError):
        pass

    # A UDP "connect" to a public address reveals the primary egress
    # interface without actually sending a packet.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass

    wsl_range = ipaddress.IPv4Network("172.16.0.0/12")
    for raw in addresses:
        try:
            addr = ipaddress.IPv4Address(raw)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or not addr.is_private:
            continue
        if addr in wsl_range:  # overwhelmingly WSL/Docker on Windows hosts
            continue
        net = ipaddress.IPv4Network(f"{addr}/24", strict=False)
        if net not in nets:
            nets.append(net)
    return nets


async def sweep(
    network: ipaddress.IPv4Network,
    *,
    port: int = ASSUMED_HTTP_PORT,
    timeout: float = 1.0,
    concurrency: int = 128,
) -> list[DeviceInfo]:
    """Probe every host in ``network`` and return everything that answered."""
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency)

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, limits=limits
    ) as client:

        async def one(ip: ipaddress.IPv4Address) -> DeviceInfo | None:
            async with semaphore:
                info = await probe_host(
                    str(ip), port=port, timeout=timeout, client=client
                )
            if info is not None:
                info.source = "sweep"
                info.address = str(ip)
            return info

        results = await asyncio.gather(
            *(one(ip) for ip in network.hosts()), return_exceptions=True
        )

    return [r for r in results if isinstance(r, DeviceInfo)]


async def discover(
    *,
    include_sweep: bool = True,
    zeroconf_timeout: float = 4.0,
    timeout: float = 2.0,
) -> list[DeviceInfo]:
    """Locate HeadRush devices, best candidate first.

    Confirmed devices sort ahead of unconfirmed ones, then by how trustworthy
    the discovery method was.
    """
    settings = get_settings()
    found: list[DeviceInfo] = []
    seen: set[tuple[str, int]] = set()

    def add(info: DeviceInfo | None) -> None:
        if info is None:
            return
        key = (info.address or info.host, info.port)
        if key in seen:
            return
        seen.add(key)
        found.append(info)

    # 1. An explicitly configured address wins outright.
    if settings.device_host:
        host, port = settings.device_host, ASSUMED_HTTP_PORT
        if ":" in host and not host.startswith("["):
            host, _, raw_port = host.rpartition(":")
            port = int(raw_port)
        info = await probe_host(host, port=port, timeout=timeout)
        if info is None:
            info = DeviceInfo(host=host, port=port)
        info.source = "configured"
        add(info)
        return found

    # 2. Documented .local hostnames.
    for hostname, address in resolve_hostnames():
        info = await probe_host(hostname, timeout=timeout)
        if info is None:
            info = await probe_host(address, timeout=timeout)
        if info is None:
            info = DeviceInfo(host=hostname, address=address)
        info.source = "hostname"
        info.hostname = hostname
        info.address = address
        add(info)

    # 3. Zeroconf service browse.
    for name, address, port in browse_zeroconf(timeout=zeroconf_timeout):
        info = await probe_host(address, port=port, timeout=timeout)
        if info is None:
            info = DeviceInfo(host=address, port=port)
        info.source = "zeroconf"
        info.hostname = name
        info.address = address
        add(info)

    # 4. Sweep, only if nothing has confirmed yet.
    if include_sweep and not any(i.confirmed for i in found):
        for network in local_subnets():
            for info in await sweep(network, timeout=min(timeout, 1.0)):
                add(info)

    found.sort(key=lambda i: (not i.confirmed, SOURCE_RANK.get(i.source, 9)))
    return found


async def find_device(**kwargs) -> DeviceInfo | None:
    """Return the single best device candidate, or ``None`` if none answered."""
    devices = await discover(**kwargs)
    for device in devices:
        if device.confirmed:
            return device
    return devices[0] if devices else None
