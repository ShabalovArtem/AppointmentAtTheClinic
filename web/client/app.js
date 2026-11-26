// Сайт клініки C02: вхід і реєстрація пацієнта, каталог лікарів, запис (головна операція),
// мої записи з курсором, зміна причини (If-Match), скасування, реєстратура (закриття дня).
// Відповіді читаються за відомими полями — невідомі додаткові поля нічого не ламають (О1.7).
import {API, call, callWithRetry, newKey, onCall, Problem, setToken, UnknownOutcome} from "./api.js";

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);

/** Гроші — цілі копійки; форматуємо без дробових обчислень. */
function money(price) {
    if (!price || !Number.isInteger(price.amount_minor)) return "—";
    const {amount_minor: m, currency} = price;
    const whole = Math.trunc(m / 100).toLocaleString("uk-UA");
    const cents = String(Math.abs(m % 100)).padStart(2, "0");
    return `${whole},${cents} ${currency === "UAH" ? "грн" : esc(currency)}`;
}

/** Календарна дата прийому (date) — без перетворення зон. */
function dayText(iso) {
    const [y, m, d] = String(iso).split("-").map(Number);
    if (!y) return esc(iso);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("uk-UA", {timeZone: "UTC", weekday: "short", day: "numeric", month: "long"});
}

