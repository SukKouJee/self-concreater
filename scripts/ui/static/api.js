// JSON fetch helper. Throws {status, error, message, data} on failure (error "aborted" when signal fires).
const BAD_TOKEN_TEXT = "서버가 다시 켜졌습니다. 터미널에 나온 새 주소로 여세요";

export function showBanner(text) {
  const el = document.getElementById("banner");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}

export async function api(method, path, body, signal) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (signal) opts.signal = signal;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  } else if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = "{}";
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (e && e.name === "AbortError") throw { status: 0, error: "aborted", message: "요청이 취소되었습니다" };
    throw { status: 0, error: "network", message: "서버에 연결할 수 없습니다" };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = {
      status: res.status,
      error: (data && data.error) || "http_error",
      message: (data && data.message) || `요청이 실패했습니다 (${res.status})`,
      data,   // full error body (extra fields such as shared_id / push_unknown)
    };
    if (res.status === 403 && err.error === "bad_token") {
      err.message = BAD_TOKEN_TEXT;
      showBanner(BAD_TOKEN_TEXT);
    }
    throw err;
  }
  return data;
}
