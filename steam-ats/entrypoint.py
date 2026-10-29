"""Allocate ATS storage, validate environment settings, and start Traffic Server."""

import ipaddress
import os
from pathlib import Path
import pwd
import re
import sys

CONFIG = Path("/opt/ats/etc/trafficserver")


def integer(name, default, minimum, maximum):
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def size(name, default):
    value = os.environ.get(name, default).upper()
    match = re.fullmatch(r"([0-9]+)([KMGT]?)", value)
    if not match:
        raise ValueError(f"{name} must be bytes or an integer with a K/M/G/T suffix")
    return int(match[1]) * 1024 ** (" KMGT".index(match[2]) if match[2] else 0)


def main():
    host = os.environ.get("UPSTREAM_HOST", "dl.steam.clngaa.com")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("UPSTREAM_HOST must be a DNS hostname or IPv4 address")
    port = integer("UPSTREAM_PORT", "80", 1, 65535)
    scheme = os.environ.get("UPSTREAM_SCHEME", "http")
    if scheme not in ("http", "https"):
        raise ValueError("UPSTREAM_SCHEME must be http or https")
    resolvers = os.environ.get("UPSTREAM_DNS", "1.1.1.1 1.0.0.1").replace(";", " ").split()
    if not resolvers:
        raise ValueError("UPSTREAM_DNS must contain an IPv4 resolver")
    for resolver in resolvers:
        ipaddress.IPv4Address(resolver)
    actions = os.environ.get("REDIRECT_ACTIONS", "routable:follow,default:reject")
    for rule in actions.split(","):
        if not re.fullmatch(r"(self|loopback|private|multicast|linklocal|routable|default):(follow|reject|return)", rule):
            raise ValueError("Invalid REDIRECT_ACTIONS rule")

    cache_bytes = size("CACHE_SIZE", "100G")
    if cache_bytes < 256 * 1024**2 or cache_bytes % (128 * 1024**2):
        raise ValueError("CACHE_SIZE must be at least 256M and a multiple of 128M")
    cache_dir = Path("/var/lib/lancache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "ats-cache.bin"
    with cache_file.open("a+b") as file:
        old_size = os.fstat(file.fileno()).st_size
        if old_size not in (0, cache_bytes):
            raise ValueError(
                f"{cache_file} is {old_size} bytes, but CACHE_SIZE is {cache_bytes}. "
                "Stop ATS and remove its disposable cache file before resizing."
            )
        os.posix_fallocate(file.fileno(), 0, cache_bytes)
        user = pwd.getpwnam("trafficserver")
        os.fchown(file.fileno(), user.pw_uid, user.pw_gid)
        os.fchmod(file.fileno(), 0o600)

    authority = host if (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    values = {
        "UPSTREAM_URL": f"{scheme}://{authority}",
        "UPSTREAM_DNS": " ".join(resolvers),
        "REDIRECT_ACTIONS": actions,
        "CACHE_BYTES": cache_bytes,
        "RAM_CACHE_BYTES": size("RAM_CACHE_SIZE", "256M"),
        "CACHE_TTL_SECONDS": integer("CACHE_TTL_SECONDS", "2592000", 1, 315360000),
        "MAX_REDIRECTS": integer("MAX_REDIRECTS", "5", 0, 20),
        "OPEN_READ_RETRIES": integer("OPEN_READ_RETRIES", "100", 1, 10000),
        "OPEN_READ_RETRY_MS": integer("OPEN_READ_RETRY_MS", "50", 1, 10000),
        "RWW_RETRIES": integer("RWW_RETRIES", "100", 1, 10000),
        "RWW_RETRY_MS": integer("RWW_RETRY_MS", "50", 1, 10000),
        "ATS_THREADS": integer("ATS_THREADS", "4", 1, 256),
    }
    for template in Path("/etc/lancache").iterdir():
        text = template.read_text()
        for key, value in values.items():
            text = text.replace(f"@@{key}@@", str(value))
        if re.search(r"@@[A-Z_]+@@", text):
            raise ValueError(f"Unresolved template in {template}")
        (CONFIG / template.name).write_text(text)
    print(f"ATS storage: {cache_file}, {cache_bytes} allocated bytes", flush=True)
    os.execvp("traffic_server", ["traffic_server"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"Configuration/startup error: {error}")
