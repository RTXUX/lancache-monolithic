-- nginx streams the body. Lua only resolves the next URL before proxy_pass.
local url = require("socket.url")

local function fail(reason)
    ngx.log(ngx.ERR, "upstream redirect rejected: ", reason)
    ngx.status = ngx.HTTP_BAD_GATEWAY
    ngx.header["Content-Type"] = "text/plain"
    ngx.header["X-Redirect-Error"] = reason
    ngx.say("Upstream redirect failed: " .. reason)
    return ngx.exit(ngx.HTTP_BAD_GATEWAY)
end

local count = tonumber(ngx.var.redirect_count)
if count >= tonumber(ngx.var.redirect_limit) then
    return fail("redirect limit exceeded")
end

-- Capture this before starting another upstream request replaces its headers.
local location = ngx.var.upstream_http_location
if not location or location == "" then
    return fail("missing Location")
end
if location:find("[%c%s]") then
    return fail("invalid Location")
end

local previous = url.parse(ngx.var.target_url)
local absolute
-- LuaSocket follows RFC 2396 for query-only references; HTTP needs the current
-- path retained (RFC 3986), rather than the containing directory.
if location:sub(1, 1) == "?" then
    absolute = ngx.var.target_url:gsub("[?#].*$", "") .. location
else
    absolute = url.absolute(ngx.var.target_url, location)
end
absolute = absolute:gsub("#.*$", "")
local target = url.parse(absolute)
if not target or (target.scheme ~= "http" and target.scheme ~= "https")
    or not target.host or target.host == "" or target.userinfo then
    return fail("unsupported redirect URL")
end
if target.port and (not tonumber(target.port) or tonumber(target.port) < 1
    or tonumber(target.port) > 65535) then
    return fail("invalid redirect port")
end

-- Relative/same-origin redirects retain an independently configured Host.
-- Cross-origin redirects use the new authority and do not forward credentials.
if target.authority ~= previous.authority or target.scheme ~= previous.scheme then
    ngx.var.target_host = target.authority
    ngx.var.target_tls_name = target.host
    ngx.var.forward_authorization = ""
    ngx.var.forward_cookie = ""
end
ngx.var.target_url = absolute
ngx.var.redirect_count = count + 1
