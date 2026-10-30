# Steam cache with Apache Traffic Server

A standalone alternative to `steam-varnish/`. ATS 10.2.1 handles the client
connection, cache, origin fetch, and redirect following in one container.

```text
Steam client -> ATS :8080 (published as :80) -> client's requested CDN
                 |                              -> redirect destination
                 +-> fixed-size ats-cache.bin
```

The initial origin is selected by the client's `Host` header, including an
optional port. ATS preserves Host, path, and query string, including signed
parameters. Different hosts and ports have separate cache entries.

## Build and run

```sh
cd steam-ats
cp .env.example .env
# Choose CACHE_DIR and CACHE_SIZE for the target disk.
docker compose up -d --build --wait
```

Configure your client-facing DNS separately:

```text
lancache.steamcontent.com A <CACHE_SERVER_LAN_IP>
```

Only the HTTP port is published. Steam uses the discovery name to find the cache
IP, then supplies the actual CDN hostname in Host when downloading content.
For example, `Host: cache1.example.com:8081` fetches
`http://cache1.example.com:8081` with the original path and query string.

ATS uses the independent `UPSTREAM_DNS` resolvers for origin and redirect
destinations. Their answers must point at the actual CDNs, not the cache.
ATS also reads `/etc/hosts`, so Compose `extra_hosts` can pin an origin IP without
changing its Host header or TLS hostname. The fixed-origin remap has been
removed; `remap_required: 0` permits requests using their original authority.
With no remap changing that authority, ATS retains the client's upstream Host.
Remove the old `UPSTREAM_HOST`,
`UPSTREAM_PORT`, and `UPSTREAM_SCHEME` settings from existing `.env` files.

The discovery hostname itself does not select a CDN for content downloads. It
can be used for the local health endpoints.

The default cache file reserves **100 GiB of real disk space** at startup. Set
the size before running. Use a dedicated directory for ATS storage. To compare
both implementations concurrently, give them different host ports; Steam's
normal discovery connection expects port 80.

Build without starting or allocating disk:

```sh
docker compose build
# Equivalent image target:
docker build --target ats -t steam-lancache-ats:local .
```

The Dockerfile pins the ATS source version and SHA-512 checksum. Compiler and
development dependencies stay in build stages. Runtime uses Debian trixie,
ATS, its standard plugins, and the small redirect guard described below.

## Read-while-writer and cache lock contention

All four settings required by ATS for read-while-writer are explicitly enabled:

```yaml
records:
  cache:
    enable_read_while_writer: 1
    max_doc_size: 0
  http:
    background_fill_active_timeout: 0
    background_fill_completed_threshold: 0.0
```

Once headers establish that the response is cacheable and its first fragment is
available, other clients can read the object while it is still being fetched.
Background fill allows that fetch to continue if the initiating client leaves.
The origin's ordinary inactivity timeout still applies.

The read retries and write-lock failure policy are:

```yaml
records:
  http:
    cache:
      max_open_read_retries: 100
      open_read_retry_time: 50
      open_write_fail_action: 5
```

**Action 5 retries a cache read after a write-lock failure**, allowing requests
to join the active writer instead of immediately issuing another origin fetch
or returning a lock-contention error. The read retry budget is 100 attempts at
50 ms intervals. This reduces duplicate fetches; it is not an unlimited wait or
a guarantee that every request joins one fetch under all origin failure modes.
This ATS release has read-retry controls rather than a separate
`open_read_fail_action` setting.

The first-fragment/mutex retries are separately configured through
`cache.read_while_writer.max_retries` and `cache.read_while_writer_retry.delay`,
also 100 attempts at 50 ms. The cache fragment target is 1 MiB.
Non-chunk and authenticated requests bypass caching and these contention waits.

## Redirects

ATS's `http.number_of_redirections` enables native following, with five hops by
default. `http.redirect_use_orig_cache_key: 1` stores the final response under
the original lookup key. Later requests therefore reuse the downloaded object
without visiting the redirect chain again. Host and TLS SNI follow the new
destination; HTTPS certificates and hostnames are verified.

The included **header-only `redirect_guard.so` plugin** resolves relative
Location values using libcurl's URL parser and rejects repeated destinations,
missing Location headers, non-HTTP(S) destinations, and URL credentials with
502. It does not fetch anything or process response bodies. ATS still performs
all network I/O, redirect chasing, and caching.

This guard addresses an observed ATS 10.2.1 interaction: a self-redirect with
the original cache key and read retries can wait for the cache object its own
transaction is writing, before reaching the native redirect hop limit. The
guard rejects that cycle before another lookup. It also gives path-relative
and query-only redirects consistent URL resolution.

