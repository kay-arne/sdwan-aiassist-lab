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
const copy = (t) => navigator.clipboard && navigator.clipboard.writeText(t);

let S = null;       // /api/status
let CAT = null;     // /api/scenarios

// --- tabs ------------------------------------------------------------------
function goTo(tab) {
  $$("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + tab));
  if (tab === "setup") loadSettings();
  if (tab === "cockpit") loadCockpit();
  if (tab === "fortiai") loadFortiAI();
  if (tab === "access") loadAccess();
  if (tab === "updates") loadVersion();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => goTo(b.dataset.tab)));
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) { e.preventDefault(); goTo(g.dataset.goto); } });

// --- joblog ----------------------------------------------------------------
let logTimer = null, activeJobId = null;
function followJob(job, onDone) {
  activeJobId = job.id;
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
$("#log-close").onclick = () => { document.body.classList.remove("logopen"); clearInterval(logTimer); refresh(); };
$("#log-stop").onclick = () => activeJobId && post(`/api/jobs/${activeJobId}/stop`);
async function runJob(path, body, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try { followJob(await post(path, body)); } catch (e) { alert(e.message); }
}

function viewer(title, html) { $("#viewer-title").textContent = title; $("#viewer-body").innerHTML = html; $("#viewer").hidden = false; }
$("#viewer-close").onclick = () => ($("#viewer").hidden = true);

// --- status ----------------------------------------------------------------
const stBadge = (s) => `<span class="tag ${s === "running" ? "ok" : s === "absent" ? "" : "warn"}">${esc(s)}</span>`;

async function refresh() {
  try { S = await api("/api/status"); } catch (e) { return; }
  const hs = $("#hoststatus");
  hs.textContent = S.host_error ? "host niet bereikbaar" : "host OK"; hs.className = "pill " + (S.host_error ? "bad" : "good");
  renderChecklist(); renderTopology(); renderProblems();
}

function renderProblems() {
  const p = (S.settings.problems || []);
  const el = $("#problems");
  el.hidden = !p.length;
  el.innerHTML = p.length ? `<b>${p.length} waarschuwing(en) bij de provisioning:</b><ul>${p.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
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
  return `<label class="comp"><input type="checkbox" data-sel="${id}" ${selected.has(id) ? "checked" : ""}> <b>${esc(name)}</b> ${stBadge(status)}<span class="sub">${sub || ""}</span></label>`;
}
function renderTopology() {
  const l = S.lab;
  const group = (t, items) => `<div class="card"><h2>${t}</h2><div class="grid">${items.join("")}</div></div>`;
  $("#topology").innerHTML =
    group("Netwerk", [comp("network", `${l.segments.filter((s) => s.present).length}/${l.segments.length} bridges`, l.segments.every((s) => s.present) ? "running" : "absent", "vmbr600, 611-612, 621-623, 631-635, 641-645")]) +
    group("Beheer", [comp("fmg", "FortiManager", l.fmg.status, `VMID ${l.fmg.vmid}`), comp("faz", "FortiAnalyzer", l.faz.status, `VMID ${l.faz.vmid}`)]) +
    group("FortiGates", l.sites.map((s) => comp(`fgt:${s.id}`, `${s.name} (${s.role})`, s.status, `${s.hostname}<br>mgmt ${s.mgmt_ip} · A ${s.wan1} · B ${s.wan2} · LAN ${s.lan_ip}`))) +
    group("Helpers", [comp("template", "LXC-template", l.template.status, `VMID ${l.template.vmid}`)].concat(l.nodes.map((n) => comp(`node:${n.name}`, n.name, n.status, `${n.role}${n.ip ? " · " + n.ip : ""}`)))) +
    group("Configuratie", [comp("config", "FortiManager/FortiAnalyzer-provisioning", S.settings.fmg_provisioned ? "running" : "absent", "overlay template, SD-WAN, policies, logging")]);
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
$("#btn-labnode").onclick = () => runJob("/api/update-labnode");
$("#btn-destroy").onclick = () => runJob("/api/destroy", { template: confirm("Ook het LXC-template verwijderen?"), network: confirm("Ook de bridges verwijderen?") }, "ALLE lab-VM's en containers verwijderen?");
$("#btn-config").onclick = async () => viewer("Gegenereerde config", `<pre>${esc(await api("/api/config"))}</pre>`);
$("#btn-devices").onclick = async () => {
  $("#devices").textContent = "ophalen…";
  const d = await api("/api/devices");
  $("#devices").innerHTML = d.error ? `<span class="warn">${esc(d.error)}</span>` : `<table class="kv"><tr><th>Device</th><th>IP</th><th>Verbinding</th><th>Config</th><th>Versie</th></tr>${d.fmg.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.ip)}</td><td>${stBadge(x.conn === "up" ? "running" : x.conn)}</td><td>${esc(x.conf)}</td><td>${esc(x.ver)}</td></tr>`).join("")}</table>`;
};

