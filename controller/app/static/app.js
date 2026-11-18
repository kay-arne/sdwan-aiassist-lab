const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(location.origin + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { location.href = "/login"; throw new Error("niet ingelogd"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json().catch(() => ({})) : await r.text();
  if (!r.ok) throw new Error((body && body.detail) || r.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });
// navigator.clipboard bestaat alleen op https/localhost; de controller draait op http, dus val terug op execCommand
async function copy(text) {
  try { if (window.isSecureContext && navigator.clipboard) { await navigator.clipboard.writeText(text); return true; } } catch (e) { /* terugval */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  (document.querySelector(".modal:not([hidden]) .modal-box") || document.body).appendChild(ta);
  ta.focus(); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

let S = null;       // /api/status
let CAT = null;     // /api/scenarios

// --- tabs ------------------------------------------------------------------
function goTo(tab) {
  $$("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + tab));
  if (tab === "setup") loadSettings();
  if (tab === "cockpit") loadCockpit();
  if (tab === "guides") loadGuides();
  if (tab === "updates") loadVersion();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => goTo(b.dataset.tab)));
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) { e.preventDefault(); goTo(g.dataset.goto); } });

// --- joblog ----------------------------------------------------------------
let logTimer = null, activeJobId = null, logDismissed = false;
function followJob(job, onDone) {
  activeJobId = job.id; logDismissed = false;
  document.body.classList.add("logopen");
  $("#job-pill").hidden = true; $("#log-stop").hidden = false;
  $("#log-title").textContent = job.title; $("#log-body").textContent = "";
  clearInterval(logTimer);
  let offset = 0;
  logTimer = setInterval(async () => {
    let j; try { j = await api(`/api/jobs/${job.id}?offset=${offset}`); } catch (e) { return; }
    offset = j.offset;
    if (j.lines.length) { $("#log-body").textContent += j.lines.join("\n") + "\n"; $("#log-body").scrollTop = $("#log-body").scrollHeight; }
    if (j.status !== "running") {
      clearInterval(logTimer); activeJobId = null; $("#log-stop").hidden = true;
      $("#log-title").textContent = `${job.title} — ${j.status}`;
      refresh(); onDone && onDone();
    }
  }, 1000);
}
$("#log-close").onclick = () => { document.body.classList.remove("logopen"); clearInterval(logTimer); activeJobId = null; logDismissed = true; refresh(); };
$("#btn-joblog").onclick = () => {      // lopende job, anders de laatste; het hele log wordt opnieuw getoond
  const jobs = S.jobs || [];
  const job = jobs.find((j) => j.status === "running") || jobs[0];
  if (job) followJob(job);
};
$("#log-stop").onclick = () => activeJobId && post(`/api/jobs/${activeJobId}/stop`);
async function runJob(path, body, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try { followJob(await post(path, body)); } catch (e) { alert(e.message); }
}

function viewer(title, html) { $("#viewer-title").textContent = title; $("#viewer-body").innerHTML = html; $("#viewer").hidden = false; }
$("#viewer-close").onclick = () => ($("#viewer").hidden = true);

// --- status ----------------------------------------------------------------
// wachtwoord verborgen tonen, met oog (tonen/verbergen) en kopieerknop
const pwCell = (v) => v ? `<span class="pwcell"><code class="pw" data-pw="${esc(v)}">••••••••••</code><button type="button" class="eye small" data-reveal title="Tonen/verbergen"></button><button type="button" class="ghost small" data-copy="${esc(v)}">Kopieer</button></span>` : "-";
const stBadge = (s) => `<span class="tag ${s === "running" ? "ok" : s === "absent" ? "absent" : "warn"}">${esc(s)}</span>`;

async function refresh() {
  try { S = await api("/api/status"); } catch (e) { return; }
  const hs = $("#hoststatus");
  hs.hidden = !S.host_error;      // alleen tonen als de host niet bereikbaar is
  renderChecklist(); renderTopology(); renderProblems();
  const runningNow = (S.jobs || []).some((j) => j.status === "running");
  $("#btn-joblog").hidden = !(S.jobs || []).length;
  $("#job-dot").hidden = !runningNow;
  const running = (S.jobs || []).find((j) => j.status === "running");      // na (her)login: lopende job weer volgen
  if (running && !activeJobId && !logDismissed) followJob(running);
}

function renderProblems() {
  const p = (S.settings.problems || []);
  const el = $("#problems");
  el.hidden = !p.length;
  el.innerHTML = p.length ? `<details open><summary>${p.length} waarschuwing(en) bij de provisioning</summary><ul>${p.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : "";
}

function renderChecklist() {
  const s = S.settings, w = s.wan || {};
  const checks = {
    host: !S.host_error,
    image: ["fgt", "fmg", "faz"].every((k) => s.images[k]),
    wan: !!(w.fmg_ip && w.faz_ip && w.gateway),
    admin: s.admin_password_set,
    license: true,
  };
  $$("[data-check]").forEach((b) => { const ok = checks[b.dataset.check]; b.textContent = ok ? "klaar" : "open"; b.className = "badge " + (ok ? "ok" : "todo"); });
  const need = [["host", "Proxmox-verbinding"], ["image", "VM-images (FortiGate, FortiManager, FortiAnalyzer)"], ["wan", "Uplink-IP's"], ["admin", "Beheerwachtwoord"]];
  $("#checklist").innerHTML = need.map(([k, t]) => `<li class="${checks[k] ? "done" : ""}">${checks[k] ? "✔" : "○"} ${t}</li>`).join("");
}

// --- lab/topologie ------------------------------------------------------------
let selected = new Set();
function comp(id, name, status, sub) {
  const lines = (Array.isArray(sub) ? sub : [sub]).filter(Boolean).map((x) => `<span>${x}</span>`).join("");
  return `<label class="comp"><input type="checkbox" data-sel="${id}" ${selected.has(id) ? "checked" : ""}><span class="comp-name">${esc(name)}</span>${stBadge(status)}<span class="comp-sub">${lines}</span></label>`;
}
function renderTopology() {
  const l = S.lab;
  const group = (t, items) => `<div class="card"><h2>${t}</h2><div class="grid">${items.join("")}</div></div>`;
  $("#topology").innerHTML =
    group("Netwerk", [comp("network", `${l.segments.filter((s) => s.present).length}/${l.segments.length} bridges`, l.segments.every((s) => s.present) ? "running" : "absent", l.segments.map((s) => s.bridge).join(", "))]) +
    group("Beheer", [comp("fmg", "FortiManager", l.fmg.status, [`VMID ${l.fmg.vmid}`]), comp("faz", "FortiAnalyzer", l.faz.status, [`VMID ${l.faz.vmid}`])]) +
    group("FortiGates", l.sites.map((s) => comp(`fgt:${s.id}`, `${s.name} (${s.role})`, s.status, [esc(s.hostname), `mgmt ${s.mgmt_ip}`, `WAN-A ${s.wan1} · WAN-B ${s.wan2}`, `LAN ${s.lan_ip}`]))) +
    group("Helpers", [comp("template", "LXC-template", l.template.status, [`VMID ${l.template.vmid}`])].concat(l.nodes.map((n) => comp(`node:${n.name}`, n.name, n.status, [`${n.role}${n.ip ? " · " + n.ip : ""}`])))) +
    group("Configuratie", [comp("config", "FortiManager/FortiAnalyzer-provisioning", S.settings.fmg_provisioned ? "running" : "absent", ["overlay template, SD-WAN, policies, logging"])]);
  $$("[data-sel]").forEach((c) => (c.onchange = () => { c.checked ? selected.add(c.dataset.sel) : selected.delete(c.dataset.sel); updateSel(); }));
  updateSel();
}
function updateSel() {
  const n = selected.size;
  $("#sel-count").textContent = n ? `${n} geselecteerd` : "Niets geselecteerd";
  ["#btn-sel-deploy", "#btn-sel-recreate", "#btn-sel-destroy", "#btn-sel-clear"].forEach((b) => ($(b).disabled = !n));
}
$("#btn-sel-clear").onclick = () => { selected.clear(); renderTopology(); };
$("#btn-sel-deploy").onclick = () => runJob("/api/deploy/selected", { items: [...selected] });
$("#btn-sel-recreate").onclick = () => runJob("/api/deploy/selected", { items: [...selected], recreate: true }, "Gekozen componenten eerst verwijderen en opnieuw uitrollen?");
$("#btn-sel-destroy").onclick = () => runJob("/api/destroy/selected", { items: [...selected] }, "Gekozen componenten verwijderen?");
$("#btn-deploy").onclick = () => runJob("/api/deploy", {}, "Volledig lab uitrollen? Dit duurt lang (30-60 min).");
$("#btn-provision").onclick = () => runJob("/api/provision");
$("#btn-install").onclick = () => runJob("/api/install");
$("#btn-destroy").onclick = () => runJob("/api/destroy", { template: confirm("Ook het LXC-template verwijderen?"), network: confirm("Ook de bridges verwijderen?") }, "ALLE lab-VM's en containers verwijderen?");
$("#btn-devices").onclick = async () => {
  $("#devices").textContent = "ophalen…";
  const d = await api("/api/devices");
  $("#devices").innerHTML = d.error ? `<span class="warn">${esc(d.error)}</span>` : `<table class="kv"><tr><th>Device</th><th>IP</th><th>Verbinding</th><th>Config</th><th>Versie</th></tr>${d.fmg.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.ip)}</td><td>${stBadge(x.conn === "up" ? "running" : x.conn)}</td><td>${esc(x.conf)}</td><td>${esc(x.ver)}</td></tr>`).join("")}</table>`;
};