/** Момент (timestamptz) — показуємо в зоні клініки. */
function momentText(iso, tz) {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleString("uk-UA", {timeZone: tz || "Europe/Kyiv", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
    } catch {
        return esc(iso);
    }
}

function problemHtml(error) {
    if (!(error instanceof Problem)) return `<div class="problem-box">Немає зв'язку з ${esc(API)}: ${esc(error.message)}</div>`;
    const p = error.body ?? {};
    const errors = Array.isArray(p.errors) ? `<ul>${p.errors.map((e) => `<li><code>${esc(e.field)}</code>: ${esc(e.message)}</li>`).join("")}</ul>` : "";
    return `<div class="problem-box"><b>${esc(p.status ?? error.status)} · ${esc(p.title ?? "Помилка")}</b>
        ${p.detail ? `<br>${esc(p.detail)}` : ""}${errors}<br><code>${esc(p.type ?? "")}</code></div>`;
}
const okHtml = (html) => `<div class="ok-box">${html}</div>`;

const STATUS_UA = {held: "утримано", confirmed: "підтверджено", expired: "строк сплив", cancelled: "скасовано", visited: "відвідано", no_show: "неявка"};
const CANCEL_UA = {patient: "пацієнтом", "day-closed": "день закрито реєстратурою"};
const statusBadge = (s) => `<span class="badge ${s === "held" ? "processing" : s === "confirmed" || s === "visited" ? "done" : "rejected"}">${esc(STATUS_UA[s] ?? s)}</span>`;

// журнал і схема

$("api-addr").textContent = API;
let flashTimer;
onCall(({method, path, status, instance, requestId}) => {
    const line = document.createElement("div");
    line.title = requestId ? `X-Request-ID: ${requestId}` : "";
    line.innerHTML = `<span>${new Date().toLocaleTimeString("uk-UA")}</span><span>${esc(method)}</span>
        <span>${esc(path)}</span><span class="s${String(status)[0]}">${status || "—"}</span><span>${esc(instance ?? "")}</span>`;
    const log = $("reqlog");
    if (log.firstElementChild?.classList.contains("muted")) log.innerHTML = "";
    log.prepend(line);
    while (log.childElementCount > 80) log.lastElementChild.remove();

    if (instance) $("instance-pill").textContent = instance;
    clearTimeout(flashTimer);
    const clear = () => {
        document.querySelectorAll(".diagram .hit").forEach((n) => n.classList.remove("hit"));
        document.querySelectorAll(".diagram .flow").forEach((n) => n.classList.remove("flow"));
    };
    clear();
    const nodes = ["n-browser", "n-nginx"];
    const lines = ["f1"];
    if (instance === "app-a" || instance === "app-b") {
        nodes.push(`n-${instance}`, "n-db");
        lines.push(instance === "app-a" ? "fa" : "fb", instance === "app-a" ? "fa2" : "fb2");
    }
    nodes.forEach((id) => $(id)?.classList.add("hit"));
    lines.forEach((id) => $(id)?.classList.add("flow"));
    flashTimer = setTimeout(clear, 1600);
});

async function pingApi() {
    const pill = $("api-pill");
    try {
        const reply = await fetch(API + "/health");
        pill.className = `pill ${reply.ok ? "ok" : "bad"}`;
        pill.lastElementChild.textContent = `API: ${reply.status}`;
    } catch {
        pill.className = "pill bad";
        pill.lastElementChild.textContent = "API: недоступний";
    }
}

// вхід, реєстрація, вихід

let me = null; // {id, login, name, role}

const store = {
    get: () => { try { return sessionStorage.getItem("token"); } catch { return null; } },
    set: (v) => { try { v ? sessionStorage.setItem("token", v) : sessionStorage.removeItem("token"); } catch { /* приватне вікно */ } },
};

function showAuth(view) {
    $("login-view").hidden = view !== "login";
    $("register-view").hidden = view !== "register";
    $("me-view").hidden = view !== "me";
}

function applyUser(user) {
    me = user;
    if (user) {
        $("me-name").textContent = user.name;
        $("me-info").textContent = `${user.role === "staff" ? "реєстратор" : "пацієнт"} · ${user.login} · ${user.id}`;
        showAuth("me");
    } else {
        showAuth("login");
    }
    // вкладки за роллю
    for (const button of subtabs) {
        const role = button.dataset.role;
        button.hidden = Boolean(role) && (!user || user.role !== role);
    }
    const active = subtabs.find((b) => b.getAttribute("aria-selected") === "true");
    if (!active || active.hidden) openPane("catalog");
    else if (active.dataset.pane === "mine") loadAllBookings();
    else if (active.dataset.pane === "registry") loadRegistry();
}

async function startSession(data) {
    setToken(data.access_token);
    store.set(data.access_token);
    applyUser(data.user);
}

$("login-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    $("auth-result").innerHTML = "";
    try {
        const {data} = await call("POST", "/v1/auth/login", {body: {login: form.get("login").trim(), password: form.get("password")}});
        event.target.reset();
        await startSession(data);
    } catch (error) {
        $("auth-result").innerHTML = problemHtml(error);
    }
};

$("register-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    $("auth-result").innerHTML = "";
    if (form.get("password") !== form.get("password2")) {
        $("auth-result").innerHTML = `<div class="problem-box">Паролі не збігаються</div>`;
        return;
    }
    try {
        const {data} = await call("POST", "/v1/auth/register", {
            body: {name: form.get("name").trim(), login: form.get("login").trim(), password: form.get("password")},
        });
        event.target.reset();
        await startSession(data);
        $("auth-result").innerHTML = okHtml(`<b>201 · Обліковий запис створено.</b> Вітаємо, ${esc(data.user.name)}! Тепер можна записатися до лікаря.`);
    } catch (error) {
        $("auth-result").innerHTML = problemHtml(error);
    }
};

$("show-register").onclick = () => { $("auth-result").innerHTML = ""; showAuth("register"); };
$("show-login").onclick = () => { $("auth-result").innerHTML = ""; showAuth("login"); };

$("logout").onclick = async () => {
    try { await call("POST", "/v1/auth/logout"); } catch { /* токен уже недійсний — однаково виходимо */ }
    setToken(null);
    store.set(null);
    $("auth-result").innerHTML = "";
    applyUser(null);
};

async function restoreSession() {
    const token = store.get();
    if (!token) return applyUser(null);
    setToken(token);
    try {
        const {data} = await call("GET", "/v1/me");
        applyUser(data);
    } catch {
        setToken(null);
        store.set(null);
        applyUser(null);
    }
}

// вкладки

const subtabs = [...document.querySelectorAll(".subtabs button")];
function openPane(name) {
    subtabs.forEach((b) => b.setAttribute("aria-selected", String(b.dataset.pane === name)));
    document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === name));
    if (name === "mine") loadAllBookings();
    if (name === "registry") loadRegistry();
}
subtabs.forEach((b) => (b.onclick = () => openPane(b.dataset.pane)));

// каталог і запис

let currentDoctor = null;

async function loadDoctors() {
    try {
        const {data} = await call("GET", "/v1/doctors");
        $("doctors").innerHTML = data.items.map((d) => `<button class="doctor" data-id="${esc(d.id)}" aria-pressed="false">
            <b>${esc(d.full_name)}</b><small>${esc(d.specialty)}</small></button>`).join("");
        for (const button of $("doctors").querySelectorAll(".doctor")) {
            button.onclick = () => {
                $("doctors").querySelectorAll(".doctor").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
                currentDoctor = button.dataset.id;
                loadDays();
            };
        }
    } catch (error) {
        $("doctors").innerHTML = problemHtml(error);
    }
}

