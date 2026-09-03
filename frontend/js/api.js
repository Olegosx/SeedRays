// Обёртка над fetch для API кабинета: JSON, единый формат ошибок, CSRF.
// Сессия ездит в куке HttpOnly; на изменяющие запросы добавляется
// заголовок X-CSRF-Token, полученный при входе или из /v1/user/me.

let csrfToken = null;

export function setCsrf(token) {
	csrfToken = token;
}

export class ApiError extends Error {
	constructor(code, message) {
		super(message);
		this.code = code;
	}
}

export async function api(method, path, body) {
	const headers = {};
	if (body !== undefined) {
		headers["Content-Type"] = "application/json";
	}
	if (csrfToken && method !== "GET") {
		headers["X-CSRF-Token"] = csrfToken;
	}
	let response;
	try {
		response = await fetch(path, {
			method,
			headers,
			body: body !== undefined ? JSON.stringify(body) : undefined,
		});
	} catch (_e) {
		throw new ApiError("network", "network error");
	}
	let data = null;
	try {
		data = await response.json();
	} catch (_e) {
		// Ответ без тела (или не JSON) — оставляем null.
	}
	if (!response.ok) {
		const error = (data && data.error) || {};
		throw new ApiError(error.code || `http_${response.status}`, error.message || response.statusText);
	}
	return data;
}
