"""Validate configuration, render templates, and exec the container's daemon."""

import ipaddress
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


def integer(name, default, minimum, maximum):
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return str(value)


def size(name, default):
    value = os.environ.get(name, default).upper()
    match = re.fullmatch(r"([1-9][0-9]*)([KMGT]?)", value)
    if not match:
        raise ValueError(f"{name} must be bytes or an integer with a K/M/G/T suffix")
    result = int(match[1]) * 1024 ** (" KMGT".index(match[2]) if match[2] else 0)
    if result < 16 * 1024 * 1024 or result % os.sysconf("SC_PAGE_SIZE"):
        raise ValueError(f"{name} must be at least 16M and a multiple of the page size")
    return result


def authority(name, default):
    value = os.environ.get(name, default)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?", value):
        raise ValueError(f"{name} must be a hostname or IPv4 address, optionally with a port")
    if ":" in value and not 1 <= int(value.rsplit(":", 1)[1]) <= 65535:
        raise ValueError(f"Invalid port in {name}")
    return value


def render(source, destination, values):
    text = Path(source).read_text()
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", str(value))
    if re.search(r"@@[A-Z_]+@@", text):
        raise ValueError(f"Unresolved template variable in {source}")
    Path(destination).write_text(text)


def nginx():
    upstream_host = authority("UPSTREAM_HOST", "dl.steam.clngaa.com")
    upstream_address = authority("UPSTREAM_ADDRESS", upstream_host)
    scheme = os.environ.get("UPSTREAM_SCHEME", "http")
    if scheme not in ("http", "https"):
        raise ValueError("UPSTREAM_SCHEME must be http or https")
    resolvers = os.environ.get("UPSTREAM_DNS", "1.1.1.1 1.0.0.1").replace(";", " ").split()
    if not resolvers:
        raise ValueError("UPSTREAM_DNS must contain at least one IPv4 DNS server")
    for resolver in resolvers:
        ipaddress.IPv4Address(resolver)
    render("/etc/lancache/nginx/nginx.conf", "/etc/nginx/nginx.conf", {
        "UPSTREAM_HOST": upstream_host,
        "UPSTREAM_ADDRESS": upstream_address,
        "UPSTREAM_SCHEME": scheme,
        "UPSTREAM_TLS_NAME": upstream_host.split(":", 1)[0],
        "UPSTREAM_DNS": " ".join(resolvers),
        "MAX_REDIRECTS": integer("MAX_REDIRECTS", "5", 0, 8),
    })
    subprocess.run(["nginx", "-t"], check=True)
    os.execvp("nginx", ["nginx", "-g", "daemon off;"])


def varnish():
    cache_bytes = size("CACHE_SIZE", "100G")
    transient_bytes = size("TRANSIENT_SIZE", "256M")
    cache_dir = Path("/var/lib/lancache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "cache.bin"
    with cache_path.open("a+b") as cache_file:
        current_size = os.fstat(cache_file.fileno()).st_size
        if current_size not in (0, cache_bytes):
            raise ValueError(
                f"{cache_path} is {current_size} bytes, but CACHE_SIZE is {cache_bytes}. "
                "Stop the stack and remove this disposable cache file before resizing."
            )
        # Reserve real space, failing immediately on ENOSPC or unsupported filesystems.
        os.posix_fallocate(cache_file.fileno(), 0, cache_bytes)
        user = pwd.getpwnam("varnish")
        os.fchown(cache_file.fileno(), user.pw_uid, user.pw_gid)
        os.fchmod(cache_file.fileno(), 0o600)
    render("/etc/lancache/default.vcl", "/etc/varnish/default.vcl", {
        "CACHE_TTL_SECONDS": integer("CACHE_TTL_SECONDS", "2592000", 1, 315360000),
    })
    print(f"Using {cache_path}: {cache_bytes} allocated bytes; cache starts cold", flush=True)
    os.execvp("varnishd", [
        "varnishd", "-F", "-a", ":80", "-n", "/var/lib/varnish",
        "-T", "127.0.0.1:6082", "-f", "/etc/varnish/default.vcl",
        "-s", f"disk=file,{cache_path},{cache_bytes}",
        "-s", f"Transient=malloc,{transient_bytes}",
        "-p", "transit_buffer=1M",
    ])


if __name__ == "__main__":
    try:
        {"nginx": nginx, "varnish": varnish}[sys.argv[1]]()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Configuration/startup error: {error}")