async function loadDays() {
    if (!currentDoctor) return;
    try {
        const {data} = await call("GET", `/v1/doctors/${encodeURIComponent(currentDoctor)}/days`);
        $("days-title").textContent = `Дні прийому · ${data.doctor?.full_name ?? ""}`;
        $("days").innerHTML = data.items.length ? data.items.map((d) => {
            const share = d.slots_total ? (100 * (d.slots_total - d.slots_free)) / d.slots_total : 0;
            return `<tr>
                <td>${dayText(d.day)}<br><small class="muted">${esc(d.clinic_tz)} · слот ${esc(d.slot_minutes ?? 15)} хв</small></td>
                <td><div class="bar ${d.slots_free < 1 ? "full" : ""}" style="height:18px"><div style="width:${share}%"></div>
                    <span>${d.slots_free.toLocaleString("uk-UA")} / ${d.slots_total.toLocaleString("uk-UA")}</span></div></td>
                <td class="num">${money(d.price)}</td>
                <td><button class="btn primary" data-day="${esc(d.id)}" data-slots="1" ${d.slots_free < 1 ? "disabled" : ""}>1 слот</button>
                    <button class="btn" data-day="${esc(d.id)}" data-slots="2" ${d.slots_free < 2 ? "disabled" : ""}>2 слоти</button></td></tr>`;
        }).join("") : `<tr class="empty"><td colspan="4">Немає відкритих днів</td></tr>`;
        for (const button of $("days").querySelectorAll("button[data-day]")) {
            button.onclick = () => book(button.dataset.day, Number(button.dataset.slots));
        }
    } catch (error) {
        $("days").innerHTML = `<tr><td colspan="4">${problemHtml(error)}</td></tr>`;
    }
}

/** Нова дія користувача — новий ключ; «Перевірити ще раз» після невідомого результату — той самий ключ і тіло. */
async function book(doctorDayId, slots, retryOf = null) {
    if (!me) {
        $("book-result").innerHTML = `<div class="problem-box">Щоб записатися, увійдіть або зареєструйтеся (картка «Вхід» угорі).</div>`;
        return;
    }
    const reason = $("reason").value.trim();
    const body = retryOf?.body ?? {doctor_day_id: doctorDayId, slots, ...(reason ? {visit_reason: reason} : {})};
    const key = retryOf?.key ?? newKey();
    const attemptsLog = [];
    $("book-result").innerHTML = `<p class="hint">Записуємо… (Idempotency-Key <code>${esc(key.slice(0, 8))}…</code>)</p>`;
    try {
        const {data, headers, attempts} = await callWithRetry("POST", "/v1/bookings", {
            body, idempotencyKey: key,
            onRetry: ({attempt, delay, status}) => {
                attemptsLog.push(`спроба ${attempt}: ${status ? `HTTP ${status}` : "немає відповіді"}, повтор через ${(delay / 1000).toFixed(1)} с`);
                $("book-result").innerHTML = `<p class="hint">${attemptsLog.map(esc).join("<br>")}<br>Повторюємо з тим самим ключем…</p>`;
            },
        });
        const replayed = headers.get("idempotent-replayed") === "true";
        $("book-result").innerHTML = okHtml(`<b>201 · Запис створено</b> — ${statusBadge(data.status)}
            ${esc(data.doctor?.full_name ?? "")}, ${dayText(data.day)}, ${data.slots} сл., ${money(data.price)}.
            Підтвердити до ${momentText(data.expires_at, data.clinic_tz)}.<br><code>${esc(headers.get("location") ?? "")}</code>
            ${attempts > 1 || replayed ? `<br><small>Спроба ${attempts}${replayed ? "; сервер повернув збережений результат — запис створено один раз" : ""}.
            ${attemptsLog.map(esc).join("; ")}</small>` : ""}`);
        loadDays();
    } catch (error) {
        if (error instanceof UnknownOutcome) {
            $("book-result").innerHTML = `<div class="problem-box"><b>Результат невідомий.</b> Після ${error.attempts} спроб
                відповіді немає — запис міг створитися або ні. Повторна перевірка з тим самим ключем не створить другого запису.
                <br><small>${attemptsLog.map(esc).join("<br>")}</small>
                <br><button class="btn" id="retry-same-key">Перевірити ще раз (той самий ключ)</button></div>`;
            $("retry-same-key").onclick = () => book(body.doctor_day_id, body.slots, {key, body});
            return;
        }
        $("book-result").innerHTML = problemHtml(error);
        if (error.status === 409 || error.status === 404) loadDays();
    }
}

// мої записи: усі сторінки курсора, дії

const bookingsById = new Map();

