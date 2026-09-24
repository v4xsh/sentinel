const API = "/api";
let CURRENT = null;
// Only the latest renderGraph call is allowed to touch the DOM.
let GRAPH_RENDER_TOKEN = 0;

function activate(v) {
  document.querySelectorAll("nav#views button").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("active", s.id === `view-${v}`));
}
document.querySelectorAll("nav#views button").forEach(btn => {
  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    activate(btn.dataset.view);
    if (btn.dataset.view === "cases") loadList();
    if (btn.dataset.view === "backtest") loadBacktest();
    if (btn.dataset.view === "memory") loadMemory();
    if (btn.dataset.view === "monitor") loadMonitor();
    if (btn.dataset.view === "findings") loadFindings();
    if (btn.dataset.view === "graph" && CURRENT) renderGraph(CURRENT);
  });
});

// ---- List --------------------------------------------------------------
async function loadList() {
  const el = document.getElementById("cases-list");
  el.innerHTML = "Loading…";
  const rows = await (await fetch(`${API}/cases`)).json();
  const tr = r => `
    <tr class="clickable" data-id="${r.case_id}">
      <td><b>${r.case_id}</b></td>
      <td><span class="badge ${r.verdict}">${r.verdict}</span></td>
      <td>${(r.fraud_probability || 0).toFixed(3)}</td>
      <td>${r.pattern}</td>
      <td>${r.status}</td>
      <td>$${(r.exposure_usd || 0).toFixed(2)}</td>
      <td>${r.sar_file ? "SAR" : ""}</td>
      <td class="muted">${r.graph_case_id || "—"}</td>
    </tr>`;
  el.innerHTML = `<table><thead><tr>
    <th>case</th><th>verdict</th><th>p</th><th>pattern</th>
    <th>status</th><th>exposure</th><th>sar</th><th>vertex</th>
  </tr></thead><tbody>${rows.map(tr).join("")}</tbody></table>`;
  el.querySelectorAll("tr.clickable").forEach(row =>
    row.addEventListener("click", () => open(row.dataset.id)));
}

// ---- Case detail + case-scoped views -----------------------------------
async function open(cid) {
  CURRENT = await (await fetch(`${API}/cases/${cid}`)).json();
  ["detail","ledger","timeline","actions","sar","graph"].forEach(v =>
    document.querySelector(`button[data-view=${v}]`).disabled = false);
  document.getElementById("detail-title").textContent = `${cid} — ${CURRENT.case.verdict}`;
  renderDetail(); renderLedger(); renderTimeline(); renderActions(); renderSar();
  // Do NOT render the graph here — it fires again when the Graph tab
  // is clicked, which used to append a second SVG. The tab-click
  // handler in the nav loop is the single trigger.
  activate("detail");
}

function renderDetail() {
  document.getElementById("case-detail-body").textContent = JSON.stringify(CURRENT, null, 2);
  const c = CURRENT.case;
  document.getElementById("case-status").innerHTML = `
    <p><b>Status:</b> ${c.status} <br>
    <b>Verdict:</b> <span class="badge ${c.verdict}">${c.verdict}</span> ·
    <b>p(fraud) = ${c.fraud_probability.toFixed(3)}</b><br>
    <b>Pattern:</b> ${c.pattern}${c.pattern_description ? ` — ${c.pattern_description}` : ""}<br>
    <b>Exposure:</b> $${c.exposure_usd.toFixed(2)}<br>
    <b>Affected txns:</b> ${(c.affected_txn_ids || []).join(", ") || "—"}<br>
    <b>Graph vertex:</b> ${c.graph_case_id || "—"}</p>`;
  document.getElementById("case-trigger").textContent = CURRENT.stop_reason || "";
}

