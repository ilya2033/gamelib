/* GameLib client: view toggle, dialogs, scan helpers and streamed uploads. */
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  // ---- grid / list view (remembered per browser)
  const list = document.querySelector("[data-games]");
  const viewBtns = document.querySelectorAll("[data-view]");
  function setView(v) {
    if (!list) return;
    list.classList.toggle("grid", v !== "list");
    list.classList.toggle("list", v === "list");
    viewBtns.forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    store.set("gamelib.view", v);
  }
  viewBtns.forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  setView(store.get("gamelib.view") || "grid");

  // ---- filters: collapsed on phones, always open on desktop (where the
  // summary toggle is hidden), also after rotating or resizing
  const filters = document.querySelector(".filter-box");
  if (filters) {
    const narrow = matchMedia("(max-width: 760px)");
    const syncFilters = () => { filters.open = !narrow.matches; };
    syncFilters();
    narrow.addEventListener("change", syncFilters);
  }

  // ---- sort select submits on change
  document.querySelectorAll("form[data-autosubmit] select").forEach((s) =>
    s.addEventListener("change", () => s.form.submit()));

  // ---- confirmation dialogs
  document.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => {
      const d = document.getElementById(b.dataset.open);
      if (!d) return;
      d.querySelector("form")?.reset(); // always open with "also delete files" unticked
      d.showModal();
    }));
  document.querySelectorAll("dialog [data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((d) => {
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
    // never keep "also delete files" ticked after Cancel / Esc
    d.addEventListener("close", () => d.querySelector("form")?.reset());
  });

  // ---- flashes: fade out (errors stay longer), × closes at once
  const dismiss = (f) => { f.style.opacity = "0"; setTimeout(() => f.remove(), 400); };
  document.querySelectorAll(".flash").forEach((f) => {
    f.querySelector(".flash-close")?.addEventListener("click", () => dismiss(f));
    setTimeout(() => dismiss(f), f.classList.contains("error") ? 10000 : 4000);
  });

  // ---- scan page: select all + dim unchecked rows
  const all = document.querySelector("[data-check-all]");
  const picks = [...document.querySelectorAll("[data-pick]")];
  const dim = (p) => p.closest(".scan-row").classList.toggle("off", !p.checked);
  const syncAll = () => {
    if (!all) return;
    const n = picks.filter((p) => p.checked).length;
    all.checked = n === picks.length;
    all.indeterminate = n > 0 && n < picks.length;
  };
  picks.forEach((p) => p.addEventListener("change", () => { dim(p); syncAll(); }));
  all?.addEventListener("change", () => { picks.forEach((p) => { p.checked = all.checked; dim(p); }); syncAll(); });

  // ---- source picker panes (+ Version is required unless "None yet")
  document.querySelectorAll("[data-source]").forEach((fs) => {
    const form = fs.closest("form");
    const ver = form?.querySelector("[data-version-input]");
    const hint = form?.querySelector("[data-version-hint]");
    const sync = () => {
      const v = fs.querySelector('input[name="source"]:checked')?.value || "upload";
      fs.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== v; });
      if (ver) ver.required = v !== "none";
      if (hint) hint.hidden = v === "none";
    };
    fs.addEventListener("change", sync);
    sync();
  });

  // ---- streamed upload with progress (raw body, not multipart)
  function fmt(n) {
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  document.querySelectorAll("[data-uploader]").forEach((up) => {
    const input = up.querySelector('input[type="file"]');
    const hidden = up.querySelector('input[name="upload_id"]');
    const drop = up.querySelector(".drop");
    const text = up.querySelector(".drop-text");
    const prog = up.querySelector(".progress");
    const bar = up.querySelector(".bar");
    const status = up.querySelector(".upload-status");
    const cancel = up.querySelector("[data-cancel]");
    const form = up.closest("form");
    let xhr = null;

    function start(file) {
      if (!file) return;
      if (!/\.(zip|html?)$/i.test(file.name)) {
        status.textContent = "Only .zip or .html files can be uploaded."; return;
      }
      if (hidden.value) fetch(`/api/uploads/${hidden.value}/delete`, { method: "POST", headers: { "X-CSRF-Token": csrf } });
      hidden.value = "";
      up.classList.remove("done", "failed");
      prog.hidden = false; cancel.hidden = false; bar.style.width = "0";
      text.textContent = file.name;
      const t0 = Date.now();
      xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/uploads");
      xhr.setRequestHeader("X-CSRF-Token", csrf);
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = (e.loaded / e.total) * 100;
        const rate = e.loaded / Math.max(1, (Date.now() - t0) / 1000);
        const eta = rate ? Math.round((e.total - e.loaded) / rate) : 0;
        bar.style.width = pct.toFixed(1) + "%";
        status.textContent = `${pct.toFixed(1)}% · ${fmt(e.loaded)} of ${fmt(e.total)} · ${fmt(rate)}/s` +
          (eta > 0 ? ` · ${eta >= 60 ? Math.round(eta / 60) + " min" : eta + " s"} left` : "");
      };
      xhr.onload = () => {
        cancel.hidden = true;
        let res = {};
        try { res = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
        if (xhr.status === 200 && res.id) {
          hidden.value = res.id; bar.style.width = "100%"; up.classList.add("done");
          status.textContent = `Uploaded ${fmt(res.size)}. It is unpacked into the share when you save.`;
        } else {
          up.classList.add("failed");
          status.textContent = xhr.status === 401
            ? "Your session has expired. Log in again in another tab, then retry."
            : res.error || `Upload failed (HTTP ${xhr.status}).`;
        }
        xhr = null;
      };
      xhr.onerror = () => { up.classList.add("failed"); cancel.hidden = true; status.textContent = "Upload failed: network error."; xhr = null; };
      xhr.onabort = () => { up.classList.add("failed"); cancel.hidden = true; status.textContent = "Upload cancelled."; xhr = null; };
      xhr.send(file);
    }

    input.addEventListener("change", () => start(input.files[0]));
    cancel.addEventListener("click", () => xhr?.abort());
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (e) => { e.preventDefault(); start(e.dataTransfer.files[0]); });

    form?.addEventListener("submit", (e) => {
      const src = form.querySelector('input[name="source"]:checked')?.value || "upload";
      if (src !== "upload") return;
      if (xhr) { e.preventDefault(); status.textContent = "Wait for the upload to finish."; return; }
      if (!hidden.value) { e.preventDefault(); status.textContent = "Choose a file to upload first."; return; }
      form.querySelectorAll('button[type="submit"]').forEach((b) => { b.disabled = true; b.textContent = "Unpacking…"; });
    });
    window.addEventListener("beforeunload", (e) => { if (xhr) { e.preventDefault(); e.returnValue = ""; } });
  });
})();
