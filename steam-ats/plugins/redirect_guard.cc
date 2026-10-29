// Normalize Location headers and reject cycles before ATS performs its native
// redirect/cache lookup. No network requests or response-body buffering here.
#include <ts/ts.h>
#include <curl/curl.h>

#include <algorithm>
#include <cctype>
#include <memory>
#include <string>
#include <unordered_set>

namespace {
struct State {
  std::unordered_set<std::string> visited;
};

std::string
normalize(const std::string &base, const std::string &location)
{
  std::unique_ptr<CURLU, decltype(&curl_url_cleanup)> url(curl_url(), curl_url_cleanup);
  if (!url || curl_url_set(url.get(), CURLUPART_URL, base.c_str(), 0) != CURLUE_OK ||
      curl_url_set(url.get(), CURLUPART_URL, location.c_str(), 0) != CURLUE_OK) {
    return {};
  }
  char *part = nullptr;
  if (curl_url_get(url.get(), CURLUPART_SCHEME, &part, 0) != CURLUE_OK) {
    return {};
  }
  const std::string scheme(part);
  curl_free(part);
  if (scheme != "http" && scheme != "https") {
    return {};
  }
  if (curl_url_get(url.get(), CURLUPART_USER, &part, 0) == CURLUE_OK) {
    curl_free(part);
    return {};
  }
  if (curl_url_get(url.get(), CURLUPART_HOST, &part, 0) != CURLUE_OK) {
    return {};
  }
  std::string host(part);
  curl_free(part);
  std::transform(host.begin(), host.end(), host.begin(), [](unsigned char ch) { return std::tolower(ch); });
  curl_url_set(url.get(), CURLUPART_HOST, host.c_str(), 0);
  curl_url_set(url.get(), CURLUPART_FRAGMENT, nullptr, 0);
  if (curl_url_get(url.get(), CURLUPART_URL, &part, CURLU_NO_DEFAULT_PORT) != CURLUE_OK) {
    return {};
  }
  std::string result(part);
  curl_free(part);
  return result;
}

void
guard(TSHttpTxn txn, State &state)
{
  TSMBuffer buffer = nullptr;
  TSMLoc header = TS_NULL_MLOC;
  if (TSHttpTxnServerRespGet(txn, &buffer, &header) != TS_SUCCESS) {
    return;
  }
  const int status = TSHttpHdrStatusGet(buffer, header);
  if (status != 300 && status != 301 && status != 302 && status != 303 &&
      status != 305 && status != 307 && status != 308) {
    TSHandleMLocRelease(buffer, TS_NULL_MLOC, header);
    return;
  }
  int length = 0;
  char *effective = TSHttpTxnEffectiveUrlStringGet(txn, &length);
  const std::string base(effective ? effective : "", effective ? length : 0);
  TSfree(effective);
  state.visited.insert(normalize(base, base));

  TSMLoc field = TSMimeHdrFieldFind(buffer, header, "Location", 8);
  std::string next;
  if (field != TS_NULL_MLOC) {
    const char *value = TSMimeHdrFieldValueStringGet(buffer, header, field, -1, &length);
    if (value && length > 0) {
      next = normalize(base, std::string(value, length));
    }
  }
  if (next.empty() || state.visited.count(next)) {
    // A same-key lookup can otherwise wait on this transaction's own writer.
    TSHttpHdrStatusSet(buffer, header, TS_HTTP_STATUS_BAD_GATEWAY);
    TSHttpHdrReasonSet(buffer, header, "Upstream redirect rejected", -1);
    TSHttpTxnCntlSet(txn, TS_HTTP_CNTL_SERVER_NO_STORE, true);
    if (field != TS_NULL_MLOC) {
      TSMimeHdrFieldDestroy(buffer, header, field);
    }
  } else {
    TSMimeHdrFieldValueStringSet(buffer, header, field, -1, next.data(), static_cast<int>(next.size()));
  }
  if (field != TS_NULL_MLOC) {
    TSHandleMLocRelease(buffer, header, field);
  }
  TSHandleMLocRelease(buffer, TS_NULL_MLOC, header);
}

int
transaction(TSCont continuation, TSEvent event, void *data)
{
  auto txn = static_cast<TSHttpTxn>(data);
  auto *state = static_cast<State *>(TSContDataGet(continuation));
  if (event == TS_EVENT_HTTP_READ_RESPONSE_HDR) {
    guard(txn, *state);
  } else if (event == TS_EVENT_HTTP_TXN_CLOSE) {
    delete state;
    TSContDestroy(continuation);
  }
  TSHttpTxnReenable(txn, TS_EVENT_HTTP_CONTINUE);
  return 0;
}

int
start(TSCont, TSEvent, void *data)
{
  auto txn = static_cast<TSHttpTxn>(data);
  TSCont continuation = TSContCreate(transaction, TSMutexCreate());
  TSContDataSet(continuation, new State);
  TSHttpTxnHookAdd(txn, TS_HTTP_READ_RESPONSE_HDR_HOOK, continuation);
  TSHttpTxnHookAdd(txn, TS_HTTP_TXN_CLOSE_HOOK, continuation);
  TSHttpTxnReenable(txn, TS_EVENT_HTTP_CONTINUE);
  return 0;
}
} // namespace

extern "C" void
TSPluginInit(int, const char *[])
{
  TSPluginRegistrationInfo info = {"redirect_guard", "steam-ats", ""};
  if (TSPluginRegister(&info) != TS_SUCCESS) {
    TSError("[redirect_guard] registration failed");
    return;
  }
  TSHttpHookAdd(TS_HTTP_READ_REQUEST_HDR_HOOK, TSContCreate(start, nullptr));
}