function renderLedger() {
  const ev = CURRENT.case.evidence || [];
  // p trajectory: intercept + cumulative sum of coef-based Evidence items.
  const points = [];
  let cum = 0;
  for (const e of ev) {
    // Guess log_lr from ref if the answer doesn't carry it — we don't have
    // log_lr in the persisted evidence, so this is best-effort.
    // Persisted evidence has {claim, source, ref, entity_ids} only.
    points.push({ref: e.ref, cum});
    cum += 0; // placeholder — real trajectory below uses just labels.
  }
  d3.select("#p-trajectory").html("");
  const div = d3.select("#p-trajectory").append("div").attr("class", "traj-list");
  ev.forEach((e, i) => {
    div.append("div").html(`<code>#${i+1}</code> <b>${e.ref}</b> <span class="muted">${e.claim.slice(0,140)}</span>`);
  });
  const tbody = document.querySelector("#ledger-table tbody");
  tbody.innerHTML = ev.map(e => `
    <tr><td>${e.source}</td>
        <td><code>${e.ref}</code></td>
        <td>${e.claim || ""}</td></tr>`).join("");
}

function renderTimeline() {
  document.getElementById("timeline-body").textContent = JSON.stringify({
    latency_s:  CURRENT.latency_s,
    tool_calls: CURRENT.tool_calls,
    tokens:     CURRENT.tokens,
    stop_reason: CURRENT.stop_reason,
  }, null, 2);
}

function renderActions() {
  const nba = CURRENT.next_best_actions || {};
  const li = a => `<li><span class="badge-action ${a.route}">${a.action}</span> <span class="muted">(${a.route})</span> — ${a.reason}</li>`;
  document.getElementById("actions-initial").innerHTML = (nba.initial || []).map(li).join("");
  document.getElementById("actions-final").innerHTML = (nba.final || []).map(li).join("");
  document.getElementById("what-changed").textContent = "what changed: " + (nba.what_changed || "");
  // What-if handlers
  document.querySelectorAll(".whatif-row button").forEach(b =>
    b.addEventListener("click", () => runWhatIf(b.dataset.resp)));
}

async function runWhatIf(resp) {
  const el = document.getElementById("whatif-body");
  el.textContent = `Running with response=${resp}…`;
  const r = await fetch(`${API}/whatif/${CURRENT.case_id}`, {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({customer_response: resp}),
  });
  const j = await r.json();
  el.textContent = JSON.stringify(j, null, 2);
}

function renderSar() {
  const s = CURRENT.sar || {};
  const b = document.getElementById("sar-body");
  if (!s.file) { b.innerHTML = `<p class="muted">No SAR — <em>${s.reason || ""}</em></p>`; return; }
  b.innerHTML = `
    <p><b>File:</b> yes · <b>Total:</b> $${(s.total_amount_usd||0).toFixed(2)}</p>
    <p><b>Subjects:</b> ${(s.subjects||[]).join(", ")}</p>
    <p><b>Dates:</b> ${(s.activity_dates||[]).join(" → ")}</p>
    <p><b>Reason:</b> ${s.reason||""}</p>
    <pre>${s.narrative||""}</pre>`;
}

