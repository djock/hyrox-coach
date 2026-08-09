/* Offline completion queue.
 *
 * The reason this file exists: every tap is otherwise a round trip to a
 * Raspberry Pi behind a Cloudflare tunnel. In a basement gym or during a tunnel
 * blip, "Done" fails and the data is gone -- and a lazy user does not re-log the
 * session at home later. So a completion is queued locally and replayed.
 *
 * The form works with JavaScript disabled. This only upgrades it.
 */

const DB_NAME = "hyrox";
const STORE = "pending";

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE, { keyPath: "idempotency_key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function withStore(mode, fn) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const result = fn(tx.objectStore(STORE));
    tx.oncomplete = () => resolve(result.result ?? result);
    tx.onerror = () => reject(tx.error);
  });
}

const queue = {
  add: (payload) => withStore("readwrite", (store) => store.put(payload)),
  remove: (key) => withStore("readwrite", (store) => store.delete(key)),
  all: () => withStore("readonly", (store) => store.getAll()),
};

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function banner(text) {
  let el = document.querySelector(".offline-note");
  if (!el) {
    el = document.createElement("div");
    el.className = "offline-note";
    document.body.appendChild(el);
  }
  el.textContent = text;
  return el;
}

function clearBanner() {
  document.querySelector(".offline-note")?.remove();
}

async function send(payload) {
  const response = await fetch("/api/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

/* Replay is safe because the server keys on idempotency_key: a duplicate
 * returns the original event instead of logging a second one. */
async function flush() {
  let pending;
  try {
    pending = await queue.all();
  } catch {
    return;
  }
  if (!pending.length) return;

  let sent = 0;
  for (const payload of pending) {
    try {
      await send(payload);
      await queue.remove(payload.idempotency_key);
      sent += 1;
    } catch {
      break;
    }
  }
  if (sent > 0) {
    const note = banner(`${sent} saved session${sent === 1 ? "" : "s"} synced.`);
    setTimeout(() => note.remove(), 4000);
  }
}

function wireCompletionForms() {
  document.querySelectorAll("form[data-offline-complete]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      const key = uuid();
      const keyField = form.querySelector('input[name="idempotency_key"]');
      if (keyField) keyField.value = key;

      if (navigator.onLine) return; // let the normal POST happen

      event.preventDefault();
      const data = new FormData(form);
      await queue.add({
        idempotency_key: key,
        slug: data.get("slug"),
        csrf_token: data.get("csrf_token"),
        substituted_from: data.get("substituted_from") || null,
        substitution_reason: data.get("substitution_reason") || null,
        training_date: new Date().toLocaleDateString("en-CA"),
      });

      form.querySelector("button")?.setAttribute("disabled", "disabled");
      banner("Saved on this phone. It will sync when you're back online.");
    });
  });
}

window.addEventListener("online", () => {
  clearBanner();
  flush();
});
window.addEventListener("offline", () => banner("Offline — sessions are saved on this phone."));

document.addEventListener("DOMContentLoaded", () => {
  wireCompletionForms();
  if (navigator.onLine) flush();
  else banner("Offline — sessions are saved on this phone.");

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});
