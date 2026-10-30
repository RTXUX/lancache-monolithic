"""Exercise actual Varnish and nginx containers, including origin request counts."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sys
import unittest
from urllib.request import urlopen

BODY = bytes(range(256)) * 1024
CHUNK = "/depot/123/chunk/0123456789abcdef0123456789abcdef01234567"


def request(path, headers=None, method="GET"):
    connection = http.client.HTTPConnection("varnish", 80, timeout=30)
    try:
        connection.request(method, path, headers={"Host": "cache-a.steamcontent.com", **(headers or {})})
        response = connection.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
    finally:
        connection.close()


def stats():
    with urlopen("http://origin/__stats", timeout=5) as response:
        return json.load(response)


class Integration(unittest.TestCase):
    def test_client_hosts_and_ports_route_and_cache_separately(self):
        path = CHUNK + "?case=host_isolation"
        for host, body in (
            ("cache-a.steamcontent.com", BODY),
            ("cache-b.steamcontent.com", BODY[::-1]),
            ("cache-a.steamcontent.com:8001", BODY[::-1]),
        ):
            with self.subTest(host=host):
                first = request(path, {"Host": host})
                hit = request(path, {"Host": host})
                self.assertEqual((first[0], first[1]["x-cache"], first[2]), (200, "MISS", body))
                self.assertEqual((hit[0], hit[1]["x-cache"], hit[2]), (200, "HIT", body))
        upstream = [r for r in stats()["requests"] if r["path"] == path]
        self.assertEqual([(r["host"], r["port"]) for r in upstream], [
            ("cache-a.steamcontent.com", 80), ("cache-b.steamcontent.com", 80),
            ("cache-a.steamcontent.com:8001", 8001),
        ])
        # An unreachable host must not be hidden by a cached object at this path.
        self.assertGreaterEqual(request(path, {"Host": "unreachable.invalid"})[0], 500)

    def test_full_chunk_and_client_host(self):
        path = CHUNK + "?case=direct"
        first = request(path)
        second = request(path)
        self.assertEqual((first[0], first[1]["x-cache"], first[2]), (200, "MISS", BODY))
        self.assertEqual((second[0], second[1]["x-cache"], second[2]), (200, "HIT", BODY))
        origin_requests = [r for r in stats()["requests"] if r["path"] == path]
        self.assertEqual(len(origin_requests), 1)
        self.assertEqual(origin_requests[0]["host"], "cache-a.steamcontent.com")

    def test_cold_range_then_full_and_other_range(self):
        path = CHUNK + "?case=range"
        first = request(path, {"Range": "bytes=17-1023"})
        self.assertEqual((first[0], first[2]), (206, BODY[17:1024]))
        self.assertEqual(first[1]["content-range"], f"bytes 17-1023/{len(BODY)}")
        full = request(path)
        self.assertEqual((full[0], full[1]["x-cache"], full[2]), (200, "HIT", BODY))
        other = request(path, {"Range": "bytes=4096-8191", "If-Range": '"chunk-v1"'})
        self.assertEqual((other[0], other[1]["x-cache"], other[2]), (206, "HIT", BODY[4096:8192]))
        mismatch = request(path, {"Range": "bytes=4096-8191", "If-Range": '"different"'})
        self.assertEqual((mismatch[0], mismatch[2]), (200, BODY))
        origin_requests = [r for r in stats()["requests"] if r["path"] == path]
        self.assertEqual(len(origin_requests), 1)
        self.assertIsNone(origin_requests[0]["range"])

    def test_head_miss_fills_complete_chunk(self):
        path = CHUNK + "?case=head"
        head = request(path, method="HEAD")
        self.assertEqual((head[0], head[2]), (200, b""))
        self.assertEqual(int(head[1]["content-length"]), len(BODY))
        full = request(path)
        self.assertEqual((full[1]["x-cache"], full[2]), ("HIT", BODY))
        self.assertEqual(stats()["counts"][path], 1)

    def test_redirect_chain_relative_query_and_absolute(self):
        path = CHUNK + "?case=chain"
        first = request(path, {"Range": "bytes=0-127"})
        self.assertEqual((first[0], first[2]), (206, BODY[:128]))
        self.assertEqual(first[1]["x-upstream-redirects"], "4")
        self.assertNotIn("location", first[1])
        self.assertEqual(request(path)[1]["x-cache"], "HIT")
        self.assertEqual(stats()["counts"]["/final?token=a%2Fb"], 1)

    def test_https_redirect_and_certificate_validation(self):
        path = CHUNK + "?case=https"
        response = request(path)
        self.assertEqual((response[0], response[2]), (200, BODY))
        self.assertEqual(response[1]["x-upstream-redirects"], "1")
        self.assertEqual(request(path)[1]["x-cache"], "HIT")
        self.assertEqual(request(CHUNK + "?case=badtls")[0], 502)

    def test_cross_origin_credentials_not_forwarded(self):
        response = request(CHUNK + "?case=cross", {"Authorization": "Bearer fixture-token", "Cookie": "test=1"})
        self.assertEqual((response[0], response[1]["x-cache"], response[2]), (200, "PASS", BODY))
        final = [r for r in stats()["requests"] if r["path"] == "/final?case=cross"][-1]
        self.assertIsNone(final["authorization"])
        self.assertIsNone(final["cookie"])

    def test_redirect_failures_are_bounded_and_not_cached(self):
        for case in ("loop", "missing", "invalid"):
            with self.subTest(case=case):
                path = CHUNK + "?case=" + case
                for _ in range(2):
                    response = request(path)
                    self.assertEqual(response[0], 502)
                    self.assertNotEqual(response[1].get("x-cache"), "HIT")
                expected = 12 if case == "loop" else 2
                self.assertEqual(stats()["counts"][path], expected)

    def test_uncacheable_responses(self):
        # Varnish rejects unsolicited 206 on a full-object fetch before VCL delivery.
        for case, status in (("error", 503), ("private", 200), ("cookie", 200), ("partial", 503)):
            with self.subTest(case=case):
                path = CHUNK + "?case=" + case
                for _ in range(2):
                    response = request(path)
                    self.assertEqual(response[0], status)
                    self.assertNotEqual(response[1].get("x-cache"), "HIT")
                self.assertEqual(stats()["counts"][path], 2)

    def test_pass_range_and_client_cache_header_spoof(self):
        path = "/depot/123/manifest/456/5?case=pass"
        for _ in range(2):
            response = request(path, {"Range": "bytes=10-29", "X-Lancache-Cacheable": "1"})
            self.assertEqual((response[0], response[1]["x-cache"], response[2]), (206, "PASS", BODY[10:30]))
        self.assertEqual(stats()["counts"][path], 2)

    def test_query_strings_separate_cache_entries(self):
        for query in ("a%2Fb", "a%2Fc"):
            path = CHUNK + "?case=query&token=" + query
            self.assertEqual(request(path)[1]["x-cache"], "MISS")
            self.assertEqual(request(path)[1]["x-cache"], "HIT")
            self.assertEqual(stats()["counts"][path], 1)

    def test_concurrent_misses_collapse(self):
        path = CHUNK + "?case=collapse"
        with ThreadPoolExecutor(max_workers=12) as executor:
            responses = list(executor.map(lambda _: request(path), range(12)))
        for response in responses:
            self.assertEqual((response[0], response[2]), (200, BODY))
        self.assertEqual(stats()["counts"][path], 1)

    def test_cache_file_is_fixed_and_preallocated(self):
        stat = Path("/cache/cache.bin").stat()
        self.assertEqual(stat.st_size, 64 * 1024 * 1024)
        self.assertGreaterEqual(stat.st_blocks * 512, stat.st_size)

    def test_storage_eviction_keeps_file_size_fixed(self):
        paths = [f"/depot/123/chunk/{index:040x}?case=evict" for index in range(384)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            for status, headers, body in executor.map(request, paths):
                self.assertEqual((status, headers["x-cache"], body), (200, "MISS", BODY))
        self.assertEqual(request(paths[-1])[1]["x-cache"], "HIT")
        self.assertEqual(request(paths[0])[1]["x-cache"], "MISS")
        self.assertEqual(Path("/cache/cache.bin").stat().st_size, 64 * 1024 * 1024)

    def test_truncated_response_does_not_poison_cache(self):
        path = CHUNK + "?case=truncated"
        try:
            first = request(path)
            self.assertGreaterEqual(first[0], 500)
        except http.client.IncompleteRead:
            pass  # Once streaming starts, failure is signalled by a broken body.
        second = request(path)
        self.assertEqual((second[0], second[1]["x-cache"], second[2]), (200, "MISS", BODY))
        self.assertEqual(request(path)[1]["x-cache"], "HIT")
        self.assertEqual(stats()["counts"][path], 2)

    def test_health_and_method(self):
        self.assertEqual(request("/_health")[0], 200)
        self.assertEqual(request("/server-status")[0], 200)
        self.assertEqual(request(CHUNK, method="POST")[0], 405)


if __name__ == "__main__":
    if sys.argv[1:] == ["seed"]:
        path = CHUNK + "?case=restart"
        assert request(path)[1]["x-cache"] == "MISS"
        assert request(path)[1]["x-cache"] == "HIT"
        print("PASS: restart probe is cached before restart")
    elif sys.argv[1:] == ["restart"]:
        path = CHUNK + "?case=restart"
        before = stats()["counts"][path]
        assert request(path)[1]["x-cache"] == "MISS", "cache must be cold after restart"
        assert request(path)[1]["x-cache"] == "HIT"
        assert stats()["counts"][path] == before + 1
        assert Path("/cache/cache.bin").stat().st_size == 64 * 1024 * 1024
        print("PASS: cold restart reuses the same fixed-size file")
    else:
        unittest.main(verbosity=2)
