"""Black-box ATS checks with real disk storage and counted, throttled origins."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import sys
import time
import unittest
from urllib.request import urlopen

BODY = bytes(range(256)) * 1024


def chunk(case):
    # Each fixture scenario needs its own object: queries no longer isolate keys.
    digest = hashlib.sha1(case.encode()).hexdigest()
    return f"/depot/123/chunk/{digest}?case={case}"


def open_request(path, headers=None, method="GET"):
    connection = http.client.HTTPConnection("ats", 8080, timeout=30)
    connection.request(method, path, headers={"Host": "cache-a.steamcontent.com", **(headers or {})})
    return connection, connection.getresponse()


def request(path, headers=None, method="GET"):
    connection, response = open_request(path, headers, method)
    try:
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def stats():
    with urlopen("http://origin/__stats", timeout=5) as response:
        return json.load(response)


class Integration(unittest.TestCase):
    def test_client_hosts_and_ports_route_and_cache_separately(self):
        path = chunk("host_isolation")
        for host, body in (
            ("cache-a.steamcontent.com", BODY),
            ("cache-b.steamcontent.com", BODY[::-1]),
            ("cache-a.steamcontent.com:8001", BODY[::-1]),
        ):
            with self.subTest(host=host):
                first = request(path, {"Host": host})
                hit = request(path, {"Host": host})
                self.assertEqual((first[0], first[2]), (200, body))
                self.assertNotEqual(first[1]["x-cache"], "hit-fresh")
                self.assertEqual((hit[0], hit[1]["x-cache"], hit[2]), (200, "hit-fresh", body))
        upstream = [r for r in stats()["requests"] if r["path"] == path]
        self.assertEqual([(r["host"], r["port"]) for r in upstream], [
            ("cache-a.steamcontent.com", 80), ("cache-b.steamcontent.com", 80),
            ("cache-a.steamcontent.com:8001", 8001),
        ])
        self.assertGreaterEqual(request(path, {"Host": "unreachable.invalid"})[0], 500)

    def test_lancache_probe_is_local_and_uncached(self):
        before = stats()["counts"]
        for host in ("lancache.steamcontent.com", "unreachable.invalid:12345"):
            for path in ("/lancache-heartbeat", "/lancache-heartbeat?probe=1"):
                for method in ("GET", "HEAD"):
                    with self.subTest(host=host, path=path, method=method):
                        status, headers, body = request(path, {"Host": host}, method=method)
                        self.assertEqual((status, body), (204, b""))
                        self.assertTrue(headers["x-lancache-processed-by"])
                        self.assertEqual(headers["access-control-allow-origin"], "*")
                        self.assertEqual(headers["access-control-expose-headers"], "*")
                        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(stats()["counts"], before)

    def test_full_chunk_and_client_host(self):
        path = chunk("direct")
        first = request(path)
        second = request(path)
        self.assertEqual((first[0], first[2]), (200, BODY))
        self.assertEqual((second[0], second[1]["x-cache"], second[2]), (200, "hit-fresh", BODY))
        upstream = [r for r in stats()["requests"] if r["path"] == path]
        self.assertEqual(len(upstream), 1)
        self.assertEqual(upstream[0]["host"], "cache-a.steamcontent.com")

    def test_cold_range_then_full_and_warm_range(self):
        path = chunk("range")
        first = request(path, {"Range": "bytes=17-1023"})
        self.assertEqual((first[0], first[2]), (206, BODY[17:1024]))
        self.assertEqual(first[1]["content-range"], f"bytes 17-1023/{len(BODY)}")
        full = request(path)
        self.assertEqual((full[0], full[1]["x-cache"], full[2]), (200, "hit-fresh", BODY))
        other = request(path, {"Range": "bytes=4096-8191", "If-Range": '"chunk-v1"'})
        self.assertEqual((other[0], other[2]), (206, BODY[4096:8192]))
        self.assertEqual(stats()["counts"][path], 1)
        self.assertIsNone([r for r in stats()["requests"] if r["path"] == path][0]["range"])

    def test_warm_head(self):
        path = chunk("head")
        request(path)
        head = request(path, method="HEAD")
        self.assertEqual((head[0], head[1]["x-cache"], head[2]), (200, "hit-fresh", b""))
        self.assertEqual(int(head[1]["content-length"]), len(BODY))
        self.assertEqual(stats()["counts"][path], 1)

    def test_redirect_chain_and_original_cache_key(self):
        path = chunk("chain")
        first = request(path)
        self.assertEqual((first[0], first[2]), (200, BODY))
        self.assertNotIn("location", first[1])
        self.assertEqual(request(path + "&token=new-client")[1]["x-cache"], "hit-fresh")
        counts = stats()["counts"]
        for step in (path, "/redirect/start?token=a%2Fb", "/next?case=chain", "/final?token=a%2Fb"):
            self.assertEqual(counts[step], 1, step)

    def test_https_redirect_and_certificate_check(self):
        path = chunk("https")
        response = request(path)
        self.assertEqual((response[0], response[2]), (200, BODY))
        self.assertEqual(request(path)[1]["x-cache"], "hit-fresh")
        self.assertGreaterEqual(request(chunk("badtls"))[0], 500)

    def test_scheme_relative_redirect(self):
        response = request(chunk("cross"))
        self.assertEqual((response[0], response[2]), (200, BODY))

    def test_authenticated_redirect_returned_without_leaking_credentials(self):
        before = stats()["counts"].get("/final?case=cross", 0)
        response = request(chunk("cross") + "&auth=1", {"Authorization": "Bearer fixture-token"})
        self.assertEqual(response[0], 303)
        self.assertEqual(stats()["counts"].get("/final?case=cross", 0), before)

    def test_redirect_loop_is_bounded(self):
        path = chunk("loop")
        response = request(path)
        # Reject a cycle before trying to read our own incomplete cache object.
        self.assertEqual(response[0], 502)
        self.assertEqual(stats()["counts"][path], 1)
        self.assertNotEqual(request(path)[1].get("x-cache"), "hit-fresh")

    def test_redirect_guard_handles_relative_urls_and_invalid_targets(self):
        path = chunk("relative")
        result = request(path)
        self.assertEqual((result[0], result[2]), (200, BODY))
        self.assertEqual(request(path)[1]["x-cache"], "hit-fresh")
        self.assertEqual(stats()["counts"]["/relative/final?token=a%2Fb&ready=1"], 1)
        for case in ("missing", "invalid", "loop_case"):
            with self.subTest(case=case):
                path = chunk(case)
                self.assertEqual(request(path)[0], 502)
                self.assertEqual(stats()["counts"][path], 1)

    def test_native_redirect_hop_limit(self):
        path = chunk("long")
        result = request(path)
        self.assertEqual(result[0], 302)
        self.assertEqual(result[1]["location"], "http://cache-a.steamcontent.com/long/5")
        self.assertEqual(stats()["counts"][path], 1)
        self.assertEqual(sum(v for k, v in stats()["counts"].items() if k.startswith("/long/")), 5)

    def test_truncated_response_is_not_cached(self):
        path = chunk("truncated")
        try:
            first = request(path)
            self.assertGreaterEqual(first[0], 500)
        except http.client.IncompleteRead:
            pass
        second = request(path)
        self.assertEqual((second[0], second[2]), (200, BODY))
        self.assertEqual(request(path)[1]["x-cache"], "hit-fresh")
        self.assertEqual(stats()["counts"][path], 2)

    def test_uncacheable_errors_and_private_responses(self):
        for case, expected in (("error", 503), ("private", 200), ("cookie", 200)):
            with self.subTest(case=case):
                path = chunk(case)
                for _ in range(2):
                    self.assertEqual(request(path)[0], expected)
                self.assertEqual(stats()["counts"][path], 2)

    def test_manifest_range_passes_through(self):
        path = "/depot/123/manifest/456/5?case=pass"
        for _ in range(2):
            response = request(path, {"Range": "bytes=10-29"})
            self.assertEqual((response[0], response[2]), (206, BODY[10:30]))
        self.assertEqual(stats()["counts"][path], 2)

    def test_query_variants_share_cache_and_preserve_upstream_query(self):
        path = chunk("query")
        initial = path + "&token=a%2Fb"
        status, headers, body = request(initial)
        self.assertEqual((status, body), (200, BODY))
        self.assertEqual(headers["x-cache"], "miss")
        base = path.split("?", 1)[0]
        for variant in (path + "&token=a%2Fc", base + "?token=a%2Fc&case=query", base):
            with self.subTest(variant=variant):
                status, headers, body = request(variant)
                self.assertEqual((status, headers["x-cache"], body), (200, "hit-fresh", BODY))
        fetched = [r for r in stats()["requests"] if r["path"].split("?", 1)[0] == base]
        self.assertEqual([r["path"] for r in fetched], [initial])
        # Ignoring queries must not merge different object paths.
        other = request(chunk("query_other") + "&token=a%2Fb")
        self.assertEqual((other[0], other[1]["x-cache"], other[2]), (200, "miss", BODY))

    def test_query_only_redirect_keeps_upstream_parameters(self):
        path = chunk("query_redirect") + "&token=a%2Fb"
        redirected = chunk("query_redirect") + "&step=done&token=a%2Fb"
        status, _, body = request(path)
        self.assertEqual((status, body), (200, BODY))
        self.assertEqual(request(chunk("query_redirect") + "&token=changed")[1]["x-cache"], "hit-fresh")
        counts = stats()["counts"]
        self.assertEqual((counts[path], counts[redirected]), (1, 1))
    def test_stale_query_redirect_reuses_update_writer(self):
        path = chunk("query_redirect_stale") + "&token=a%2Fb"
        redirected = chunk("query_redirect_stale") + "&step=done&token=a%2Fb"
        first = request(path)
        self.assertEqual((first[0], first[2]), (200, BODY))
        # The fixture's first final response has max-age=0. Exercise a stale
        # revalidation, rather than a fresh hit with a client no-cache request.
        time.sleep(1.1)
        refreshed = request(path)
        self.assertEqual((refreshed[0], refreshed[2]), (200, BODY))
        self.assertEqual(request(chunk("query_redirect_stale") + "&token=new")[1]["x-cache"], "hit-fresh")
        counts = stats()["counts"]
        self.assertEqual((counts[path], counts[redirected]), (2, 2))

    def test_concurrent_misses_before_origin_headers(self):
        path = chunk("collapse")
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda index: request(path + f"&token={index}"), range(12)))
        for response in results:
            self.assertEqual((response[0], response[2]), (200, BODY))
        base = path.split("?", 1)[0]
        self.assertEqual(sum(v for k, v in stats()["counts"].items() if k.split("?", 1)[0] == base), 1)

    def test_read_while_writer_and_writer_disconnect(self):
        path = chunk("abandon")
        writer_connection, writer = open_request(path)
        # RWW requires the first 1 MiB fragment. Keep draining the initiating
        # client until it exists, before testing a paused/disconnected writer.
        self.assertEqual(writer.read(2 * 1024 * 1024), BODY * 8)
        reader_path = path + "&token=another-client"
        reader_connection, reader = open_request(reader_path)
        first = reader.read(65536)
        self.assertEqual(first, BODY[:65536])
        self.assertNotIn(path, stats()["completed"], "reader must receive data while origin is still writing")
        writer.close()
        writer_connection.close()
        try:
            self.assertEqual(first + reader.read(), BODY * 32)
        finally:
            reader_connection.close()
        self.assertEqual(stats()["counts"][path], 1)
        self.assertEqual(stats()["counts"].get(reader_path, 0), 0)
        self.assertEqual(request(path)[1]["x-cache"], "hit-fresh")

    def test_cache_file_fixed_and_preallocated(self):
        stat = Path("/cache/ats-cache.bin").stat()
        self.assertEqual(stat.st_size, 256 * 1024 * 1024)
        self.assertGreaterEqual(stat.st_blocks * 512, stat.st_size)

    def test_health(self):
        self.assertEqual(request("/_health")[0], 200)
        self.assertEqual(request("/server-status")[0], 200)


if __name__ == "__main__":
    if sys.argv[1:] == ["seed"]:
        path = chunk("restart")
        request(path)
        assert request(path)[1]["x-cache"] == "hit-fresh"
        # Let ATS's disk-directory synchronization run before a graceful restart.
        time.sleep(12)
        print("PASS: restart object cached and directory sync interval elapsed")
    elif sys.argv[1:] == ["restart"]:
        path = chunk("restart")
        before = stats()["counts"][path]
        assert request(path + "&token=after-restart")[1]["x-cache"] == "hit-fresh", "disk cache should survive graceful restart"
        assert stats()["counts"][path] == before
        print("PASS: fixed-file cache survives graceful restart")
    else:
        unittest.main(verbosity=2)
