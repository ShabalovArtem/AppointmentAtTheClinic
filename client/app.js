// Клієнт C02 «Запис до клініки»: вкладки ЛР1–ЛР6 поверх маршрутів openapi.yaml.
import {API, call, FINAL, getToken, Problem, setToken} from "./api.js";

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const fmt = (v) => (v === null || v === undefined ? "—" : typeof v === "number" ? v.toLocaleString("uk-UA") : v);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shortId = () => crypto.randomUUID().slice(0, 8);

/** Текст помилки для людини: неіснуючий маршрут — це робота, яку ще не зроблено. */
function explain(error) {
    if (error instanceof Problem) {
        if (error.status === 404 && error.body?.title === "Not Found") {
            return "Маршрут ще не реалізовано (404) — з'явиться у відповідній ЛР.";
        }
        return `${error.status} ${error.body?.title ?? ""}${error.body?.detail ? ` — ${error.body.detail}` : ""}`;
    }
    return `Немає зв'язку з API (${API}): ${error.message}`;
}

function showJson(pre, value, isProblem = false) {
    pre.classList.toggle("problem", isProblem);
    pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function into(pre, method, path, body) {
    try {
        const reply = await call(method, path, {body});
        showJson(pre, reply.data);
        return reply;
    } catch (error) {
        showJson(pre, explain(error), true);
        return null;
    }
}

// ------------------------------------------------------------------ header: token, API status, tabs

const token = $("token");
token.value = getToken();
token.onchange = () => setToken(token.value.trim());

async function pingApi() {
    const pill = $("api-pill");
    const started = performance.now();
    try {
        const reply = await fetch(API + "/health");
        const ms = performance.now() - started;
        pill.className = `pill ${reply.ok ? "ok" : "bad"}`;
        pill.lastElementChild.textContent = `API: ${reply.status}`;
        $("m-health").textContent = ms.toFixed(1);
    } catch {
        pill.className = "pill bad";
        pill.lastElementChild.textContent = "API: недоступний";
    }
}

const tabs = [...document.querySelectorAll("nav.tabs button")];
function openTab(name) {
    for (const tab of tabs) tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
    for (const lab of document.querySelectorAll(".lab")) lab.classList.toggle("active", lab.id === name);
    try { localStorage.setItem("tab", name); } catch { /* приватне вікно */ }
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    if (name !== "lab1") return;
    refreshMetrics();
    refreshResource();
}
for (const tab of tabs) tab.onclick = () => openTab(tab.dataset.tab);

// ------------------------------------------------------------------ ЛР1: запис, черга, ресурс

const requests = new Map(); // request_id -> останній відомий стан
const MAX_ROWS = 300;
let metricsCache = null;

function log(text, kind = "") {
    const line = document.createElement("div");
    line.className = kind;
    line.textContent = `${new Date().toLocaleTimeString("uk-UA")}  ${text}`;
    $("l1-log").prepend(line);
    while ($("l1-log").childElementCount > 60) $("l1-log").lastElementChild.remove();
}

async function loadDays() {
    try {
        const {data} = await call("GET", "/v1/doctor-days");
        for (const select of [$("l1-day"), $("l3-day")]) {
            const keep = select.value;
            select.innerHTML = data.items
                .map((d) => `<option value="${esc(d.id)}">${esc(d.id)} · ${esc(d.full_name)}, ${esc(d.specialty)} · ${esc(d.day)}</option>`)
                .join("");
            select.value = keep || (data.items.some((d) => d.id === "dd-small") ? "dd-small" : data.items[0]?.id);
        }
    } catch (error) {
        log(explain(error), "bad");
    }
}

function badge(state) {
    return `<span class="badge ${esc(state)}">${esc(state)}</span>`;
}

function renderRequests() {
    const rows = [...requests.values()].slice(-MAX_ROWS).reverse();
    $("l1-requests").innerHTML = rows.length
        ? rows.map((r) => `<tr title="${esc(r.reason ?? "")}">
            <td class="mono">${esc(r.id)}<br><small>${esc(r.doctor_day_id)} · ${esc(r.patient_id)}</small></td>
            <td>${badge(r.state)}${r.reason ? `<br><small>${esc(r.reason)}</small>` : ""}</td>
            <td class="num">${fmt(r.timing?.queue_wait_ms)}</td>
            <td class="num">${fmt(r.timing?.processing_ms)}</td>
            <td class="num">${fmt(r.timing?.total_ms)}</td></tr>`).join("")
        : `<tr class="empty"><td colspan="5">Ще немає запитів</td></tr>`;
}

/** Подати один запит. Повертає HTTP-статус (0 — немає зв'язку). */
async function submit(body, {quiet = false} = {}) {
    try {
        const reply = await call("POST", "/v1/booking-requests", {body});
        requests.set(body.request_id, {id: body.request_id, ...body, state: reply.data.state});
        if (!quiet) log(`202 ${body.request_id} → у черзі`, "ok");
        return reply.status;
    } catch (error) {
        if (!quiet) log(`${body.request_id}: ${explain(error)}`, "bad");
        return error instanceof Problem ? error.status : 0;
    }
}

async function pollPending() {
    const pending = [...requests.values()].filter((r) => !FINAL.has(r.state)).slice(0, 40);
    await Promise.all(pending.map(async (r) => {
        try {
            const {data} = await call("GET", `/v1/booking-requests/${encodeURIComponent(r.id)}`);
            requests.set(r.id, {...data, quietFinal: r.quietFinal});
            if (FINAL.has(data.state) && !r.quietFinal) {
                log(`${data.id}: ${data.state}${data.reason ? ` (${data.reason})` : ""}`, data.state === "done" ? "ok" : "bad");
            }
        } catch { /* наступна спроба */ }
    }));
    if (pending.length) {
        renderRequests();
        refreshResource();
    }
}

$("l1-book").onclick = () => {
    const patient = $("l1-patient").value.trim() || "p-1";
    submit({
        request_id: `req-${shortId()}`,
        doctor_day_id: $("l1-day").value,
        patient_id: $("l1-invalid").checked ? `invalid-${patient}` : patient,
        slots: Number($("l1-slots").value) || 1,
    }).then(renderRequests);
};

async function series(dayId, count, label) {
    const statuses = await Promise.all(Array.from({length: count}, (_, i) =>
        submit({request_id: `${label}-${shortId()}`, doctor_day_id: dayId, patient_id: `p-${i}`, slots: 1}, {quiet: true})));
    const counts = statuses.reduce((acc, s) => ({...acc, [s]: (acc[s] ?? 0) + 1}), {});
    for (const r of requests.values()) if (r.id.startsWith(label)) r.quietFinal = true;
    log(`${label}: ${count} запитів → ${Object.entries(counts).map(([k, v]) => `${k}×${v}`).join(", ")}`, counts[429] ? "bad" : "ok");
    renderRequests();
}

$("l1-burst").onclick = async (event) => {
    event.target.disabled = true;
    await series($("l1-day").value, Math.min(500, Number($("l1-burst-n").value) || 80), "burst");
    event.target.disabled = false;
};

$("l1-race").onclick = async (event) => {
    const day = $("l1-race-day").value;
    event.target.disabled = true;
    $("l1-day").value = day;
    await refreshResource();
    await series(day, 40, "race");
    event.target.disabled = false;
};

$("l1-day").onchange = () => refreshResource();

async function refreshResource() {
    const id = $("l1-day").value;
    if (!id) return;
    try {
        const {data: d} = await call("GET", `/v1/doctor-day/${encodeURIComponent(id)}`);
        const share = Math.min(100, (100 * d.booked) / d.slots_total);
        const over = d.booked > d.slots_total;
        $("l1-res-title").textContent = d.id;
        $("l1-res").innerHTML = `
            <p class="hint" style="margin:0 0 10px">${esc(d.specialty)} · ${esc(d.day)}</p>
            <div class="bar ${over ? "over" : share >= 100 ? "full" : ""}"><div style="width:${share}%"></div>
                <span>${fmt(d.booked)} / ${fmt(d.slots_total)} слотів</span></div>
            <div class="stats" style="margin-top:10px">
                <div class="stat"><b>${fmt(d.slots_taken)}</b><small>slots_taken</small></div>
                <div class="stat"><b>${fmt(d.booked)}</b><small>фактично записано</small></div>
                <div class="stat"><b>${fmt(d.slots_free)}</b><small>вільно</small></div>
            </div>
            <p style="margin:12px 0 0">Інваріант: ${d.invariant_ok
                ? `<span class="badge ok">виконано</span>`
                : `<span class="badge bad">порушено</span> <small>${d.slots_taken !== d.booked ? "втрачені оновлення" : ""}${over ? " · записано понад наявні слоти" : ""}</small>`}</p>`;
    } catch (error) {
        $("l1-res").innerHTML = `<p class="hint">${esc(explain(error))}</p>`;
    }
}

const INTERVALS = [
    ["queue_wait_ms", "очікування в черзі"],
    ["processing_ms", "оброблення"],
    ["total_ms", "повне виконання"],
    ["delivery_wait_ms", "очікування видачі"],
];

async function refreshMetrics() {
    try {
        const reply = await fetch(API + "/metrics");
        const m = await reply.json();
        metricsCache = m;
        const cap = m.queue_capacity ?? 50;
        const len = m.queue_length ?? 0;
        const bar = $("l1-qbar");
        bar.classList.toggle("full", len >= cap);
        bar.firstElementChild.style.width = `${(100 * len) / cap}%`;
        bar.lastElementChild.textContent = len >= cap ? "черга повна" : "";
        $("l1-qtext").textContent = `${len} / ${cap}`;
        document.querySelectorAll(".cap").forEach((el) => (el.textContent = cap));

        const workers = m.workers ?? 0;
        $("l1-wcount").textContent = workers;
        $("l1-workers").innerHTML = Array.from({length: workers}, (_, i) =>
            `<div class="worker ${i < (m.processing ?? 0) ? "busy" : ""}" title="обробник ${i + 1}">${i + 1}</div>`).join("");

        $("m-accepted").textContent = fmt(m.accepted);
        $("m-rejected").textContent = fmt(m.rejected);
        $("m-processing").textContent = fmt(m.processing);
        $("m-done").textContent = fmt(m.outcomes?.done);
        $("m-domain").textContent = fmt(m.outcomes?.rejected);
        $("m-failed").textContent = fmt(m.outcomes?.failed);
        $("m-http").textContent = fmt(m.latency_ms?.p95);
        $("l1-mode").textContent = m.resource_mode ?? "?";

        $("l1-timing").innerHTML = INTERVALS.map(([key, name]) => {
            const t = m.timing_ms?.[key] ?? {};
            return `<tr><td>${name}</td><td class="num">${fmt(t.n)}</td><td class="num">${fmt(t.p50)}</td>
                    <td class="num">${fmt(t.p95)}</td><td class="num">${fmt(t.p99)}</td></tr>`;
        }).join("");
    } catch { /* статус API показує шапка */ }
}

// ------------------------------------------------------------------ ЛР2: розрахунок і гістограма

let computeId = null;

$("l2-run").onclick = async () => {
    const body = {
        a: Number($("l2-a").value), b: Number($("l2-b").value), n: Number($("l2-n").value),
        workers: Number($("l2-workers").value), method: $("l2-method").value, mode: $("l2-mode").value,
        ...($("l2-async").checked ? {async: true} : {}),
    };
    $("l2-summary").innerHTML = `<p class="hint">Обчислення…</p>`;
    let reply = await into($("l2-out"), "POST", "/v1/compute", body);
    if (!reply) return void ($("l2-summary").innerHTML = "");
    computeId = reply.data.id ?? null;
    $("l2-cancel").disabled = !computeId;
    while (computeId && reply.data.state === "running") {
        await sleep(500);
        reply = await into($("l2-out"), "GET", `/v1/compute/${encodeURIComponent(computeId)}`);
        if (!reply) break;
    }
    $("l2-cancel").disabled = true;
    const r = reply?.data?.result;
    $("l2-summary").innerHTML = r ? `<div class="stats" style="margin-bottom:12px">
        <div class="stat"><b>${Number(r.value).toPrecision(10)}</b><small>значення</small></div>
        <div class="stat"><b>${Number(r.reference).toPrecision(10)}</b><small>еталон</small></div>
        <div class="stat ${Math.abs(r.error) < 1e-6 ? "ok" : "bad"}"><b>${Number(r.error).toExponential(2)}</b><small>похибка</small></div>
        <div class="stat"><b>${fmt(r.wall_s)}</b><small>час, с</small></div>
        <div class="stat"><b>${fmt(r.workers)}</b><small>процесів</small></div></div>` : "";
};

$("l2-cancel").onclick = () => computeId && into($("l2-out"), "DELETE", `/v1/compute/${encodeURIComponent(computeId)}`);

$("l2-hist").onclick = async () => {
    const view = $("l2-hist-view");
    try {
        const {data} = await call("GET", "/v1/histogram");
        const h = data.histogram ?? data;
        const entries = Array.isArray(h) ? h.map((v, i) => [i, v]) : Object.entries(h);
        const max = Math.max(1, ...entries.map(([, v]) => Number(v)));
        view.innerHTML = entries.map(([k, v]) => `<div class="row" style="margin:2px 0">
            <span style="width:56px;font-size:12px">${esc(k)} хв</span>
            <div class="bar" style="flex:1;height:14px"><div style="width:${(100 * v) / max}%"></div></div>
            <span style="width:60px;text-align:right;font-size:12px">${fmt(Number(v))}</span></div>`).join("");
    } catch (error) {
        view.innerHTML = `<p class="hint">${esc(explain(error))}</p>`;
    }
};

// ------------------------------------------------------------------ ЛР3

$("l3-check").onclick = async () => {
    try {
        const {data: d} = await call("GET", `/v1/doctor-day/${encodeURIComponent($("l3-day").value)}`);
        $("l3-res").innerHTML = `<p>slots_taken <b>${d.slots_taken}</b>, записів <b>${d.bookings}</b>, інваріант
            ${d.invariant_ok ? `<span class="badge ok">виконано</span>` : `<span class="badge bad">порушено</span>`}</p>`;
    } catch (error) {
        $("l3-res").innerHTML = `<p class="hint">${esc(explain(error))}</p>`;
    }
};
$("l3-metrics").onclick = async () => {
    await refreshMetrics();
    const m = metricsCache ?? {};
    showJson($("l3-out"), {in_flight: m.in_flight, queue_length: m.queue_length, processing: m.processing, execution: m.execution ?? "local"});
};

// ------------------------------------------------------------------ ЛР4: звіт і еталон

const reportKey = (row) => `${row.doctor_id}|${row.weekday ?? row.day_of_week}`;

function renderReport(rows) {
    $("l4-rows").innerHTML = rows?.length
        ? rows.map((r) => `<tr><td>${esc(r.doctor_id)}</td><td>${esc(r.weekday ?? r.day_of_week)}</td>
            <td class="num">${fmt(r.visits)}</td><td class="num">${fmt(r.no_shows)}</td>
            <td class="num">${r.no_show_share === undefined ? "—" : (100 * r.no_show_share).toFixed(2) + " %"}</td></tr>`).join("")
        : `<tr class="empty"><td colspan="5">Немає рядків</td></tr>`;
}

$("l4-refresh").onclick = async () => {
    try {
        const {data} = await call("POST", "/v1/reports/main/refresh");
        $("l4-status").textContent = `Запущено: ${JSON.stringify(data)}`;
    } catch (error) {
        $("l4-status").textContent = explain(error);
    }
};
$("l4-get").onclick = async () => {
    try {
        const {data} = await call("GET", "/v1/reports/main");
        $("l4-status").textContent = `Стан: ${data.job?.state ?? data.state ?? "?"}`;
        renderReport(data.rows);
    } catch (error) {
        $("l4-status").textContent = explain(error);
    }
};
$("l4-compare").onclick = async () => {
    try {
        const [{data: report}, {data: reference}] = await Promise.all([
            call("GET", "/v1/reports/main"), call("GET", "/v1/reports/main/reference")]);
        const ref = new Map((reference.rows ?? []).map((r) => [reportKey(r), r]));
        const diffs = [];
        for (const row of report.rows ?? []) {
            const other = ref.get(reportKey(row));
            if (!other) diffs.push({key: reportKey(row), problem: "немає в еталоні"});
            else if (row.visits !== other.visits || row.no_shows !== other.no_shows
                || Math.abs(row.no_show_share - other.no_show_share) > 1e-9) diffs.push({key: reportKey(row), report: row, reference: other});
            ref.delete(reportKey(row));
        }
        for (const key of ref.keys()) diffs.push({key, problem: "немає у звіті"});
        showJson($("l4-out"), {groups_report: report.rows?.length ?? 0, groups_reference: reference.rows?.length ?? 0, match: diffs.length === 0, diffs: diffs.slice(0, 20)}, diffs.length > 0);
    } catch (error) {
        showJson($("l4-out"), explain(error), true);
    }
};

// ------------------------------------------------------------------ ЛР5: панель

const windowKey = (w) => `${w.doctor_id}|${w.window_start}`;
function renderWindows(windows) {
    $("l5-rows").innerHTML = windows?.length
        ? windows.map((w) => `<tr><td>${esc(w.doctor_id)}</td><td class="mono">${esc(w.window_start)} – ${esc(w.window_end ?? "")}</td>
            <td class="num">${fmt(w.late_count ?? w.count)}</td>
            <td>${(w.late_count ?? w.count) > 3 ? `<span class="badge bad">затримка прийому</span>` : ""}</td></tr>`).join("")
        : `<tr class="empty"><td colspan="4">Немає вікон</td></tr>`;
}
for (const [button, path] of [["l5-panel", "/v1/panel"], ["l5-batch", "/v1/panel/batch"]]) {
    $(button).onclick = async () => {
        const reply = await into($("l5-out"), "GET", path);
        if (reply) renderWindows(reply.data.windows);
    };
}
$("l5-compare").onclick = async () => {
    try {
        const [{data: stream}, {data: batch}] = await Promise.all([call("GET", "/v1/panel"), call("GET", "/v1/panel/batch")]);
        const b = new Map((batch.windows ?? []).map((w) => [windowKey(w), w]));
        const diffs = (stream.windows ?? []).filter((w) => (b.get(windowKey(w))?.late_count) !== w.late_count).map(windowKey);
        showJson($("l5-out"), {stream: stream.windows?.length ?? 0, batch: batch.windows?.length ?? 0, differ: diffs}, diffs.length > 0);
    } catch (error) {
        showJson($("l5-out"), explain(error), true);
    }
};

// ------------------------------------------------------------------ ЛР6: координатор

async function refreshCoordinator() {
    try {
        const {data} = await call("GET", "/v1/coordinator");
        $("l6-owner").textContent = data.owner ?? "—";
        $("l6-gen").textContent = data.generation ?? "—";
    } catch (error) {
        $("l6-owner").textContent = "—";
        showJson($("l6-out"), explain(error), true);
    }
}
$("l6-coord").onclick = refreshCoordinator;
$("l6-history").onclick = () => into($("l6-out"), "GET", "/v1/history");
$("l6-check").onclick = () => into($("l6-out"), "GET", "/v1/history/check");

// ------------------------------------------------------------------ старт і таймери

let savedTab = location.hash.slice(1) || "lab1";
if (!location.hash) {
    try { savedTab = localStorage.getItem("tab") ?? "lab1"; } catch { /* приватне вікно */ }
}
window.addEventListener("hashchange", () => document.getElementById(location.hash.slice(1)) && openTab(location.hash.slice(1)));
openTab(document.getElementById(savedTab) ? savedTab : "lab1");
pingApi();
loadDays().then(refreshResource);

setInterval(pingApi, 3000);
setInterval(() => $("lab1").classList.contains("active") && refreshMetrics(), 600);
setInterval(pollPending, 1000);
setInterval(() => $("lab6").classList.contains("active") && $("l6-auto").checked && refreshCoordinator(), 1000);
