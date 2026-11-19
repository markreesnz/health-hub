/* Arete front-end. Plain JS, relative URLs so it works behind HA ingress. */
(() => {
  const $view = document.getElementById("view");
  const $sheet = document.getElementById("sheet-root");
  let S = null;           // last /api/state snapshot
  let viewDate = null;    // date shown on Today (null = today)

  // ------------------------------------------------------------ utils
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const roman = (n) => ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"][n] || String(n + 1);
  const parseD = (d) => { const [y, m, dd] = d.split("-").map(Number); return new Date(y, m - 1, dd); };
  const fmtD = (dt) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  const addDays = (d, n) => { const x = parseD(d); x.setDate(x.getDate() + n); return fmtD(x); };
  const longDate = (d) => parseD(d).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const ARROW_R = `<svg viewBox="0 0 28 16" width="28"><path d="M1 8h25M19 1l7 7-7 7" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
  const ARROW_L = `<svg viewBox="0 0 28 16"><path d="M27 8H2M9 1L2 8l7 7" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;

  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  }
  async function loadState(d) {
    S = await api("GET", `api/state${d ? `?date=${d}` : ""}`);
    return S;
  }
  async function mutate(method, path, body) {
    try {
      S = await api(method, path, body);
      return S;
    } catch (e) { toast("Couldn't save — check your connection"); throw e; }
  }
  function toast(msg) {
    const t = document.getElementById("toast");
    t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 1800);
  }
  if (navigator.vibrate) { /* noop: feature check */ }
  const buzz = () => { try { navigator.vibrate && navigator.vibrate(12); } catch (_) {} };

  function ring(hit, total, size = 64, stroke = 4) {
    const r = (size - stroke) / 2, c = 2 * Math.PI * r;
    const pct = total ? hit / total : 0;
    const full = total > 0 && hit >= total;
    const off = c * (1 - Math.max(pct, 0.015));
    return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="${stroke}"/>
      <circle class="val" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="${stroke}" stroke-linecap="round"
        stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 ${size / 2} ${size / 2})"/>
      ${full ? `<path class="check" d="M${size * .36} ${size * .51} l${size * .1} ${size * .1} l${size * .19} -${size * .21}"/>` : ""}
    </svg>`;
  }

  // ------------------------------------------------------------ router
  async function route() {
    closeSheet();
    const h = location.hash.replace(/^#\/?/, "");
    const [page, arg] = h.split("/");
    document.querySelectorAll(".tabbar a").forEach((a) => a.classList.toggle("on",
      a.dataset.tab === (page === "area" || page === "commit" || !page ? "today" : page)));
    try {
      if (page === "area") await renderArea(arg);
      else if (page === "reflect") await renderReflect(arg);
      else if (page === "progress") await renderProgress();
      else if (page === "commit") await renderCommit();
      else await renderToday();
    } catch (e) {
      $view.innerHTML = `<div class="empty-state">Couldn't reach Arete.<br><br><button class="wide-btn" onclick="location.reload()">Retry</button></div>`;
      console.error(e);
    }
  }
  window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });

  // ------------------------------------------------------------ Today
  async function renderToday() {
    await loadState(viewDate);
    const isToday = S.date === S.today;
    const hour = new Date().getHours();
    const greet = !isToday ? longDate(S.date).split(",")[0] : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    const days = ["S", "M", "T", "W", "T", "F", "S"];
    const m = S.morning, e = S.evening;
    $view.innerHTML = `
      <div class="today-head">
        <div><div class="date">${esc(longDate(S.date))}</div><h1>${esc(greet)}</h1></div>
        <div class="daynav">
          <button id="prev" aria-label="Previous day">‹</button>
          <button id="next" aria-label="Next day" ${isToday ? "disabled" : ""}>›</button>
        </div>
      </div>
      <div class="quote"><p>${esc(S.quote.text)}</p><span>${esc(S.quote.author)}</span></div>

      <div class="card streak">
        <div class="badge">${S.streak}</div>
        <div class="body">
          <div class="label">My Streak <small>Best ${S.best_streak}</small></div>
          <div class="dots">${S.week.map((w) => `<div class="dot ${w.complete ? "on" : ""} ${w.date === S.today ? "today" : ""}"></div>`).join("")}</div>
          <div class="dots-labels">${S.week.map((w) => `<span>${days[parseD(w.date).getDay()]}</span>`).join("")}</div>
        </div>
      </div>

      ${isToday ? (S.pledged_at ? `
        <a class="pledged" href="#/commit"><span>✓ Committed at ${esc(S.pledged_at.slice(11))} · ${S.progress.done}/${S.progress.total} done</span><span>adjust</span></a>` : `
        <a class="commit-card" href="#/commit">
          <div class="k">Morning commitment</div>
          <div class="t">Commit to today</div>
          <div class="s">${S.areas.map((a) => esc(a.identity)).join(" · ")}</div>
          <div class="go">Choose your targets ${ARROW_R}</div>
        </a>`) : ""}

      <div class="section-title">Daily Reflection</div>
      <div class="reflect-row">
        <a class="reflect-tile morning" href="#/reflect/morning">
          <div class="k">Morning</div><div class="t">Prepare</div>
          <div class="s ${m ? "done" : ""}">${m ? "✓ Done" : "Set your intention"}</div>
        </a>
        <a class="reflect-tile evening" href="#/reflect/evening">
          <div class="k">Evening</div><div class="t">Review</div>
          <div class="s ${e ? "done" : ""}">${e ? "✓ Done" + (e.rating ? ` · ${e.rating}/5` : "") : "Examine the day"}</div>
        </a>
      </div>

      <div class="section-title">My Protocol</div>
      ${S.areas.map((a) => `
        <a class="protocol-card" href="#/area/${a.id}">
          <div class="txt"><div class="area">${esc(a.name)}</div><div class="identity">${esc(a.identity)}</div></div>
          ${ring(a.hit, a.total, 58, 3.5)}
          <span class="arrow">${ARROW_R}</span>
        </a>`).join("")}
      <button class="add-area" id="add-area">+ Add a protocol area</button>
    `;
    document.getElementById("prev").onclick = () => { viewDate = addDays(S.date, -1); renderToday(); };
    document.getElementById("next").onclick = () => { const n = addDays(S.date, 1); viewDate = n >= S.today ? null : n; renderToday(); };
    document.getElementById("add-area").onclick = () => editAreaSheet(null);
  }

  // ------------------------------------------------------------ Area
  async function renderArea(id) {
    await loadState(viewDate);
    const a = S.areas.find((x) => x.id === id);
    if (!a) { location.hash = "#/"; return; }
    const committed = a.targets.filter((t) => t.active);
    const uncommitted = a.targets.length - committed.length;
    const isToday = S.date === S.today;
    $view.innerHTML = `
      <div class="topbar">
        <a class="icon-btn" href="#/" aria-label="Back">${ARROW_L}</a>
        <div class="title">${esc(a.name)}</div>
        <button class="text-btn" id="edit">Edit</button>
      </div>
      <div class="hero">
        ${ring(a.hit, a.total, 150, 9)}
        <h1>${esc(a.identity)}</h1>
        <div class="traits">${a.traits.map(esc).join(" / ")}</div>
        ${isToday ? "" : `<div class="meta">Viewing ${esc(longDate(S.date))}</div>`}
      </div>
      <div class="panel">
        <div class="panel-head"><b>${esc(a.name)} Protocol</b><button class="round-btn" id="plus" aria-label="Add target">+</button></div>
        <div class="target-list">
          ${committed.map((t, i) => `
            <div class="target ${t.done ? "done" : ""}" data-id="${t.id}">
              <div class="reveal">${t.done ? "UNDO" : "HIT ✓"}</div>
              <div class="row"><div class="bar"></div><div class="name">${esc(t.title)}</div>
                <button class="numeral" aria-label="Toggle">${t.done ? "✓" : roman(i)}</button></div>
            </div>`).join("") || `<div class="empty-state">No committed targets yet.</div>`}
        </div>
        <hr class="dashed">
        ${uncommitted ? `<div class="note">Not showing ${uncommitted} target${uncommitted > 1 ? "s" : ""} not committed ${isToday ? "today" : "that day"}.</div>` : ""}
        ${isToday && !S.pledged_at ? `<button class="wide-btn primary" onclick="location.hash='#/commit'">Commit to today</button>` : ""}
        <button class="wide-btn" id="manage">Manage Targets</button>
        <button class="wide-btn" id="add">Add Target</button>
        <div class="hint">Swipe right (or tap the numeral) to hit targets.</div>
      </div>`;
    document.getElementById("edit").onclick = () => editAreaSheet(a);
    document.getElementById("plus").onclick = document.getElementById("add").onclick = () => addTargetSheet(a);
    document.getElementById("manage").onclick = () => manageSheet(a);
    $view.querySelectorAll(".target").forEach((el) => bindSwipe(el, () => toggle(el.dataset.id, a.id)));
  }

  async function toggle(tid, areaId) {
    buzz();
    const before = S.areas.find((x) => x.id === areaId);
    const wasFull = before && before.total && before.hit >= before.total;
    await mutate("POST", `api/day/${S.date}/toggle`, { target_id: tid });
    const after = S.areas.find((x) => x.id === areaId);
    if (after && after.total && after.hit >= after.total && !wasFull) toast(`${after.identity} — protocol complete`);
    renderArea(areaId);
  }

  function bindSwipe(el, onHit) {
    const row = el.querySelector(".row");
    const btn = el.querySelector(".numeral");
    btn.addEventListener("click", (e) => { e.stopPropagation(); onHit(); });
    let x0 = null, y0 = 0, dx = 0, locked = null;
    el.addEventListener("pointerdown", (e) => {
      if (e.target === btn) return;
      x0 = e.clientX; y0 = e.clientY; dx = 0; locked = null; row.classList.add("dragging");
    });
    el.addEventListener("pointermove", (e) => {
      if (x0 === null) return;
      const mx = e.clientX - x0, my = e.clientY - y0;
      if (locked === null && (Math.abs(mx) > 8 || Math.abs(my) > 8)) {
        locked = Math.abs(mx) > Math.abs(my) ? "x" : "y";
        if (locked === "x") el.setPointerCapture(e.pointerId);
      }
      if (locked !== "x") return;
      dx = Math.max(0, Math.min(mx, el.clientWidth * 0.6));
      row.style.transform = `translateX(${dx}px)`;
    });
    const end = () => {
      if (x0 === null) return;
      row.classList.remove("dragging");
      row.style.transform = "";
      const hit = locked === "x" && dx > Math.min(110, el.clientWidth * 0.28);
      x0 = null;
      if (hit) onHit();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  }

  // ------------------------------------------------------------ sheets
  function openSheet(html, onMount) {
    $sheet.innerHTML = `<div class="backdrop"></div><div class="sheet"><div class="grab"></div>${html}</div>`;
    $sheet.querySelector(".backdrop").onclick = closeSheet;
    onMount && onMount($sheet.querySelector(".sheet"));
  }
  function closeSheet() { $sheet.innerHTML = ""; }

  function addTargetSheet(a) {
    openSheet(`
      <h2>New ${esc(a.name)} target</h2>
      <div class="field"><span>What will you do each day?</span><input type="text" id="t-title" maxlength="80" placeholder="e.g. Cold shower" autocomplete="off" autocapitalize="sentences" enterkeyhint="done"></div>
      <div class="field" style="display:flex;align-items:center;justify-content:space-between">
        <span style="margin:0">Commit to it daily</span>
        <label class="switch"><input type="checkbox" id="t-commit" checked><i></i></label></div>
      <div class="legend">Committed targets count toward your ring and streak. Uncommitted ones stay on your list for later.</div>
      <button class="wide-btn primary" id="t-save">Add Target</button>`, (sh) => {
      const inp = sh.querySelector("#t-title");
      // Auto-focus only with a mouse/trackpad. On iPhone (esp. inside the HA app) a
      // programmatic focus can grab the field without raising the keyboard, and a
      // later tap on the already-focused field then does nothing.
      if (matchMedia("(pointer: fine)").matches) setTimeout(() => inp.focus(), 250);
      else inp.addEventListener("touchend", () => { if (document.activeElement !== inp) inp.focus(); });
      const save = async () => {
        if (!inp.value.trim()) return inp.focus();
        await mutate("POST", `api/areas/${a.id}/targets`, { title: inp.value, committed: sh.querySelector("#t-commit").checked });
        closeSheet(); renderArea(a.id);
      };
      sh.querySelector("#t-save").onclick = save;
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
    });
  }

  function manageSheet(a) {
    const ICON_UP = `<svg viewBox="0 0 24 24"><path d="M6 15l6-6 6 6" fill="none" stroke="currentColor" stroke-width="2"/></svg>`;
    const ICON_DEL = `<svg viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>`;
    const area = () => S.areas.find((x) => x.id === a.id);
    const draw = (sh) => {
      const ar = area();
      sh.querySelector("#mlist").innerHTML = ar.targets.map((t, i) => `
        <div class="manage-row" data-id="${t.id}">
          <input type="text" value="${esc(t.title)}" maxlength="80">
          <label class="switch" title="Committed"><input type="checkbox" ${t.committed ? "checked" : ""}><i></i></label>
          <button class="mini up" ${i === 0 ? "disabled" : ""} aria-label="Move up">${ICON_UP}</button>
          <button class="mini del" aria-label="Remove">${ICON_DEL}</button>
        </div>`).join("") || `<div class="empty-state">No targets yet.</div>`;
      sh.querySelectorAll(".manage-row").forEach((row) => {
        const id = row.dataset.id;
        const inp = row.querySelector("input[type=text]");
        inp.onchange = () => mutate("PUT", `api/targets/${id}`, { title: inp.value });
        row.querySelector(".switch input").onchange = (e) => mutate("PUT", `api/targets/${id}`, { committed: e.target.checked });
        row.querySelector(".del").onclick = async () => {
          if (!confirm(`Remove “${inp.value}”? Past history is kept.`)) return;
          await mutate("DELETE", `api/targets/${id}`); draw(sh);
        };
        row.querySelector(".up").onclick = async () => {
          const ids = area().targets.map((t) => t.id);
          const i = ids.indexOf(id); if (i < 1) return;
          [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]];
          await mutate("PUT", `api/areas/${a.id}/order`, { ids }); draw(sh);
        };
      });
    };
    openSheet(`<h2>Manage targets</h2>
      <div class="legend">Toggle = committed daily. Rename inline, reorder with the arrow.</div>
      <div id="mlist"></div>
      <div style="height:16px"></div>
      <button class="wide-btn primary" id="mdone">Done</button>`, (sh) => {
      draw(sh);
      sh.querySelector("#mdone").onclick = () => { closeSheet(); renderArea(a.id); };
      $sheet.querySelector(".backdrop").onclick = () => { closeSheet(); renderArea(a.id); };
    });
  }

  function editAreaSheet(a) {
    const isNew = !a;
    a = a || { name: "", identity: "", traits: [] };
    openSheet(`
      <h2>${isNew ? "New protocol area" : `Edit ${esc(a.name)}`}</h2>
      <div class="field"><span>Area</span><input type="text" id="e-name" maxlength="40" value="${esc(a.name)}" placeholder="e.g. Mind"></div>
      <div class="field"><span>Who are you becoming?</span><input type="text" id="e-id" maxlength="60" value="${esc(a.identity)}" placeholder="e.g. Philosopher"></div>
      <div class="field"><span>Virtues / traits (separate with /)</span><input type="text" id="e-traits" value="${esc(a.traits.join(" / "))}" placeholder="Curious / Equanimous"></div>
      <button class="wide-btn primary" id="e-save">${isNew ? "Create" : "Save"}</button>
      ${isNew ? "" : `<button class="danger" id="e-del">Delete this area</button>`}`, (sh) => {
      sh.querySelector("#e-save").onclick = async () => {
        const body = {
          name: sh.querySelector("#e-name").value, identity: sh.querySelector("#e-id").value,
          traits: sh.querySelector("#e-traits").value.split("/").map((s) => s.trim()).filter(Boolean),
        };
        if (isNew) {
          await mutate("POST", "api/areas", body);
          const created = S.areas[S.areas.length - 1];
          closeSheet(); location.hash = `#/area/${created.id}`;
        } else {
          await mutate("PUT", `api/areas/${a.id}`, body);
          closeSheet(); renderArea(a.id);
        }
      };
      const del = sh.querySelector("#e-del");
      if (del) del.onclick = async () => {
        if (!confirm(`Delete the ${a.name} area and its targets?`)) return;
        await mutate("DELETE", `api/areas/${a.id}`); closeSheet(); location.hash = "#/";
      };
    });
  }

  // ------------------------------------------------------------ Commit
  async function renderCommit() {
    viewDate = null;
    await loadState();
    const pick = new Set();
    S.areas.forEach((a) => a.targets.forEach((t) => { if (S.pledged_at ? t.active : t.committed) pick.add(t.id); }));
    $view.innerHTML = `
      <div class="topbar"><a class="icon-btn" href="#/" aria-label="Back">${ARROW_L}</a><div class="title">Commitment</div><span></span></div>
      <div class="reflect-intro">
        <div class="today-head" style="padding:0 0 8px"><div class="date">${esc(longDate(S.today))}</div></div>
        <h1>${S.pledged_at ? "Adjust today's commitment" : "Commit to today"}</h1>
        <p>“First say to yourself what you would be; and then do what you have to do.” — Epictetus</p>
      </div>
      ${S.areas.map((a) => `
        <div class="commit-area">
          <div class="ca-head"><span>${esc(a.name)}</span><b>${esc(a.identity)}</b></div>
          ${a.targets.map((t) => `
            <button class="pick ${pick.has(t.id) ? "on" : ""}" data-id="${t.id}">
              <span class="box"></span><span class="pt">${esc(t.title)}</span>${t.done ? `<span class="pd">done</span>` : t.committed ? "" : `<span class="pd">extra</span>`}
            </button>`).join("") || `<div class="note" style="padding:8px 0">No targets in this area.</div>`}
        </div>`).join("")}
      <div class="commit-foot">
        <div class="sum" id="sum"></div>
        <button class="hold" id="hold"><span class="fill"></span><span class="lbl">Hold to commit</span></button>
        <div class="note" style="text-align:center;margin:10px 0 0">Your rings and streak hold you to this list today.</div>
      </div>`;
    const sum = () => {
      document.getElementById("sum").textContent = pick.size ? `Today I will do ${pick.size} thing${pick.size > 1 ? "s" : ""}.` : "Choose at least one target.";
      document.getElementById("hold").disabled = !pick.size;
    };
    $view.querySelectorAll(".pick").forEach((b) => b.onclick = () => {
      const id = b.dataset.id;
      pick.has(id) ? pick.delete(id) : pick.add(id);
      b.classList.toggle("on", pick.has(id)); buzz(); sum();
    });
    sum();
    // press-and-hold: a deliberate act, not a tap
    const hold = document.getElementById("hold"), fill = hold.querySelector(".fill"), lbl = hold.querySelector(".lbl");
    const MS = 1400; let t0 = 0, raf = 0, fired = false;
    const tick = (now) => {
      const k = Math.min(1, (now - t0) / MS);
      fill.style.width = `${k * 100}%`;
      if (k < 1) { raf = requestAnimationFrame(tick); return; }
      fired = true; lbl.textContent = "Committed";
      try { navigator.vibrate && navigator.vibrate([20, 40, 30]); } catch (_) {}
      mutate("PUT", `api/day/${S.today}/commit`, { target_ids: [...pick] }).then(() => {
        toast("Committed. Now go and do it.");
        setTimeout(() => { location.hash = "#/"; }, 500);
      });
    };
    const start = (e) => {
      if (hold.disabled || fired) return;
      e.preventDefault(); t0 = performance.now(); lbl.textContent = "Keep holding…"; raf = requestAnimationFrame(tick);
    };
    const stop = () => {
      if (fired) return;
      cancelAnimationFrame(raf); fill.style.width = "0"; lbl.textContent = "Hold to commit";
    };
    hold.addEventListener("pointerdown", start);
    ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => hold.addEventListener(ev, stop));
    hold.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  // ------------------------------------------------------------ Reflect
  async function renderReflect(kind) {
    if (kind !== "morning" && kind !== "evening") kind = new Date().getHours() < 14 ? "morning" : "evening";
    await loadState(viewDate);
    const prompts = S.prompts[kind];
    const entry = S[kind];
    const ans = (entry && entry.answers) || {};
    let rating = entry && entry.rating || 0;
    const intro = kind === "morning"
      ? { t: "Morning Preparation", p: "Before the day begins, rehearse it. Decide who you will be, anticipate the obstacles, and separate what is yours to control from what is not." }
      : { t: "Evening Review", p: "“I examine my entire day and go back over what I've done and said, hiding nothing from myself.” — Seneca" };
    $view.innerHTML = `
      <div class="seg"><a href="#/reflect/morning" class="${kind === "morning" ? "on" : ""}">Morning</a><a href="#/reflect/evening" class="${kind === "evening" ? "on" : ""}">Evening</a></div>
      <div class="reflect-intro">
        <div class="today-head" style="padding:0 0 8px"><div class="date">${esc(longDate(S.date))}</div></div>
        <h1>${intro.t}</h1><p>${esc(intro.p)}</p>
      </div>
      ${kind === "morning" ? `<div class="quote" style="margin-top:18px"><p>${esc(S.quote.text)}</p><span>${esc(S.quote.author)}</span></div>` : ""}
      ${prompts.map((p) => `
        <div class="prompt"><label for="p-${p.key}">${esc(p.label)}</label><div class="h">${esc(p.hint)}</div>
        <textarea id="p-${p.key}" data-key="${p.key}">${esc(ans[p.key] || "")}</textarea></div>`).join("")}
      ${kind === "evening" ? `
        <div class="prompt"><label>How well did I live my virtues today?</label>
          <div class="rating">${[1, 2, 3, 4, 5].map((n) => `<button data-n="${n}" class="${rating === n ? "on" : ""}">${roman(n - 1)}</button>`).join("")}</div></div>
        <div class="prompt"><label>Today's protocol</label><div class="h">${S.progress.done} of ${S.progress.total} committed targets hit.</div></div>` : ""}
      <div style="height:26px"></div>
      <button class="wide-btn primary" id="r-save">${entry ? "Update reflection" : "Save reflection"}</button>
      ${entry ? `<div class="saved-at">Last saved ${esc(entry.saved_at.replace("T", " at "))}</div>` : ""}`;
    $view.querySelectorAll(".rating button").forEach((b) => b.onclick = () => {
      rating = Number(b.dataset.n);
      $view.querySelectorAll(".rating button").forEach((x) => x.classList.toggle("on", x === b));
    });
    // auto-grow textareas
    $view.querySelectorAll("textarea").forEach((t) => {
      const fit = () => { t.style.height = "auto"; t.style.height = Math.max(96, t.scrollHeight + 4) + "px"; };
      t.addEventListener("input", fit); fit();
    });
    document.getElementById("r-save").onclick = async () => {
      const answers = {};
      $view.querySelectorAll("textarea").forEach((t) => answers[t.dataset.key] = t.value);
      await mutate("PUT", `api/day/${S.date}/reflection/${kind}`, { answers, rating: rating || undefined });
      buzz();
      if (kind === "morning" && S.date === S.today && !S.pledged_at) { location.hash = "#/commit"; return; }
      toast(kind === "morning" ? "Go meet the day." : "Rest well. Begin again tomorrow.");
      location.hash = "#/";
    };
  }

  // ------------------------------------------------------------ Progress
  async function renderProgress() {
    await loadState();
    const H = await api("GET", "api/history");
    const days = H.days;
    const last30 = days.slice(-30);
    const rate = Math.round(100 * last30.filter((d) => d.complete).length / 30);
    const reflections = last30.filter((d) => d.evening || d.morning).length;
    // align heatmap so columns are weeks starting Sunday
    const pad = parseD(days[0].date).getDay();
    const cells = Array(pad).fill(`<i class="empty"></i>`).concat(days.map((d) => {
      const r = d.total ? d.done / d.total : 0;
      const lvl = d.complete ? 4 : r >= .66 ? 3 : r >= .33 ? 2 : r > 0 ? 1 : 0;
      return `<i class="l${lvl}" title="${d.date}: ${d.done}/${d.total}"></i>`;
    }));
    const labelFor = Object.fromEntries(H.prompts.morning.concat(H.prompts.evening).map((p) => [p.key, p.label]));
    const qa = (e) => e ? Object.entries(e.answers).filter(([, v]) => v).map(([k, v]) =>
      `<div class="qa"><div>${esc(labelFor[k] || k)}</div><p>${esc(v)}</p></div>`).join("") : "";
    $view.innerHTML = `
      <div class="reflect-intro"><h1>Progress</h1><p>Small steps, every day.</p></div>
      <div class="stats">
        <div class="stat"><b>${S.streak}</b><span>Streak</span></div>
        <div class="stat"><b>${S.best_streak}</b><span>Best</span></div>
        <div class="stat"><b>${rate}%</b><span>30-day</span></div>
      </div>
      <div class="section-title">Last 12 weeks</div>
      <div class="card"><div class="heat">${cells.join("")}</div></div>
      <div class="section-title">Targets · last 30 days</div>
      <div class="card bars">${H.targets.map((t) => `
        <div class="r"><div>${esc(t.title)}<small>${esc(t.area)}</small></div>
        <div class="track"><div class="fill" style="width:${Math.round(100 * t.hits30 / 30)}%"></div></div>
        <div class="n">${t.hits30}</div></div>`).join("") || `<div class="empty-state">No targets yet.</div>`}</div>
      <div class="section-title">Journal <span style="font-weight:400;color:var(--muted);font-size:14px">· ${reflections} in 30 days</span></div>
      <div class="card">${H.journal.map((j) => `
        <div class="entry">
          <div class="d">${esc(longDate(j.date))}${j.evening && j.evening.rating ? `<small>Virtue ${roman(j.evening.rating - 1)}</small>` : ""}</div>
          ${j.morning ? `<div class="tag">Morning</div>${qa(j.morning)}` : ""}
          ${j.evening ? `<div class="tag">Evening</div>${qa(j.evening)}` : ""}
        </div>`).join("") || `<div class="empty-state">Your reflections will appear here.</div>`}</div>
      <div style="text-align:center;margin-top:20px"><a href="api/export" download="arete-export.json" style="color:var(--muted);font-size:14px;text-decoration:underline">Export data (JSON)</a></div>`;
  }

  // refresh when returning to the app (e.g. next morning)
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !$sheet.innerHTML) route(); });
  route();
})();