// --- setup ----------------------------------------------------------------------
document.addEventListener("click", (e) => {
  const rv = e.target.closest("[data-reveal]");
  if (rv) { const c = rv.parentElement.querySelector(".pw"); const open = rv.classList.toggle("open"); c.textContent = open ? c.dataset.pw : "••••••••••"; }
  const eye = e.target.closest("[data-eye]");
  if (eye) { const i = $("#" + eye.dataset.eye); i.type = i.type === "password" ? "text" : "password"; }
});
$("#btn-genpw").onclick = () => { const c = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"; $("#i-adminpw").value = Array.from(crypto.getRandomValues(new Uint8Array(14)), (b) => c[b % c.length]).join(""); $("#i-adminpw").type = "text"; };
$("#btn-host-test").onclick = async () => { const r = await post("/api/host/test"); $("#host-test-result").textContent = r.detail; $("#host-test-result").className = "result " + (r.ok ? "ok" : "err"); };

async function upload(kind, file, prog) {
  prog.hidden = false;
  await new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/upload/image?kind=${kind}&filename=${encodeURIComponent(file.name)}`);
    x.upload.onprogress = (e) => (prog.value = (e.loaded / e.total) * 100);
    x.onload = () => (x.status < 300 ? res() : rej(new Error(x.responseText)));
    x.onerror = () => rej(new Error("upload mislukt"));
    x.send(file);
  });
  prog.hidden = true; loadSettings(); refresh();
}
async function uploadLicense(site, file) {
  const r = await fetch(`/api/upload/license?site=${site}&filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
  if (!r.ok) alert(await r.text()); loadSettings();
}
async function loadSettings() {
  const [s, h] = await Promise.all([api("/api/settings"), api("/api/host")]);
  const topo = CAT || (CAT = await api("/api/scenarios"));
  $("#host-info").innerHTML = h.configured ? `<table class="kv"><tr><th>Host</th><td>${esc(h.host)} (node ${esc(h.node)})</td></tr><tr><th>Gebruiker</th><td>${esc(h.ssh_user)}</td></tr><tr><th>Storage</th><td>${esc(h.storage)}</td></tr><tr><th>Uplink-bridge</th><td>${esc(h.uplink_bridge)}</td></tr><tr><th>VMID-reeks</th><td>${esc(h.vmid_range)}</td></tr><tr><th>UI-wachtwoord</th><td>${pwCell(h.ui_password)}</td></tr></table>` : "<i>Nog niet geconfigureerd door de installer.</i>";
  $("#up-bridge").textContent = h.uplink_bridge || "-";
  $("#img-fields").innerHTML = [["fgt", "FortiGate-VM (FortiOS 7.6)"], ["fmg", "FortiManager 7.6"], ["faz", "FortiAnalyzer 7.6"]].map(([k, t]) =>
    `<div class="field"><label>${t}</label><div class="row"><input type="file" id="f-${k}" accept=".qcow2"><button data-up="${k}">Uploaden</button></div><progress id="p-${k}" max="100" value="0" hidden></progress><div class="current">Huidig: <b>${esc(s.images[k] || "geen")}</b></div></div>`).join("");
  $$("[data-up]").forEach((b) => (b.onclick = () => { const f = $("#f-" + b.dataset.up).files[0]; if (f) upload(b.dataset.up, f, $("#p-" + b.dataset.up)).catch((e) => alert(e.message)); }));
  const w = s.wan || {};
  $("#i-fmgip").value = w.fmg_ip || ""; $("#i-fazip").value = w.faz_ip || ""; $("#i-gw").value = w.gateway || "";
  $("#i-ispip").value = w.isp_ip || ""; $("#i-dns1").value = w.dns1 || ""; $("#i-dns2").value = w.dns2 || "";
  $("#i-adminpw").value = s.admin_password || "";
  $$("input[name=lic]").forEach((r) => (r.checked = r.value === s.license_mode));
  renderLic(s, topo);
}
function renderLic(s, topo) {
  const mode = ($("input[name=lic]:checked") || {}).value || s.license_mode;
  const tok = (id, label, val) => `<div class="field"><label>${label} FortiFlex-token</label><input data-tok="${id}" value="${esc(val || "")}"></div>`;
  let h = "";
  if (mode === "fortiflex") h = tok("fmg", "FortiManager", s.fortiflex.fmg) + tok("faz", "FortiAnalyzer", s.fortiflex.faz) + topo.sites.map((x) => tok("fgt:" + x.id, x.device, s.fortiflex.fgt[x.id])).join("");
  if (mode === "fortiflex_api") {
    const f = s.fortiflex_api || {};
    h = `<div class="field"><label>API-gebruiker</label><input data-ff="username" value="${esc(f.username || "")}"></div>
      <div class="field"><label>API-wachtwoord</label><input type="password" data-ff="password" placeholder="${f.password_set ? "ongewijzigd" : ""}"></div>
      <div class="field"><label>Programma-serienummer</label><input data-ff="program_serial" value="${esc(f.program_serial || "")}" placeholder="leeg = eerste programma"></div>
      <div class="field"><label>Account-ID</label><input data-ff="account_id" value="${esc(f.account_id || "")}" placeholder="optioneel"></div>`;
  }
  $("#ff-actions").hidden = mode !== "fortiflex_api";
  if (mode === "fortiflex_api") loadFlex();
  if (mode === "file") h = topo.sites.map((x) => `<div class="field"><label>${x.device} .lic</label><input type="file" data-lic="${x.id}" accept=".lic"></div>`).join("");
  $("#lic-fields").innerHTML = h;
  $$("[data-lic]").forEach((i) => (i.onchange = () => i.files[0] && uploadLicense(i.dataset.lic, i.files[0])));
}
async function ffCall(url, method = "POST", body) {
  const out = $("#ff-out"); out.hidden = false; out.textContent = "bezig...";
  try {
    const r = await (method === "GET" ? api(url) : post(url, body));
    out.textContent = r.log ? r.log.join("\n") || "klaar" : JSON.stringify(r, null, 2);
  } catch (e) { out.textContent = "Fout: " + e.message; }
  loadFlex();
}
async function loadFlex() {
  const el = $("#ff-table"); if (!el || $("#ff-actions").hidden) return;
  if (!el.children.length) el.innerHTML = '<span class="muted">laden…</span>';
  let rows;
  try { rows = await api("/api/fortiflex/entitlements"); } catch (e) { el.innerHTML = `<span class="warn">${esc(e.message)}</span>`; return; }
  const st = (s) => !s ? '<span class="tag absent">geen</span>' : `<span class="tag ${s === "ACTIVE" ? "ok" : s === "PENDING" ? "" : "warn"}">${esc(s)}</span>`;
  const ICON = {
    create: '<path d="M12 5v14M5 12h14"/>',
    token: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    reactivate: '<path d="M7 4l13 8-13 8z"/>',
    unlink: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/><path d="M3 3l18 18"/>',
  };
  const TIP = { create: "Entitlement aanmaken", token: "Nieuwe token genereren", stop: "Stoppen (geen verbruik meer)", reactivate: "Reactiveren", unlink: "Ontkoppelen van de controller" };
  const btn = (d, a, _t, cls = "") => `<button type="button" class="icon-btn ${cls}" data-ffa="${a}" data-ffd="${d}" data-tip="${TIP[a]}" aria-label="${TIP[a]}"><svg viewBox="0 0 24 24">${ICON[a]}</svg></button>`;
  el.innerHTML = `<table class="kv fftable"><tr><th>Apparaat</th><th>Entitlement</th><th>Status</th><th>Token</th><th></th></tr>${rows.map((r) => `<tr>
    <td>${esc(r.label)}</td><td class="mono">${esc(r.serial || "-")}</td><td>${st(r.status)}</td>
    <td>${r.token_status === "USED" ? "gebruikt" : r.token_status === "NOTUSED" ? "ongebruikt" : "-"}</td>
    <td class="ffbtns">${!r.serial ? btn(r.device, "create", "Aanmaken", "primary")
      : btn(r.device, "token", "Nieuwe token") + (r.status === "STOPPED" ? btn(r.device, "reactivate", "Reactiveren") : btn(r.device, "stop", "Stoppen")) + btn(r.device, "unlink", "Ontkoppelen", "ghost")}</td></tr>`).join("")}</table>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-ffa]"); if (!b) return;
  if (b.dataset.ffa === "unlink" && !confirm("Koppeling met deze entitlement verwijderen? (De entitlement zelf blijft in FortiFlex bestaan; stop hem eerst.)")) return;
  ffCall("/api/fortiflex/entitlement", "POST", { device: b.dataset.ffd, action: b.dataset.ffa });
});
$("#ff-test").onclick = () => ffCall("/api/fortiflex/test");
$("#ff-configs").onclick = () => ffCall("/api/fortiflex/configs");
$("#ff-refresh").onclick = () => loadFlex();
$("#ff-activate-all").onclick = () => ffCall("/api/fortiflex/entitlement", "POST", { device: "all", action: "activate" });
$("#ff-create-all").onclick = () => ffCall("/api/fortiflex/entitlement", "POST", { device: "all", action: "create" });
$("#ff-stop").onclick = () => confirm("Alle FortiFlex-entitlements van dit lab stoppen?") && ffCall("/api/fortiflex/entitlement", "POST", { device: "all", action: "stop" });
$$("input[name=lic]").forEach((r) => (r.onchange = async () => renderLic(await api("/api/settings"), CAT)));
$$("[data-save]").forEach((b) => (b.onclick = async () => {
  const k = b.dataset.save; let body = {};
  if (k === "wan") body = { wan: { fmg_ip: $("#i-fmgip").value, faz_ip: $("#i-fazip").value, gateway: $("#i-gw").value, isp_ip: $("#i-ispip").value || "dhcp", dns1: $("#i-dns1").value, dns2: $("#i-dns2").value } };
  if (k === "admin") body = { admin_password: $("#i-adminpw").value };
  if (k === "license") {
    body = { license_mode: ($("input[name=lic]:checked") || {}).value || "none" };
    if (body.license_mode === "fortiflex_api") {
      body.fortiflex_api = {};
      $$("[data-ff]").forEach((i) => (body.fortiflex_api[i.dataset.ff] = i.value));
    }
    const fgt = {};
    $$("[data-tok]").forEach((i) => { const id = i.dataset.tok; if (id === "fmg") body.fmg_fortiflex_token = i.value; else if (id === "faz") body.faz_fortiflex_token = i.value; else fgt[id.slice(4)] = i.value; });
    if (Object.keys(fgt).length) body.fgt_fortiflex_tokens = fgt;
  }
  const r = document.querySelector(`[data-result=${k}]`);
  try { await post("/api/settings", body); r.textContent = "opgeslagen"; r.className = "result ok"; refresh(); } catch (e) { r.textContent = e.message; r.className = "result err"; }
}));

// --- cockpit --------------------------------------------------------------------------
async function loadCockpit() {
  CAT = await api("/api/scenarios");
  const opt = (a, f) => a.map(f).join("");
  $("#m-site").innerHTML = opt(CAT.sites.filter((s) => s.role === "spoke"), (s) => `<option value="${s.id}">${esc(s.name)}</option>`);
  $("#m-tsite").innerHTML = `<option value="all">alle branches</option>` + opt(CAT.sites.filter((s) => s.role === "spoke"), (s) => `<option value="${s.id}">${esc(s.name)}</option>`);
  $("#m-preset").innerHTML = opt(CAT.presets, (p) => `<option>${p}</option>`);
  $("#m-hub").innerHTML = opt(CAT.sites.filter((s) => s.role === "hub"), (s) => `<option value="${s.id}">${esc(s.name)}</option>`);
  $("#h-dev").innerHTML = opt(CAT.sites, (s) => `<option value="${s.device}">${esc(s.name)}</option>`);
}
function aiRow(q) {
  return `<div class="ai-row"><div class="ai-head"><span class="tag">${esc(q.where)}</span><button class="ghost small" data-copy="${esc(q.prompt)}">Kopieer</button></div><div class="ai-prompt">${esc(q.prompt)}</div><div class="hint">${esc(q.expect)}</div></div>`;
}
document.addEventListener("click", (e) => { const c = e.target.closest("[data-copy]"); if (c) { copy(c.dataset.copy).then((ok) => { c.textContent = ok ? "Gekopieerd" : "Mislukt"; setTimeout(() => (c.textContent = "Kopieer"), 1500); }); } });
const act = (a) => post("/api/action", a).then((j) => followJob(j)).catch((e) => alert(e.message));
$("#btn-reset").onclick = () => act({ do: "clear" });
$("#btn-traffic-stop").onclick = () => act({ do: "traffic_stop", background: false });
$("#m-impair").onclick = () => act({ do: "impair", site: $("#m-site").value, transport: $("#m-tr").value, delay: +$("#m-delay").value || null, jitter: +$("#m-jitter").value || null, loss: +$("#m-loss").value || null });
$("#m-down").onclick = () => act({ do: "link", site: $("#m-site").value, transport: $("#m-tr").value, state: "down" });
$("#m-up").onclick = () => act({ do: "link", site: $("#m-site").value, transport: $("#m-tr").value, state: "up" });
$("#m-traffic").onclick = () => act({ do: "traffic", site: $("#m-tsite").value, preset: $("#m-preset").value });
$("#m-hubstop").onclick = () => act({ do: "vm", site: $("#m-hub").value, op: "stop" });
$("#m-hubstart").onclick = () => act({ do: "vm", site: $("#m-hub").value, op: "start" });
$("#btn-health").onclick = async () => {
  $("#health").textContent = "ophalen…";
  const r = await api("/api/sdwan/" + $("#h-dev").value);
  $("#health").innerHTML = r.error ? `<span class="warn">${esc(r.error)}</span>` : r.health.length ? `<table class="kv"><tr><th>Health-check</th><th>Member</th><th>Status</th><th>Latency</th><th>Jitter</th><th>Loss</th></tr>${r.health.map((x) => `<tr><td>${esc(x.health_check)}</td><td>${esc(x.member)}</td><td>${stBadge(x.status === "alive" ? "running" : x.status)}</td><td>${esc(x.latency)}</td><td>${esc(x.jitter)}</td><td>${esc(x.loss)}</td></tr>`).join("")}</table>` : "Geen data.";
};
$("#btn-links").onclick = async () => {
  const r = await api("/api/links");
  $("#health").innerHTML = r.error ? `<span class="warn">${esc(r.error)}</span>` : `<table class="kv"><tr><th>WAN-link</th><th>Site</th><th>Transport</th><th>Status</th><th>Impairment</th></tr>${Object.entries(r).map(([k, v]) => `<tr><td>${k}</td><td>${esc(v.site)}</td><td>${esc(v.transport)}</td><td>${v.up ? "up" : "<b>down</b>"}</td><td class="mono">${esc(v.netem || "-")}</td></tr>`).join("")}</table>`;
};

// --- FortiAI ------------------------------------------------------------------------------
/* Labguides: hoofdstukken uit lab/guides.yaml met uitleg, actieknoppen, live checks en FortiAI-prompts */
let GUIDES = null, curGuide = null;
const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
async function loadGuides() {
  if (!GUIDES) { [GUIDES, CAT] = await Promise.all([api("/api/guides"), api("/api/scenarios")]); }
  if (!curGuide) curGuide = GUIDES[0].id;
  $("#guide-nav").innerHTML = `<button class="access-btn" data-access title="Adressen en wachtwoorden van alle lab-onderdelen"><b>Toegang</b><small>URL's en inloggegevens</small></button>` + GUIDES.map((g) => `<button class="${g.id === curGuide ? "active" : ""}" data-guide="${g.id}"><b>${esc(g.title)}</b><small>${esc(g.summary || "")}</small></button>`).join("");
  $$("[data-guide]").forEach((b) => (b.onclick = () => { curGuide = b.dataset.guide; loadGuides(); window.scrollTo(0, 0); }));
  const g = GUIDES.find((x) => x.id === curGuide);
  const sc = g.scenario ? CAT.scenarios.find((s) => s.id === g.scenario) : null;
  const step = (s, i) => {
    if (s.text) return `<p>${md(s.text)}</p>`;
    if (s.image) return `<figure class="shot"><a href="/static/guide/${esc(s.image)}" target="_blank" rel="noopener"><img src="/static/guide/${esc(s.image)}" alt="${esc(s.caption || "")}" loading="lazy"></a><figcaption>${md(s.caption || "")}</figcaption></figure>`;
    if (s.tip) return `<div class="tipbox">${md(s.tip)}</div>`;
    if (s.buttons) return `<div class="gbtns">${s.buttons.map((b, k) => `<button class="${b.check ? "" : "primary"}" data-gs="${i}.${k}">${esc(b.label)}</button>`).join("")}</div><div class="gout" id="gout-${i}"></div>`;
    if (s.ai && sc) return (s.ai === "all" ? sc.fortiai : s.ai.map((n) => sc.fortiai[n])).map(aiRow).join("");
    if (s.access) return `<div class="gbtns"><button data-access>Toegang openen</button></div>`;
    if (s.fortiai_status) return `<div class="gbtns"><button id="btn-ai-status">FortiAI-status controleren</button></div><div id="ai-status" class="muted"></div>`;
    if (s.docs) return `<div id="g-docs" class="doclist"></div>`;
    return "";
  };
  $("#guide-body").innerHTML = `<h2>${esc(g.title)}</h2>` + g.steps.map(step).join("");
  $$("[data-gs]").forEach((b) => (b.onclick = () => {
    const [i, k] = b.dataset.gs.split(".").map(Number), btn = g.steps[i].buttons[k], out = $("#gout-" + i);
    if (btn.action) return act(btn.action);
    out.innerHTML = '<span class="muted">ophalen…</span>';
    if (btn.check === "links") showLinks(out); else showHealth(out, btn.device);
  }));
  if ($("#g-docs")) loadDocs();
  const st = $("#btn-ai-status"); if (st) st.onclick = aiStatus;
  watchTraffic();
}
let trafficTimer = null;
async function loadTraffic() {
  const box = $("#traffic-box");
  if (!box || !$("#tab-guides").classList.contains("active")) { clearInterval(trafficTimer); trafficTimer = null; return; }
  let rows;
  try { rows = await api("/api/traffic/status"); } catch (e) { box.innerHTML = `<b>Verkeer</b><span class="warn">${esc(e.message)}</span>`; return; }
  const fmt = (n) => (n || 0).toLocaleString("nl-NL");
  const ago = (r) => { const s = Math.max(0, Math.round((r.now || 0) - (r.started || 0))); return !r.started ? "" : s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} u`; };
  const allOn = rows.length && rows.every((r) => r.running);
  box.innerHTML = `<b>Verkeer</b> <span class="tag ${allOn ? "ok" : "warn"}">${allOn ? "draait" : "niet overal actief"}</span>` + rows.map((r) => {
    const on = r.running, tag = r.state !== "running" ? "container uit" : on ? "actief" : "gestopt";
    return `<div class="trow"><span class="dot ${on ? "on" : "off"}"></span><div><b>${esc(r.name)}</b> <small>${tag}${on ? " · " + ago(r) : ""}</small>
      ${r.requests !== undefined ? `<div class="tstat">${fmt(r.requests)} verzoeken · ${fmt(r.blocked)} geblokkeerd · ${fmt(r.errors)} fouten · ${fmt(r.mb)} MB</div>` : `<div class="tstat">${esc(r.detail || "")}</div>`}</div></div>`;
  }).join("") + (allOn ? "" : `<div class="gbtns"><button class="primary small" id="tb-start">Achtergrondverkeer starten</button></div>`);
  const b = $("#tb-start"); if (b) b.onclick = () => act({ do: "traffic", site: "all", preset: "background" });
}
function watchTraffic() { loadTraffic(); if (!trafficTimer) trafficTimer = setInterval(loadTraffic, 15000); }

async function showHealth(el, dev) {
  try {
    const r = await api("/api/sdwan/" + dev);
    el.innerHTML = r.error ? `<span class="warn">${esc(r.error)}</span>` : r.health.length ? `<table class="kv"><tr><th>Health-check</th><th>Member</th><th>Status</th><th>Latency</th><th>Jitter</th><th>Loss</th></tr>${r.health.map((x) => `<tr><td>${esc(x.health_check)}</td><td>${esc(x.member)}</td><td>${stBadge(x.status === "alive" ? "running" : x.status)}</td><td>${esc(x.latency)}</td><td>${esc(x.jitter)}</td><td>${esc(x.loss)}</td></tr>`).join("")}</table>` : "Geen data.";
  } catch (e) { el.innerHTML = `<span class="warn">${esc(e.message)}</span>`; }
}
async function showLinks(el) {
  try {
    const r = await api("/api/links");
    el.innerHTML = r.error ? `<span class="warn">${esc(r.error)}</span>` : `<table class="kv"><tr><th>WAN-link</th><th>Site</th><th>Transport</th><th>Status</th><th>Impairment</th></tr>${Object.entries(r).map(([k, v]) => `<tr><td>${k}</td><td>${esc(v.site)}</td><td>${esc(v.transport)}</td><td>${v.up ? "up" : "<b>down</b>"}</td><td class="mono">${esc(v.netem || "-")}</td></tr>`).join("")}</table>`;
  } catch (e) { el.innerHTML = `<span class="warn">${esc(e.message)}</span>`; }
}
async function aiStatus() {
  $("#ai-status").textContent = "controleren…";
  const r = await api("/api/fortiai/status");
  $("#ai-status").innerHTML = Object.entries(r).map(([k, v]) => `<div><b>${k}</b>: ${v.licensed === true ? '<span class="tag ok">FortiAI-contract aanwezig</span>' : v.licensed === false ? '<span class="tag warn">geen FortiAI-contract gevonden</span>' : '<span class="tag warn">onbekend</span>'}<pre class="logbox">${esc(v.raw)}</pre></div>`).join("");
}

// --- toegang & docs ----------------------------------------------------------------------------
async function openAccess() {
  viewer("Toegang", '<div class="access pad"><span class="muted">laden…</span></div>');
  const a = await api("/api/access");
  const row = (x) => `<div class="acc"><div class="acc-title"><b>${esc(x.name)}</b></div><div class="muted">${esc(x.note)}</div>
    <div class="acc-url">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a>` : "<i class=\"muted\">nog geen adres (niet uitgerold?)</i>"}</div>
    <div class="acc-cred"><code>${esc(x.user)}</code>${pwCell(x.password)}</div></div>`;
  viewer("Toegang", '<div class="access pad"><h3>Platformen en FortiGates</h3>' + a.platforms.map(row).join("") + "<h3>Traffic-Gen</h3>" + a.trafficgen.map(row).join("") + "</div>");
}
document.addEventListener("click", (e) => { if (e.target.closest("[data-access]")) openAccess(); });
async function loadDocs() {
  const docs = await api("/api/docs");
  $("#g-docs").innerHTML = docs.map((d) => `<a href="#" class="doc" data-doc="${d.id}">${esc(d.title)}</a>`).join("");
  $$("[data-doc]").forEach((l) => (l.onclick = async (e) => { e.preventDefault(); viewer(l.textContent, await api("/api/docs/" + l.dataset.doc)); }));
}

// --- updates --------------------------------------------------------------------------------------------
async function loadVersion() {
  const v = await api("/api/version");
  $("#version-text").textContent = v.git ? v.commit : "n/a";
  $("#upd-current").textContent = v.git ? `${v.commit} ${v.subject}` : "geen git-checkout"; $("#upd-branch").textContent = v.branch || "-";
}
async function checkUpdates() {      // stille controle: stipje op het buildnummer als er een update is
  try { const r = await post("/api/update/check"); $("#update-dot").hidden = !r.behind; return r; } catch (e) { return null; }
}
$("#btn-upd-check").onclick = async () => {
  try {
    const r = await post("/api/update/check");
    $("#update-dot").hidden = !r.behind;
    $("#upd-result").textContent = r.behind ? `${r.behind} nieuwe commit(s)` : "up-to-date";
    $("#btn-upd-run").hidden = !r.behind;
    $("#upd-commits").innerHTML = r.commits.map((c) => `<div>${esc(c.commit)} ${esc(c.subject)}</div>`).join("");
  } catch (e) { $("#upd-result").textContent = e.message; }
};
$("#btn-upd-run").onclick = async () => {
  await post("/api/update/run"); $("#upd-log").hidden = false;
  let off = 0; const t = setInterval(async () => { const r = await api("/api/update/log?offset=" + off); off = r.offset; $("#upd-log").textContent += r.lines.join("\n") + (r.lines.length ? "\n" : ""); if (!r.running) { clearInterval(t); setTimeout(() => location.reload(), 3000); } }, 1500);
};

loadSettings(); refresh(); loadVersion(); checkUpdates(); setInterval(refresh, 15000); setInterval(checkUpdates, 6 * 3600 * 1000);