// ---- Graph (d3 force layout, live TG-enriched) ------------------------
async function renderGraph(state) {
  // Bump the token; only the newest call may touch the DOM.
  const myToken = ++GRAPH_RENDER_TOKEN;
  const container = document.getElementById("graph-container");
  const g = await (await fetch(`${API}/cases/${state.case_id}/graph`)).json();
  if (myToken !== GRAPH_RENDER_TOKEN) return;    // superseded — bail
  container.innerHTML = "";                       // clear right before draw
  if (!g.nodes || !g.nodes.length) { container.textContent = "no graph"; return; }

  const COLORS = {
    case:        "#f26666",   // red
    device:      "#f2c04d",   // yellow
    card:        "#6aa8ff",   // blue
    closed_case: "#b58bff",   // purple
  };

  // Legend + caption above the SVG.
  const header = document.createElement("div");
  header.className = "graph-header";
  header.innerHTML = `
    <div class="graph-caption">${g.caption || ""}</div>
    <div class="graph-legend">
      <span><i style="background:${COLORS.case}"></i> case</span>
      <span><i style="background:${COLORS.device}"></i> device</span>
      <span><i style="background:${COLORS.card}"></i> peer card</span>
      <span><i style="background:${COLORS.card};border:2px solid ${COLORS.case}"></i> peer card + confirmed-fraud CC</span>
      <span><i style="background:${COLORS.closed_case}"></i> confirmed-fraud ClosedCase</span>
    </div>`;
  container.appendChild(header);

  // Real container size, height ≈ 75vh.
  const W = container.clientWidth || 1200;
  const H = Math.max(420, Math.floor(window.innerHeight * 0.75));

  const svg = d3.select(container).append("svg")
    .attr("width", "100%")
    .attr("height", H)
    .attr("viewBox", `0 0 ${W} ${H}`)
    .attr("preserveAspectRatio", "xMidYMid meet");
  const zoomLayer = svg.append("g").attr("class", "zoom-layer");
  svg.call(d3.zoom()
    .scaleExtent([0.25, 4])
    .on("zoom", (ev) => zoomLayer.attr("transform", ev.transform)));

  const sim = d3.forceSimulation(g.nodes)
    .force("link",    d3.forceLink(g.edges).id(d => d.id)
                         .distance(d => d.label === "USED" ? 130 : 180))
    .force("charge",  d3.forceManyBody().strength(-800))
    .force("collide", d3.forceCollide().radius(28))
    .force("x",       d3.forceX(W / 2).strength(0.05))
    .force("y",       d3.forceY(H / 2).strength(0.05))
    .force("center",  d3.forceCenter(W / 2, H / 2));

  const edge = zoomLayer.append("g").selectAll("line").data(g.edges).enter().append("line")
    .attr("stroke", "#3a4051").attr("stroke-width", 1.2)
    .attr("opacity", d => d.label === "USED" ? 0.5 : 0.85);
  const node = zoomLayer.append("g").selectAll("g").data(g.nodes).enter().append("g");
  node.append("circle")
     .attr("r", d => d.type === "case" ? 18 : (d.type === "device" ? 14 : 9))
     .attr("fill", d => COLORS[d.type] || "#888")
     .attr("stroke", d => (d.type === "card" && d.has_fraud_cc) ? COLORS.case : "#0e1116")
     .attr("stroke-width", d => (d.type === "card" && d.has_fraud_cc) ? 3 : 1);
  node.append("text")
     .text(d => (d.type === "case" || d.type === "device" ||
                 (d.type === "card" && d.has_fraud_cc) ||
                 d.type === "closed_case") ? d.label : "")
     .attr("dx", d => d.type === "case" ? 24 : 14).attr("dy", 4)
     .style("font-size", "12px").style("fill", "#dfe4ee")
     .style("pointer-events", "none");
  node.append("title").text(d => d.label + (d.has_fraud_cc ? "  ⚠ confirmed-fraud CC on shared device" : ""));

  sim.on("tick", () => {
    edge.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("transform", d => `translate(${d.x},${d.y})`);
  });
}

// ---- Backtest ---------------------------------------------------------
async function loadBacktest() {
  const el = document.getElementById("backtest-body");
  el.textContent = "Loading…";
  const s = await (await fetch(`${API}/backtest`)).json();
  if (!s.available) { el.textContent = "no backtest yet"; return; }
  const rowH = r => `
    <tr><td>${r.case_id}</td><td>${r.gt_outcome}</td><td>${r.gt_pattern}</td>
        <td>${r.predicted_verdict}</td><td>${r.predicted_pattern}</td>
        <td>${(r.p_initial||0).toFixed(3)}</td></tr>`;
  const preview = (mode) => s[mode] ? `
    <h3>${mode} — ${s[mode].n} cases · <code>${s[mode].path}</code></h3>
    <table><thead><tr><th>case</th><th>gt outcome</th><th>gt pattern</th>
    <th>pred verdict</th><th>pred pattern</th><th>p_initial</th></tr></thead>
    <tbody>${s[mode].preview.map(rowH).join("")}</tbody></table>` : "";
  const rel = s.reliability_url ? `<h3>Reliability</h3><img src="${s.reliability_url}" alt="reliability">` : "";
  // Top metrics box: OOT (from oot_eval.json) + 5-fold CV (from alert_model.json).
  const m = s.metrics || {};
  let metricsBox = "";
  if (m.oot || m.cv) {
    const cells = [];
    if (m.oot && m.oot.auc != null) {
      cells.push(`<div class="metric">
        <div class="metric-value">${m.oot.auc.toFixed(4)}</div>
        <div class="metric-label">Out-of-time AUC</div>
        <div class="metric-sub">model trained Jul–Sep, tested on
          ${m.oot.n.toLocaleString()} later cases
          (${m.oot.n_fraud.toLocaleString()} fraud +
           ${m.oot.n_cleared.toLocaleString()} cleared)
          · Brier ${m.oot.brier != null ? m.oot.brier.toFixed(4) : "?"}
        </div>
      </div>`);
    }
    if (m.cv && m.cv.auc != null) {
      cells.push(`<div class="metric">
        <div class="metric-value">${m.cv.auc.toFixed(4)}</div>
        <div class="metric-label">5-fold CV AUC</div>
        <div class="metric-sub">n=${(m.cv.n_train||0).toLocaleString()}
          · Brier ${m.cv.brier != null ? m.cv.brier.toFixed(4) : "?"}
        </div>
      </div>`);
    }
    metricsBox = `<div class="metrics-box">${cells.join("")}</div>`;
  }
  el.innerHTML = `${metricsBox}${rel}${preview("oracle")}${preview("simulated")}
    <details><summary>Full BACKTEST_REPORT.md</summary>
    <div>${marked.parse(s.report || "")}</div></details>`;
}

