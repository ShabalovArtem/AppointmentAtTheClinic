// Calls to the system through nginx: base address, current user, Idempotency-Key, Problem Details.
// Serve the client outside Compose:  python -m http.server 8081 --bind 127.0.0.1 --directory web/client
// Until lab 3 the user is the X-User-Id header; from lab 3 a token (setToken / login below).

export const API = (() => {
    try { return localStorage.getItem("api") ?? "http://localhost:8080"; } catch { return "http://localhost:8080"; }
})();

export class Problem extends Error {
    constructor(status, body) {
        super(body?.title ?? `HTTP ${status}`);
        this.status = status;
        this.body = body;
    }
}

let token = null;
let userId = null;
const observers = [];

export const setToken = (value) => { token = value; };
export const setUser = (value) => { userId = value; };
export const getUser = () => userId;
/** fn({method, path, status, instance, requestId, ms, problem}) after every call — for the request log. */
export const onCall = (fn) => observers.push(fn);
export const newKey = () => crypto.randomUUID();

export async function call(method, path, {body, idempotencyKey, headers = {}} = {}) {
    const started = performance.now();
    let reply;
    try {
        reply = await fetch(API + path, {
            method,
            headers: {
                ...(body !== undefined ? {"Content-Type": "application/json"} : {}),
                ...(token ? {Authorization: `Bearer ${token}`} : {}),
                ...(userId ? {"X-User-Id": userId} : {}),
                ...(idempotencyKey ? {"Idempotency-Key": idempotencyKey} : {}),
                ...headers,
            },
            body: body !== undefined ? JSON.stringify(body) : undefined,
        });
    } catch (error) {
        observers.forEach((fn) => fn({method, path, status: 0, ms: performance.now() - started, problem: {title: "немає зв'язку з nginx"}}));
        throw error;
    }

    const type = reply.headers.get("content-type") ?? "";
    const data = type.includes("json") ? await reply.json() : await reply.text();
    const info = {
        method, path, status: reply.status, ms: performance.now() - started,
        instance: reply.headers.get("x-instance"), requestId: reply.headers.get("x-request-id"),
        problem: reply.ok ? null : (typeof data === "object" ? data : {title: data}),
    };
    observers.forEach((fn) => fn(info));
    if (!reply.ok) {
        const problem = new Problem(reply.status, info.problem);
        problem.retryAfter = Number(reply.headers.get("retry-after")) || 0;
        throw problem;
    }
    return {status: reply.status, headers: reply.headers, data};
}

// О2.4: повторні спроби з тим самим ключем

/** Результат змінювального запиту встановити не вдалося: він міг виконатися або ні. */
export class UnknownOutcome extends Error {
    constructor(key, attempts, last) {
        super("Результат невідомий");
        this.key = key;
        this.attempts = attempts;
        this.last = last;
    }
}

/** Чи варто повторити: обрив/немає відповіді, перевантаження чи збій шлюзу, ліміт частоти,
 *  або той самий ключ ще обробляється. Відповіді за предметним правилом (404, 409, 422) — остаточні. */
const retryable = (error) =>
    !(error instanceof Problem)
    || [429, 502, 503, 504].includes(error.status)
    || (error.status === 409 && String(error.body?.type ?? "").endsWith("idempotency-key-in-progress"));

export const RETRY = {maxAttempts: 5, budgetMs: 15_000, baseMs: 300, capMs: 4_000};

/**
 * Змінювальний запит із повторами (О2.4). У всіх спробах — ТОЙ САМИЙ Idempotency-Key, тож сервер
 * виконає зміну щонайбільше один раз. Пауза — експоненційна з повним розсіюванням (full jitter):
 * random(0, min(cap, base·2^(n−1))), але не менша за Retry-After. Обмежено кількість спроб і загальний час.
 */
export async function callWithRetry(method, path, {body, idempotencyKey, onRetry, policy = RETRY} = {}) {
    const started = Date.now();
    for (let attempt = 1; ; attempt += 1) {
        try {
            const reply = await call(method, path, {body, idempotencyKey});
            return {...reply, attempts: attempt};
        } catch (error) {
            if (!retryable(error)) {
                error.attempts = attempt;
                throw error;
            }
            const backoff = Math.random() * Math.min(policy.capMs, policy.baseMs * 2 ** (attempt - 1));
            const delay = Math.max(backoff, (error.retryAfter ?? 0) * 1000);
            if (attempt >= policy.maxAttempts || Date.now() - started + delay > policy.budgetMs) {
                throw new UnknownOutcome(idempotencyKey, attempt, error);
            }
            onRetry?.({attempt, delay, status: error instanceof Problem ? error.status : 0});
            await new Promise((resolve) => setTimeout(resolve, delay));
        }
    }
}