async function loadAllBookings() {
    if (!me) return;
    const limit = Number($("page-size").value);
    const rows = [];
    let cursor = null;
    let pages = 0;
    $("pages-info").textContent = "завантаження…";
    try {
        do {
            const query = `?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
            const {data} = await call("GET", `/v1/bookings${query}`);
            rows.push(...(data.items ?? []));
            cursor = data.next_cursor ?? null;
            pages += 1;
        } while (cursor && pages < 200);
        bookingsById.clear();
        rows.forEach((b) => bookingsById.set(b.id, b));
        $("pages-info").textContent = `${rows.length} записів, ${pages} стор.`;
        $("bookings").innerHTML = rows.length ? rows.map((b) => {
            const active = b.status === "held" || b.status === "confirmed";
            return `<tr>
            <td class="mono">${esc(b.id)}<br><small class="muted">${b.channel === "call-center" ? "кол-центр" : "сайт"}${b.visit_reason ? ` · ${esc(b.visit_reason)}` : ""}</small></td>
            <td>${esc(b.doctor?.full_name ?? b.doctor_day_id)}<br><small class="muted">${esc(b.doctor?.specialty ?? "")} · ${dayText(b.day)}</small></td>
            <td class="num">${esc(b.slots)}</td><td class="num">${money(b.price)}</td>
            <td>${statusBadge(b.status)}${b.cancel_reason ? `<br><small class="muted">${esc(CANCEL_UA[b.cancel_reason] ?? b.cancel_reason)}</small>` : ""}</td>
            <td>${b.status === "held" ? momentText(b.expires_at, b.clinic_tz) : "—"}</td>
            <td>${active ? `<button class="btn" data-reason="${esc(b.id)}">Причина</button><br>
                <button class="btn" data-cancel="${esc(b.id)}">Скасувати</button>` : ""}</td></tr>`;
        }).join("") : `<tr class="empty"><td colspan="7">Записів немає</td></tr>`;
        $("bookings").querySelectorAll("[data-reason]").forEach((b) => (b.onclick = () => changeReason(b.dataset.reason)));
        $("bookings").querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = () => cancelBooking(b.dataset.cancel)));
    } catch (error) {
        $("pages-info").textContent = "";
        $("bookings").innerHTML = `<tr><td colspan="7">${problemHtml(error)}</td></tr>`;
    }
}
$("reload").onclick = loadAllBookings;
$("page-size").onchange = loadAllBookings;

async function changeReason(id) {
    const booking = bookingsById.get(id);
    const reason = prompt("Причина візиту", booking?.visit_reason ?? "");
    if (reason === null) return;
    try {
        // If-Match — версія, яку клієнт бачив; якщо хтось змінив запис раніше — 412
        await call("PATCH", `/v1/bookings/${encodeURIComponent(id)}`, {
            body: {visit_reason: reason.trim() || null},
            headers: {"If-Match": `"${booking?.version ?? ""}"`},
        });
        $("mine-result").innerHTML = okHtml("<b>200 · Причину змінено</b>");
    } catch (error) {
        $("mine-result").innerHTML = problemHtml(error);
    }
    loadAllBookings();
}

async function cancelBooking(id) {
    if (!confirm("Скасувати цей запис? Слоти повернуться в розклад.")) return;
    try {
        await call("POST", `/v1/bookings/${encodeURIComponent(id)}/cancel`);
        $("mine-result").innerHTML = okHtml("<b>200 · Запис скасовано</b>");
    } catch (error) {
        $("mine-result").innerHTML = problemHtml(error);
    }
    loadAllBookings();
}

// реєстратура

async function loadRegistry() {
    if (me?.role !== "staff") return;
    try {
        const {data} = await call("GET", "/v1/doctor-days");
        $("registry-days").innerHTML = data.items.map((d) => `<tr>
            <td>${esc(d.doctor?.full_name ?? "")}<br><small class="muted">${esc(d.doctor?.specialty ?? "")} · ${esc(d.id)}</small></td>
            <td>${dayText(d.day)}</td>
            <td>${d.slots_free.toLocaleString("uk-UA")} / ${d.slots_total.toLocaleString("uk-UA")}</td>
            <td>${d.closed ? `<span class="badge bad">закрито</span>` : `<span class="badge ok">відкрито</span>`}</td>
            <td>${d.closed ? "" : `<button class="btn" data-close="${esc(d.id)}">Закрити день</button>`}</td></tr>`).join("");
        $("registry-days").querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => closeDay(b.dataset.close)));
    } catch (error) {
        $("registry-days").innerHTML = `<tr><td colspan="5">${problemHtml(error)}</td></tr>`;
    }
}

async function closeDay(id) {
    if (!confirm(`Закрити день ${id}? Активні записи буде скасовано.`)) return;
    try {
        const {data} = await call("POST", `/v1/doctor-days/${encodeURIComponent(id)}/close`);
        $("registry-result").innerHTML = okHtml(`<b>200 · День закрито.</b> Скасовано записів: ${data.cancelled_bookings?.length ?? 0}.`);
    } catch (error) {
        $("registry-result").innerHTML = problemHtml(error);
    }
    loadRegistry();
}

// старт

pingApi();
setInterval(pingApi, 5000);
loadDoctors();
restoreSession();