async function loadMemory() {
  const el = document.getElementById("memory-body");
  const m = await (await fetch(`${API}/memory`)).json();
  if (!m.ok) { el.innerHTML = `<p>Graph unreachable: ${m.error}</p>`; return; }
  // Fetch extras count so we can render the "20 + N monitoring" split.
  let extras_n = 0;
  try {
    const s = await (await fetch(`${API}/extras`)).json();
    extras_n = (s && s.n) ? s.n : 0;
  } catch (_) {}
  const bench = 20;
  const total = m.sentinel_case_count;
  el.innerHTML = `<p>SentinelCase vertices: <b>${total}</b>
    (${bench} benchmark + ${extras_n} monitoring)</p>`;
}

async function loadMonitor() {
  const el = document.getElementById("monitor-body");
  el.textContent = "Loading…";
  const s = await (await fetch(`${API}/extras`)).json();
  if (!s.available) { el.innerHTML = "<p class='muted'>Run <code>python sentinel/monitor.py 15 0.92</code>.</p>"; return; }
  const row = r => `<tr>
    <td><b>${r.case_id}</b></td><td>${r.txn_id}</td>
    <td>${r.opened_at || "—"}</td>
    <td>${(r.risk_score||0).toFixed(2)}</td>
    <td><span class="badge ${r.verdict}">${r.verdict}</span></td>
    <td>${r.pattern}</td>
    <td>$${(r.exposure||0).toFixed(2)}</td>
    <td>${r.sar ? "✓" : "—"}</td>
    <td class="muted">${(r.actions||[]).join(", ")}</td>
    <td class="muted">${r.graph_case_id || "—"}</td>
  </tr>`;
  el.innerHTML = `<p><b>${s.n}</b> extra alerts. Same detectors, same
    alert model, same policy engine as the benchmark set. Verdicts land
    in <code>cases_extra/</code>.</p>
    <table><thead><tr>
      <th>case</th><th>txn</th><th>ts</th><th>risk_score</th>
      <th>verdict</th><th>pattern</th><th>exposure</th><th>sar</th>
      <th>actions</th><th>vertex</th>
    </tr></thead><tbody>${s.rows.map(row).join("")}</tbody></table>`;
}

async function loadFindings() {
  const el = document.getElementById("findings-body");
  el.textContent = "Loading…";
  try {
    const s = await (await fetch("/api/findings")).json();
    if (!s.available) throw new Error(s.error || "not run");
    el.innerHTML = marked.parse(s.markdown || "");
  } catch (e) {
    el.innerHTML = "<p class='muted'>Run <code>python scripts/undocumented_sweep.py</code> to generate docs/UNDOCUMENTED_FINDINGS.md.</p>";
  }
}

loadList();
