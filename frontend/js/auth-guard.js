// Защита страниц кабинета: без живой сессии — на страницу входа.
// Заодно подставляет имя пользователя в [data-username] и вешает выход
// на [data-logout]. Сбой связи или сервера — не повод разлогинивать:
// показываем сообщение и даём повторить.

import { api, setCsrf } from "./api.js";
import { errorText } from "./i18n.js";

try {
	const me = await api("GET", "/v1/user/me");
	setCsrf(me.csrf);
	document.addEventListener("DOMContentLoaded", () => fill(me.user));
	if (document.readyState !== "loading") {
		fill(me.user);
	}
} catch (e) {
	if (e.code === "network" || String(e.code).startsWith("http_5")) {
		showOutage(e);
	} else {
		// 401 и прочие ошибки авторизации: сессии нет — на вход.
		location.href = "login.html";
	}
}

function fill(user) {
	document.querySelectorAll("[data-username]").forEach((el) => {
		el.textContent = user.username;
	});
	document.querySelectorAll("[data-logout]").forEach((el) => {
		el.addEventListener("click", async (event) => {
			event.preventDefault();
			try {
				await api("POST", "/v1/user/logout");
			} catch (_e) {
				// Сессия могла уже истечь — всё равно уходим на вход.
			}
			location.href = "login.html";
		});
	});
}

function showOutage(error) {
	const render = () => {
		const banner = document.createElement("div");
		banner.className = "alert alert-danger m-3";
		banner.textContent = errorText(error);
		const retry = document.createElement("a");
		retry.href = "";
		retry.className = "ms-2";
		retry.textContent = "↻";
		banner.appendChild(retry);
		document.body.prepend(banner);
	};
	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", render);
	} else {
		render();
	}
}
