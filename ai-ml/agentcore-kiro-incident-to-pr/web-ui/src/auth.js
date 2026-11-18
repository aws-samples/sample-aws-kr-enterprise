// Web password login: the API returns a signed token that expires after 12 hours. It lives in sessionStorage,
// so closing the browser tab logs out.
const KEY = "incident-to-pr-token";

export function getToken() {
  const token = sessionStorage.getItem(KEY);
  const expiry = Number((token || "").split(".")[0]);
  if (!token || !expiry || expiry * 1000 <= Date.now()) { sessionStorage.removeItem(KEY); return null; }
  return token;
}

export function setToken(token) { sessionStorage.setItem(KEY, token); }

export function logout() {
  sessionStorage.removeItem(KEY);
  window.dispatchEvent(new Event("incident-logout"));
}
