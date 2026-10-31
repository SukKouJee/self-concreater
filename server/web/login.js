// Server edition login screen. GET /auth/methods says which sign-in methods the server offers:
// - password (YTC_PASSWORD_LOGIN=1): an email + password form built here with h() (POST /auth/password, JSON),
// - the Google link (GET /auth/google, a plain navigation) when a Google client is configured,
// - the dev form (POST /auth/dev, JSON) only when the server allows it (YTC_DEV_LOGIN outside production).
// The Google link and dev form are static markup in login.html; this only unhides them. Text is always set as text (h(), textContent).
import { h } from "/auth/dom.js";

const $ = (id) => document.getElementById(id);
const OFFLINE = "서버에 연결할 수 없습니다. 잠시 뒤 다시 시도하세요";

async function postJson(path, body) {
  return fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Message for a failed login response: the server's Korean message (401 generic, 429 with minutes) or a fallback.
async function failureText(res) {
  const data = await res.json().catch(() => null);
  if (data && typeof data.message === "string" && data.message) return data.message;
  if (res.status === 429) return "로그인 시도가 너무 많습니다. 잠시 뒤 다시 시도하세요";
  return `로그인하지 못했습니다 (${res.status})`;
}

function passwordForm() {
  const email = h("input", { id: "pw-email", name: "email", type: "email", autocomplete: "username", required: true });
  const password = h("input", {
    id: "pw-password", name: "password", type: "password", autocomplete: "current-password", required: true,
  });
  const button = h("button", { type: "submit" }, "로그인");
  const error = h("p", { id: "pw-error", class: "error", role: "alert", hidden: true });
  const form = h("form", { id: "password-form", "aria-label": "이메일과 비밀번호로 로그인" },
    h("label", { for: "pw-email" }, "이메일"), email,
    h("label", { for: "pw-password" }, "비밀번호"), password,
    button, error);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    button.disabled = true;
    button.textContent = "로그인하는 중…";
    error.hidden = true;
    let message = "";
    try {
      const res = await postJson("/auth/password", { email: email.value, password: password.value });
      if (res.ok) {
        location.assign("/");
        return;
      }
      message = await failureText(res);
    } catch {
      message = OFFLINE;
    }
    error.textContent = message;
    error.hidden = false;
    password.value = "";
    password.focus();
    button.disabled = false;
    button.textContent = "로그인";
  });
  return form;
}

function devForm() {
  const form = $("dev-form");
  form.hidden = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = form.querySelector("button");
    const error = $("dev-error");
    button.disabled = true;
    error.hidden = true;
    try {
      const res = await postJson("/auth/dev", { email: $("dev-email").value });
      if (res.ok) {
        location.assign("/");
        return;
      }
      error.textContent = await failureText(res);
    } catch {
      error.textContent = "서버에 연결할 수 없습니다";
    }
    error.hidden = false;
    button.disabled = false;
  });
}

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
  if (!methods.google && !methods.dev_login && !methods.password) {
    $("no-method").hidden = false;
    return;
  }
  if (methods.password) {
    $("password-slot").append(passwordForm());
    if (methods.google || methods.dev_login) {
      $("password-or").hidden = false;
    }
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
  devForm();
}

start();
