// Server edition login screen. GET /auth/methods says which sign-in methods the server offers: the Google link
// (GET /auth/google, a plain navigation) when a Google client is configured, and the dev form (POST /auth/dev, JSON)
// only when the server allows it (YTC_DEV_LOGIN outside production). Both are static markup in login.html; this only unhides.
const $ = (id) => document.getElementById(id);

async function start() {
  let methods = null;
  try {
    const res = await fetch("/auth/methods", { credentials: "same-origin" });
    methods = res.ok ? await res.json() : null;
  } catch {
    methods = null;
  }
  if (!methods || typeof methods !== "object") {   // server down, starting (503) or not answering JSON
    $("offline").hidden = false;
    return;
  }
  if (!methods.google && !methods.dev_login) {
    $("no-method").hidden = false;
    return;
  }
  if (methods.google) {
    $("google-login").hidden = false;
  }
  if (!methods.dev_login) {
    return;
  }
  if (methods.google) {
    $("or").hidden = false;
  }
  const form = $("dev-form");
  form.hidden = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = form.querySelector("button");
    const error = $("dev-error");
    button.disabled = true;
    error.hidden = true;
    try {
      const res = await fetch("/auth/dev", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: $("dev-email").value }),
      });
      if (res.ok) {
        location.assign("/");
        return;
      }
      const data = await res.json().catch(() => null);
      error.textContent = (data && data.message) || `로그인하지 못했습니다 (${res.status})`;
    } catch {
      error.textContent = "서버에 연결할 수 없습니다";
    }
    error.hidden = false;
    button.disabled = false;
  });
}

start();