// --- setup ----------------------------------------------------------------------
document.addEventListener("click", (e) => {
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
  $("#host-info").innerHTML = h.configured ? `<table class="kv"><tr><th>Host</th><td>${esc(h.host)} (node ${esc(h.node)})</td></tr><tr><th>Gebruiker</th><td>${esc(h.ssh_user)}</td></tr><tr><th>Storage</th><td>${esc(h.storage)}</td></tr><tr><th>Uplink-bridge</th><td>${esc(h.uplink_bridge)}</td></tr><tr><th>VMID-reeks</th><td>${esc(h.vmid_range)}</td></tr><tr><th>UI-wachtwoord</th><td class="mono">${esc(h.ui_password)}</td></tr></table>` : "<i>Nog niet geconfigureerd door de installer.</i>";
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
  if (mode === "file") h = topo.sites.map((x) => `<div class="field"><label>${x.device} .lic</label><input type="file" data-lic="${x.id}" accept=".lic"></div>`).join("");
  $("#lic-fields").innerHTML = h;
  $$("[data-lic]").forEach((i) => (i.onchange = () => i.files[0] && uploadLicense(i.dataset.lic, i.files[0])));
}
$$("input[name=lic]").forEach((r) => (r.onchange = async () => renderLic(await api("/api/settings"), CAT)));
$$("[data-save]").forEach((b) => (b.onclick = async () => {
  const k = b.dataset.save; let body = {};
  if (k === "wan") body = { wan: { fmg_ip: $("#i-fmgip").value, faz_ip: $("#i-fazip").value, gateway: $("#i-gw").value, isp_ip: $("#i-ispip").value || "dhcp", dns1: $("#i-dns1").value, dns2: $("#i-dns2").value } };
  if (k === "admin") body = { admin_password: $("#i-adminpw").value };
  if (k === "license") {
    body = { license_mode: ($("input[name=lic]:checked") || {}).value || "none" };
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
  $("#scenarios").innerHTML = CAT.scenarios.map((sc) => `
    <div class="demo-group card"><h2>${esc(sc.title)}</h2><p class="help">${esc(sc.story)}</p>
      <div class="demo-actions"><button class="primary" data-run="${sc.id}">Start scenario</button>${sc.cleanup.length ? `<button data-clean="${sc.id}">Opruimen</button>` : ""}</div>
      ${sc.fortiai.map(aiRow).join("")}</div>`).join("");
  $$("[data-run]").forEach((b) => (b.onclick = () => runJob("/api/scenarios/run", { id: b.dataset.run, phase: "setup" })));
  $$("[data-clean]").forEach((b) => (b.onclick = () => runJob("/api/scenarios/run", { id: b.dataset.clean, phase: "cleanup" })));
}
function aiRow(q) {
  return `<div class="ev"><span class="tag">${esc(q.where)}</span> <span class="ai-prompt">${esc(q.prompt)}</span> <button class="ghost small" data-copy="${esc(q.prompt)}">Kopieer</button><div class="hint">${esc(q.expect)}</div></div>`;
}
document.addEventListener("click", (e) => { const c = e.target.closest("[data-copy]"); if (c) { copy(c.dataset.copy); c.textContent = "Gekopieerd"; setTimeout(() => (c.textContent = "Kopieer"), 1500); } });
const act = (a) => post("/api/action", a).then((j) => followJob(j)).catch((e) => alert(e.message));
$("#btn-reset").onclick = () => act({ do: "clear" });
$("#btn-traffic-stop").onclick = () => act({ do: "traffic_stop" });
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
  $("#health").innerHTML = r.error ? `<span class="warn">${esc(r.error)}</span>` : `<table class="kv"><tr><th>ISP-interface</th><th>Site</th><th>Transport</th><th>Status</th><th>Impairment</th></tr>${Object.entries(r).map(([k, v]) => `<tr><td>${k}</td><td>${esc(v.site)}</td><td>${esc(v.transport)}</td><td>${v.up ? "up" : "<b>down</b>"}</td><td class="mono">${esc(v.netem || "-")}</td></tr>`).join("")}</table>`;
};

// --- FortiAI ------------------------------------------------------------------------------
async function loadFortiAI() {
  CAT = CAT || (await api("/api/scenarios"));
  const draw = () => {
    const q = ($("#ai-filter").value || "").toLowerCase();
    $("#ai-prompts").innerHTML = CAT.scenarios.map((sc) => {
      const rows = sc.fortiai.filter((x) => !q || (x.prompt + x.expect + x.where + sc.title).toLowerCase().includes(q));
      return rows.length ? `<h3>${esc(sc.title)}</h3>${rows.map(aiRow).join("")}` : "";
    }).join("");
  };
  $("#ai-filter").oninput = draw; draw();
}
$("#btn-ai-status").onclick = async () => {
  $("#ai-status").textContent = "controleren…";
  const r = await api("/api/fortiai/status");
  $("#ai-status").innerHTML = Object.entries(r).map(([k, v]) => `<div><b>${k}</b>: ${v.licensed === true ? '<span class="tag ok">FortiAI-contract aanwezig</span>' : v.licensed === false ? '<span class="tag warn">geen FortiAI-contract gevonden</span>' : '<span class="tag warn">onbekend</span>'}<pre class="logbox">${esc(v.raw)}</pre></div>`).join("");
};

// --- toegang & docs ----------------------------------------------------------------------------
async function loadAccess() {
  const a = await api("/api/access");
  const row = (x) => `<div class="acc"><b>${esc(x.name)}</b> <span class="muted">${esc(x.note)}</span><br>${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a>` : "<i>(nog geen adres)</i>"} · <code>${esc(x.user)}</code> / <code>${esc(x.password)}</code></div>`;
  $("#access").innerHTML = "<h3>Platformen en FortiGates</h3>" + a.platforms.map(row).join("") + "<h3>Traffic-Gen</h3>" + a.trafficgen.map(row).join("");
  const docs = await api("/api/docs");
  $("#docs").innerHTML = docs.map((d) => `<a href="#" class="doc" data-doc="${d.id}">${esc(d.title)}</a>`).join("");
  $$("[data-doc]").forEach((l) => (l.onclick = async (e) => { e.preventDefault(); viewer(l.textContent, await api("/api/docs/" + l.dataset.doc)); }));
}

// --- updates --------------------------------------------------------------------------------------------
async function loadVersion() {
  const v = await api("/api/version");
  $("#version-pill").textContent = v.git ? v.commit : "n/a";
  $("#upd-current").textContent = v.git ? `${v.commit} ${v.subject}` : "geen git-checkout"; $("#upd-branch").textContent = v.branch || "-";
}
$("#btn-upd-check").onclick = async () => {
  try {
    const r = await post("/api/update/check");
    $("#upd-result").textContent = r.behind ? `${r.behind} nieuwe commit(s)` : "up-to-date";
    $("#btn-upd-run").hidden = !r.behind;
    $("#upd-commits").innerHTML = r.commits.map((c) => `<div>${esc(c.commit)} ${esc(c.subject)}</div>`).join("");
  } catch (e) { $("#upd-result").textContent = e.message; }
};
$("#btn-upd-run").onclick = async () => {
  await post("/api/update/run"); $("#upd-log").hidden = false;
  let off = 0; const t = setInterval(async () => { const r = await api("/api/update/log?offset=" + off); off = r.offset; $("#upd-log").textContent += r.lines.join("\n") + (r.lines.length ? "\n" : ""); if (!r.running) { clearInterval(t); setTimeout(() => location.reload(), 3000); } }, 1500);
};

loadSettings(); refresh(); loadVersion(); setInterval(refresh, 15000);
