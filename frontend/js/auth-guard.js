// Защита страниц кабинета: без живой сессии — на страницу входа.
// Заодно подставляет имя пользователя в [data-username] и вешает выход
// на [data-logout].

import { api, setCsrf } from "./api.js";

try {
	const me = await api("GET", "/v1/user/me");
	setCsrf(me.csrf);
	window.seedraysUser = me.user;
	document.addEventListener("DOMContentLoaded", () => fill(me.user));
	if (document.readyState !== "loading") {
		fill(me.user);
	}
} catch (_e) {
	location.href = "login.html";
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
