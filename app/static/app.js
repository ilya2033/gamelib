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

  // ---- scan page: select all, filters, sections, in-place rule changes
  const scan = document.querySelector("[data-scan]");
  if (scan) {
    const all = scan.querySelector("[data-check-all]");
    const filter = scan.querySelector("[data-scan-filter]");
    const suspectsOnly = scan.querySelector("[data-suspects-only]");
    const status = scan.querySelector("[data-rule-status]");
    const bar = scan.querySelector("[data-rule-bar]");
    const undoBtn = scan.querySelector("[data-rule-undo]");
    const sectionsEl = scan.querySelector("[data-sections]");
    const picks = () => [...scan.querySelectorAll("[data-pick]")];
    const esc = (v) => (window.CSS && CSS.escape ? CSS.escape(v) : v.replace(/["\\]/g, "\\$&"));

    const syncAll = () => {
      const p = picks(), n = p.filter((x) => x.checked).length;
      if (all) { all.checked = p.length > 0 && n === p.length; all.indeterminate = n > 0 && n < p.length; }
    };
    const recount = () => {
      let visible = 0;
      scan.querySelectorAll("[data-section]").forEach((sec) => {
        const rows = [...sec.querySelectorAll(".scan-row")];
        const shown = rows.filter((r) => !r.hidden).length;
        visible += shown;
        const c = sec.querySelector("[data-section-count]");
        if (c) c.textContent = rows.length === shown ? rows.length : `${shown} / ${rows.length}`;
        if (!rows.length) sec.remove();
      });
      const total = scan.querySelectorAll(".scan-row").length;
      const c = scan.querySelector("[data-scan-count]");
      if (c) c.textContent = visible === total ? `${total} folder(s)` : `${visible} of ${total} folder(s) shown`;
      const gl = scan.querySelector("[data-group-list]"), gc = scan.querySelector("[data-group-count]");
      if (gl && gc) gc.textContent = gl.children.length;
      scan.querySelectorAll("[data-import-bar], .scan-tools").forEach((el) => { el.hidden = total === 0; });
      syncAll();
    };
    let filtering = false;
    const applyFilter = () => {
      const q = (filter?.value || "").trim().toLowerCase();
      const only = suspectsOnly?.checked;
      filtering = Boolean(q || only);
      scan.querySelectorAll(".scan-row").forEach((r) => {
        r.hidden = (q && !r.dataset.search.includes(q)) || (only && !r.classList.contains("is-suspect"));
      });
      // while filtering: open sections with matches, hide sections without any
      scan.querySelectorAll("[data-section]").forEach((sec) => {
        const any = [...sec.querySelectorAll(".scan-row")].some((r) => !r.hidden);
        sec.hidden = filtering && !any;
        if (filtering && any) sec.open = true;
        else if (!filtering) sec.open = !closed.has(sec.dataset.section);
      });
      recount();
    };
    // only rows you can see are imported
    scan.querySelector("[data-scan-form]")?.addEventListener("submit", (e) => {
      if (e.submitter && e.submitter.matches("[data-rule]")) return;
      scan.querySelectorAll(".scan-row").forEach((r) => {
        const cb = r.querySelector("[data-pick]");
        if (cb) cb.disabled = r.hidden || r.closest("[data-section]")?.hidden;
      });
    });
    filter?.addEventListener("input", applyFilter);
    suspectsOnly?.addEventListener("change", applyFilter);

    scan.addEventListener("change", (e) => {
      if (e.target.matches("[data-pick]")) { e.target.closest(".scan-row").classList.toggle("off", !e.target.checked); syncAll(); }
    });
    all?.addEventListener("change", () => {
      picks().filter((p) => !p.closest(".scan-row").hidden).forEach((p) => {
        p.checked = all.checked; p.closest(".scan-row").classList.toggle("off", !p.checked);
      });
      syncAll();
    });

    // remember which sections are collapsed (not while a filter forces them open)
    const KEY = "gamelib.scan.closed";
    const closed = new Set(JSON.parse(store.get(KEY) || "[]"));
    const wireSection = (sec) => {
      if (closed.has(sec.dataset.section)) sec.open = false;
      sec.addEventListener("toggle", () => {
        if (filtering) return;
        sec.open ? closed.delete(sec.dataset.section) : closed.add(sec.dataset.section);
        store.set(KEY, JSON.stringify([...closed]));
      });
    };
    const byPath = (a, b) => a.dataset.path.localeCompare(b.dataset.path, undefined, { numeric: true, sensitivity: "base" });
    const sortChildren = (list) => [...list.children].sort(byPath).forEach((el) => list.appendChild(el));
    const placeSection = (sec) => {          // keep sections in name order, "Top level" first
      const name = sec.dataset.section;
      const after = [...sectionsEl.children].find((s) => s !== sec && name && (!s.dataset.section ? false :
        s.dataset.section.localeCompare(name, undefined, { numeric: true, sensitivity: "base" }) > 0));
      if (!name) sectionsEl.prepend(sec); else sectionsEl.insertBefore(sec, after || null);
    };
    scan.querySelectorAll("[data-section]").forEach(wireSection);

    const say = (text) => { if (status) status.textContent = text; if (bar) bar.hidden = false; };
    const busy = (on) => scan.classList.toggle("rules-busy", on);

    const waitJob = async (id) => {
      for (;;) {
        const r = await fetch(`/api/jobs/${id}`);
        const j = await r.json();
        if (!j.active) return j;
        say(j.message ? `Re-checking… ${j.message}` : "Re-checking folders…");
        await new Promise((res) => setTimeout(res, 400));
      }
    };

    const patch = (frag) => {
      const under = (p) => frag.roots.some((r) => p === r || p.startsWith(r + "/"));
      scan.querySelectorAll(".scan-row[data-path], [data-group-list] > li[data-path]").forEach((el) => {
        if (under(el.dataset.path)) el.remove();
      });
      const tpl = scan.querySelector("[data-section-template]");
      const touched = new Set();
      let first = null;
      for (const row of frag.rows) {
        let sec = scan.querySelector(`[data-section="${esc(row.section)}"]`);
        if (!sec && tpl) {
          sec = tpl.content.firstElementChild.cloneNode(true);
          sec.dataset.section = row.section;
          sec.querySelector("b").textContent = row.section || "Top level";
          placeSection(sec);
          wireSection(sec);
        }
        if (!sec) continue;
        const list = sec.querySelector(".scan-list");
        list.insertAdjacentHTML("beforeend", row.html);
        touched.add(list);
        first = first || sec;
      }
      touched.forEach(sortChildren);
      const gl = scan.querySelector("[data-group-list]");
      if (gl) { frag.group_rows.forEach((g) => gl.insertAdjacentHTML("beforeend", g.html)); sortChildren(gl); }
      if (undoBtn) { undoBtn.hidden = !frag.can_undo; undoBtn.title = frag.undo_label || ""; }
      if (bar) bar.dataset.undoLabel = frag.undo_label || "";
      scan.querySelector("[data-empty]")?.remove();
      applyFilter();
      if (first) { first.open = true; first.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
    };

    const run = async (url, payload) => {
      busy(true);
      say("Saving…");
      try {
        const r = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: payload ? JSON.stringify(payload) : "{}",
        });
        const res = await r.json();
        if (!r.ok) throw new Error(res.error || `HTTP ${r.status}`);
        const j = await waitJob(res.job);
        if (j.state !== "done") throw new Error(j.error || `Re-check ${j.state}`);
        const fr = await fetch(`/scan/fragment/${res.job}`);
        const frag = await fr.json();
        patch(frag);
        say(`Updated ${frag.roots.length} folder(s): ${frag.rows.length} game row(s), ${frag.group_rows.length} group folder(s).` +
            (frag.undo_label ? ` Undo: ${frag.undo_label}.` : ""));
      } catch (err) {
        say(`Couldn't apply that: ${err.message}`);
      } finally {
        busy(false);
      }
    };
    const rules = (kind, paths) => run("/api/scan/rules", { rules: paths.map((path) => ({ path, kind })) });

    // single-folder buttons (fall back to the normal form post without JS)
    scan.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-rule]");
      if (!btn) return;
      e.preventDefault();
      rules(btn.dataset.rule, [btn.dataset.path]);
    });
    scan.querySelectorAll("[data-bulk-rule]").forEach((btn) => btn.addEventListener("click", () => {
      const paths = [...scan.querySelectorAll("[data-group-pick]:checked")].map((c) => c.value);
      if (!paths.length) return say("Tick some group folders first.");
      rules(btn.dataset.bulkRule, paths);
    }));
    scan.querySelector("[data-fix-suspects]")?.addEventListener("click", (e) => {
      const paths = [...scan.querySelectorAll(".scan-row.is-suspect")].map((r) => r.dataset.path);
      if (!paths.length) return say("No suspects left.");
      if (!confirm(`Mark ${paths.length} folder(s) as groups? Their subfolders become separate games. You can undo this.`)) return;
      e.currentTarget.remove();
      rules("group", paths);
    });
    undoBtn?.addEventListener("click", () => run("/api/scan/rules/undo"));
    applyFilter();
  }

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

  // ---- background jobs: live progress, reload when finished
  document.querySelectorAll("[data-job-poll]").forEach((card) => {
    const id = card.dataset.jobPoll;
    const bar = card.querySelector(".bar");
    const msg = card.querySelector("[data-job-msg]");
    const st = card.querySelector("[data-job-state]");
    const tick = async () => {
      try {
        const r = await fetch(`/api/jobs/${id}`, { headers: { Accept: "application/json" } });
        if (!r.ok) return setTimeout(tick, 3000);
        const j = await r.json();
        if (bar && j.pct !== null) bar.style.width = j.pct + "%";
        if (msg && j.message) msg.textContent = j.message + (j.total ? ` (${j.done} / ${j.total})` : "");
        if (st) st.textContent = j.state;
        if (!j.active) return location.reload();
      } catch { /* network blip */ }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 600);
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
  // ---- simple confirmations and "select all" (1.4.0 tools)
  document.querySelectorAll("[data-confirm]").forEach((b) =>
    b.addEventListener("click", (e) => { if (!confirm(b.dataset.confirm)) e.preventDefault(); }));
  document.querySelectorAll("[data-check-all]").forEach((b) =>
    b.addEventListener("click", () => {
      const id = b.dataset.checkAll;
      const boxes = [...document.querySelectorAll(`input[type=checkbox][name=vid][form="${id}"], #${id} input[type=checkbox][name=vid]`)];
      const on = boxes.some((x) => !x.checked);
      boxes.forEach((x) => { x.checked = on; });
    }));

  // ---- library: select games, then change them together
  const bulk = document.getElementById("bulk");
  if (bulk && list) {
    const picks = () => [...list.querySelectorAll("input.pick")];
    const count = bulk.querySelector("[data-bulk-count]");
    const action = bulk.querySelector("[data-bulk-action]");
    const value = bulk.querySelector("[data-bulk-value]");
    const update = () => {
      const n = picks().filter((p) => p.checked).length;
      count.textContent = `${n} selected`;
      bulk.querySelector("button.primary").disabled = n === 0;
    };
    const syncValue = () => {
      const needs = action.selectedOptions[0]?.dataset.needs;
      value.hidden = !needs;
      value.placeholder = needs || "";
      value.required = !!needs && !action.value.startsWith("rating");
    };
    const setSelecting = (on) => {
      document.body.classList.toggle("selecting", on);
      bulk.hidden = !on;
      if (!on) picks().forEach((p) => { p.checked = false; });
      update();
    };
    document.querySelectorAll("[data-select-toggle]").forEach((b) =>
      b.addEventListener("click", () => setSelecting(!document.body.classList.contains("selecting"))));
    bulk.querySelector("[data-select-all]").addEventListener("click", () => {
      const all = picks();
      const on = all.some((p) => !p.checked);
      all.forEach((p) => { p.checked = on; });
      update();
    });
    list.addEventListener("click", (e) => {
      if (!document.body.classList.contains("selecting")) return;
      const card = e.target.closest("li.card");
      if (!card) return;
      const box = card.querySelector("input.pick");
      if (e.target !== box) { e.preventDefault(); box.checked = !box.checked; }
      card.classList.toggle("picked", box.checked);
      update();
    });
    list.addEventListener("change", (e) => {
      if (e.target.matches("input.pick")) { e.target.closest("li.card").classList.toggle("picked", e.target.checked); update(); }
    });
    action.addEventListener("change", syncValue);
    syncValue();
  }
})();