For a chain of distinct URLs that exceeds the hop budget, native ATS returns
the last redirect to the client. Intermediate redirects are not cached.
Authenticated requests (Authorization or Cookie) are passed uncached and their
redirects are returned to the client, preventing automatic forwarding of those
credentials to another origin.

The default redirect-address policy follows publicly routable destinations.
`REDIRECT_ACTIONS` can be changed for a private origin; the integration tests
explicitly enable private addresses on their isolated Docker network.

## Storage and cache policy

- `/var/lib/lancache/ats-cache.bin` is preallocated with `posix_fallocate`.
  Insufficient space fails startup. File size never grows beyond `CACHE_SIZE`.
- This deployment accepts sizes of at least 256 MiB in multiples of 128 MiB.
  The existing size is checked on startup. Stop ATS and remove only its disposable
  cache file before changing the size.
- ATS manages its object store, metadata, and eviction within the file. Its
  native disk cache can survive graceful restarts; startup does not clear it.
  Cache-format or storage-layout changes can invalidate it. This differs from
  Varnish's non-persistent file backend.
- RAM cache defaults to 256 MiB and can be disabled with `RAM_CACHE_SIZE=0`.
  Indexes, worker memory, and filesystem page cache are additional memory uses.
- Only `/depot/<id>/chunk/<hex-hash>` responses are eligible for caching.
  Manifests and other URLs pass through. ATS honors origin freshness and cache
  restrictions; `CACHE_TTL_SECONDS` supplies fallback freshness when origin
  freshness headers are absent.
- On a chunk cache miss, the origin Range/If-Range headers are removed, so ATS
  fetches a complete object and serves the client's requested range. Warm ranges
  and HEAD requests use cached objects. A cold HEAD does not proactively fill
  the cache as the Varnish variant does.
- Partial origin responses, errors, and redirects are not stored as complete
  chunks. The original authority (host and port), path, and query remain part of
  the cache identity.
- `X-Cache` reports ATS's lookup result: `miss`, `hit-fresh`, `hit-stale`, `none`,
  or `skipped`. `/_health` and `/server-status` are local health responses.

## Settings and inspection

Deployment settings are in `.env.example` and `config/records.yaml`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_DIR` | `./cache` | Host directory for the ATS object store |
| `CACHE_SIZE` | `100G` | Preallocated fixed disk capacity |
| `RAM_CACHE_SIZE` | `256M` | Optional RAM object cache; `0` disables it |
| `CACHE_TTL_SECONDS` | `2592000` | Fallback freshness: 30 days |
| `MAX_REDIRECTS` | `5` | Native redirect hop limit |
| `OPEN_READ_RETRIES` / `OPEN_READ_RETRY_MS` | `100` / `50` | Cache open-read retry policy |
| `RWW_RETRIES` / `RWW_RETRY_MS` | `100` / `50` | First-fragment/read-while-writer retries |
| `ATS_THREADS` | `4` | ATS network worker threads |

```sh
docker compose ps
docker compose logs ats
docker compose exec ats traffic_ctl config get \
  proxy.config.cache.enable_read_while_writer \
  proxy.config.http.cache.open_write_fail_action
docker compose exec ats traffic_ctl metric match cache
docker compose exec ats tail -f /opt/ats/var/log/trafficserver/access.log
docker compose exec ats tail -f /opt/ats/var/log/trafficserver/diags.log
```

## Integration tests

```sh
sh tests/run.sh
```

The suite builds and runs the real ATS image against counted local HTTP/HTTPS
origins. It checks client-selected hosts and ports, separation of cache entries
between origins, cold and warm ranges, cache-key retention
through redirects, relative URLs, cycles and hop limits, TLS verification,
query separation, uncacheable responses, truncation recovery, and concurrent
miss coalescing. A throttled 8 MiB response proves a second reader gets data
**before the origin completes**, including after the first client disconnects.
The runner also prints ATS's `cache.read_busy.success` counter, checks the
active retry configuration, and verifies a cached object survives a graceful
restart with RAM caching disabled.

Tests allocate a disposable 256 MiB file, publish no host ports, and remove the
test containers and volume on exit. Test certificates are confined to test image
stages. These checks do not constitute a full Steam client game-download test.

References: [ATS read-while-writer and open-read retries](https://docs.trafficserver.apache.org/en/10.2.x/admin-guide/configuration/cache-basics.en.html#reducing-origin-server-requests-avoiding-the-thundering-herd),
[ATS records](https://docs.trafficserver.apache.org/en/10.2.x/admin-guide/files/records.yaml.en.html),
[ATS fixed-file storage](https://docs.trafficserver.apache.org/en/10.2.x/admin-guide/files/storage.config.en.html).
