vcl 4.1;

backend default {
    .host = "nginx";
    .port = "8080";
    .connect_timeout = 5s;
    .first_byte_timeout = 120s;
    .between_bytes_timeout = 120s;
}

sub vcl_recv {
    unset req.http.X-Lancache-Cacheable;
    if (req.url == "/_health" || req.url == "/server-status") {
        return (synth(200, "OK"));
    }
    if (req.method != "GET" && req.method != "HEAD") {
        return (synth(405, "Only GET and HEAD are supported"));
    }

    # All client Host values select the same fixed origin and cache namespace.
    # This also supports Steam's native LANCache discovery, which retains CDN Host.
    set req.http.host = "lancache.steamcontent.com";
    if (req.http.Authorization || req.http.Cookie) {
        return (pass);
    }
    # Cache content-addressed chunks. Manifests and other API requests pass through.
    if (req.url !~ "^/depot/[0-9]+/chunk/[a-fA-F0-9]+(\?.*)?$") {
        return (pass);
    }
    set req.http.X-Lancache-Cacheable = "1";
    return (hash);
}

sub vcl_hash {
    hash_data(req.url);  # Keep query strings, including authorization tokens.
    hash_data("steam-clngaa-v1");
    return (lookup);
}

sub vcl_backend_fetch {
    if (bereq.http.X-Lancache-Cacheable == "1") {
        # Fetch one complete chunk, including on a cold Range/HEAD request.
        # Varnish generates the client's 206/HEAD response from that object.
        set bereq.method = "GET";
        unset bereq.http.Range;
        unset bereq.http.If-Range;
    }
}

sub vcl_backend_response {
    set beresp.do_stream = true;
    if (bereq.http.X-Lancache-Cacheable != "1" || beresp.status != 200
        || beresp.http.Set-Cookie || beresp.http.Vary == "*"
        || beresp.http.Cache-Control ~ "(?i)(private|no-store|no-cache)") {
        set beresp.uncacheable = true;
        set beresp.ttl = 0s;
        return (deliver);
    }
    set beresp.ttl = @@CACHE_TTL_SECONDS@@s;
    set beresp.grace = 1h;
    set beresp.storage = storage.disk;
    return (deliver);
}

sub vcl_deliver {
    if (req.http.X-Lancache-Cacheable != "1") {
        set resp.http.X-Cache = "PASS";
    } else if (obj.uncacheable) {
        set resp.http.X-Cache = "PASS";
    } else if (obj.hits > 0) {
        set resp.http.X-Cache = "HIT";
    } else {
        set resp.http.X-Cache = "MISS";
    }
    unset resp.http.X-Lancache-Cacheable;
}

sub vcl_synth {
    set resp.http.Content-Type = "text/plain; charset=utf-8";
    synthetic(resp.reason + "\n");
    return (deliver);
}
