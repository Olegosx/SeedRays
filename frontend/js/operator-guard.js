// Защита страниц панели оператора: без живой сессии — на вход оператора.
// Подставляет логин в [data-username], вешает выход на [data-logout];
// сбой связи или сервера показывается баннером, а не разлогином.

import { api, setCsrf } from "./api.js";
import { errorText } from "./i18n.js";

try {
	const me = await api("GET", "/v1/operator/me");
	setCsrf(me.csrf);
	document.addEventListener("DOMContentLoaded", () => fill(me.operator));
	if (document.readyState !== "loading") {
		fill(me.operator);
	}
} catch (e) {
	if (e.code === "network" || String(e.code).startsWith("http_5")) {
		showOutage(e);
	} else {
		location.href = "operator-login.html";
	}
}

function fill(operator) {
	document.querySelectorAll("[data-username]").forEach((el) => {
		el.textContent = operator.login;
	});
	document.querySelectorAll("[data-logout]").forEach((el) => {
		el.addEventListener("click", async (event) => {
			event.preventDefault();
			try {
				await api("POST", "/v1/operator/logout");
			} catch (_e) {
				// Сессия могла уже истечь — всё равно уходим на вход.
			}
			location.href = "operator-login.html";
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
