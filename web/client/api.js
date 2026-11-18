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
        throw new Problem(reply.status, info.problem);
    }
    return {status: reply.status, headers: reply.headers, data};
}
