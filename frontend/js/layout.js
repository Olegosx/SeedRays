// Общий каркас кабинета: боковое меню, верхняя панель, угловой
// переключатель языка страниц входа. Одна точка правды для разметки,
// продублированной прежде на каждой странице.
//
// Плейсхолдеры: <div data-layout="sidebar" data-active="…"></div>,
// <div data-layout="topbar"></div>, <div data-layout="lang-corner"></div>.
// Модули исполняются до отложенного alpine.min.js, поэтому вставленная
// разметка обрабатывается Alpine как обычная.

import { t } from "./i18n.js";

// Разделы бокового меню: [ключ, страница, иконка, ключ словаря].
const MENU = [
	["dashboard", "dashboard.html", "ti-home", "menu.dashboard"],
	["wallets", "wallets.html", "ti-wallet", "menu.wallets"],
	["apps", "applications.html", "ti-plug-connected", "menu.apps"],
	["history", "history.html", "ti-history", "menu.history"],
	["settings", "settings.html", "ti-settings", "menu.settings"],
];

// Боковое меню панели оператора (см. docs, operator-panel).
const MENU_OPERATOR = [
	["users", "operator-users.html", "ti-users", "op.menuUsers"],
	["settings", "operator-settings.html", "ti-settings", "op.menuSettings"],
];

// Подменю приложений под пунктом «Приложения (API)»: обычный дропдаун
// Tabler со своим переключателем. Первый подпункт — общий список, дальше
// сами приложения (приходят асинхронно в $store.nav.apps, см. ниже).
// В разделе приложений подменю открыто сразу: иначе не видно, какое из
// них выбрано. Свернуть можно кликом, как у любого дропдауна.
const APPS_SUBMENU = (href, expanded) => `
						<div class="dropdown-menu${expanded ? " show" : ""}">
							<a class="dropdown-item" :class="{ active: $store.nav.allAppsActive }"
								href="${href}" x-text="$store.i18n.t('menu.allApps')"></a>
							<template x-for="app in $store.nav.apps" :key="app.id">
								<a class="dropdown-item"
									:class="{ active: app.id === $store.nav.activeAppId }"
									:href="'application.html?id=' + app.id" x-text="app.name"></a>
							</template>
						</div>`;

function sidebar(active, menu = MENU) {
	const items = menu.map(([key, href, icon, labelKey]) => {
		const label = `<span class="nav-link-icon"><i class="ti ${icon}"></i></span>
							<span class="nav-link-title" x-text="$store.i18n.t('${labelKey}')"></span>`;
		const activeClass = key === active ? " active" : "";
		if (key === "apps" && menu === MENU) {
			const expanded = key === active;
			return `
					<li class="nav-item dropdown${activeClass}">
						<a class="nav-link dropdown-toggle" href="#" data-bs-toggle="dropdown"
							role="button" aria-expanded="${expanded}">
							${label}
						</a>${APPS_SUBMENU(href, expanded)}
					</li>`;
		}
		return `
					<li class="nav-item${activeClass}">
						<a class="nav-link" href="${href}">
							${label}
						</a>
					</li>`;
	}).join("");
	return `<aside class="navbar navbar-vertical navbar-expand-lg" data-bs-theme="dark">
		<div class="container-fluid">
			<button class="navbar-toggler" type="button" data-bs-toggle="collapse"
				data-bs-target="#sidebar-menu" aria-expanded="false">
				<span class="navbar-toggler-icon"></span>
			</button>
			<div class="navbar-brand navbar-brand-autodark">
				<a href="dashboard.html" class="text-decoration-none fs-2 fw-bold">SeedRays</a>
			</div>
			<div class="collapse navbar-collapse" id="sidebar-menu">
				<ul class="navbar-nav pt-lg-3">${items}
				</ul>
			</div>
		</div>
	</aside>`;
}

function topbar(settingsHref = "settings.html", loginHref = "login.html") {
	return `<header class="navbar navbar-expand-md d-print-none" x-cloak x-data>
		<div class="container-xl justify-content-end">
			<div class="navbar-nav flex-row align-items-center">
				<div class="nav-item dropdown me-2">
					<a href="#" class="nav-link px-2" data-bs-toggle="dropdown">
						<i class="ti ti-world me-1"></i>
						<span x-text="$store.i18n.lang.toUpperCase()"></span>
					</a>
					<div class="dropdown-menu dropdown-menu-end">
						<a class="dropdown-item" :class="{ active: $store.i18n.lang === 'ru' }" href="#"
							@click.prevent="$store.i18n.set('ru')">Русский</a>
						<a class="dropdown-item" :class="{ active: $store.i18n.lang === 'en' }" href="#"
							@click.prevent="$store.i18n.set('en')">English</a>
					</div>
				</div>
				<div class="nav-item dropdown">
					<a href="#" class="nav-link d-flex lh-1 px-2" data-bs-toggle="dropdown">
						<span class="avatar avatar-sm"><i class="ti ti-user"></i></span>
						<span class="ps-2" data-username></span>
					</a>
					<div class="dropdown-menu dropdown-menu-end">
						<a class="dropdown-item" href="${settingsHref}"
							x-text="$store.i18n.t('menu.settings')"></a>
						<a class="dropdown-item" href="${loginHref}" data-logout
							x-text="$store.i18n.t('menu.logout')"></a>
					</div>
				</div>
			</div>
		</div>
	</header>`;
}

function langCorner() {
	return `<div class="position-absolute top-0 end-0 p-3">
		<template x-if="$store.i18n.lang !== 'ru'">
			<a href="#" class="link-secondary" @click.prevent="$store.i18n.set('ru')">Русский</a>
		</template>
		<template x-if="$store.i18n.lang === 'ru'">
			<span class="text-secondary">Русский</span>
		</template>
		<span class="text-secondary mx-1">·</span>
		<template x-if="$store.i18n.lang !== 'en'">
			<a href="#" class="link-secondary" @click.prevent="$store.i18n.set('en')">English</a>
		</template>
		<template x-if="$store.i18n.lang === 'en'">
			<span class="text-secondary">English</span>
		</template>
	</div>`;
}

const TEMPLATES = {
	sidebar: (el) => sidebar(el.dataset.active),
	topbar: () => topbar(),
	"op-sidebar": (el) => sidebar(el.dataset.active, MENU_OPERATOR),
	"op-topbar": () => topbar("operator-settings.html", "operator-login.html"),
	"lang-corner": () => langCorner(),
};

let cabinetSidebar = false;
for (const el of document.querySelectorAll("[data-layout]")) {
	const render = TEMPLATES[el.dataset.layout];
	if (render) {
		cabinetSidebar = cabinetSidebar || el.dataset.layout === "sidebar";
		el.outerHTML = render(el);
	}
}

// Наполнение подменю приложений. Список грузится параллельно со страницей;
// Alpine мог ещё не стартовать, поэтому данные кладутся в переменную,
// а store подхватывает их при инициализации (или сразу, если уже жив).
let apps = [];
if (cabinetSidebar) {
	import("./api.js")
		.then(({ api }) => api("GET", "/v1/user/applications"))
		.then((result) => {
			apps = result.applications;
			const nav = window.Alpine && window.Alpine.store("nav");
			if (nav) {
				nav.apps = apps;
			}
		})
		.catch(() => {
			// Меню — не место для баннеров: об ошибках (сеть, 401) честно
			// сообщают сама страница и auth-guard, подменю просто пустое.
		});
}

// Общие помощники отображения (статусы операций, даты) — одна точка
// вместо копий в каждой странице.
document.addEventListener("alpine:init", () => {
	window.Alpine.store("nav", {
		apps,
		// Подсветка подпункта: id приложения из адреса страницы приложения.
		activeAppId: location.pathname.endsWith("/application.html")
			? Number(new URLSearchParams(location.search).get("id"))
			: null,
		// Первый подпункт подменю — общий список приложений.
		allAppsActive: location.pathname.endsWith("/applications.html"),
	});
	window.Alpine.store("fmt", {
		statusClass(status) {
			return { confirmed: "bg-green-lt", pending: "bg-yellow-lt", failed: "bg-red-lt" }[status];
		},
		statusText(status) {
			return t("history.status" + status.charAt(0).toUpperCase() + status.slice(1));
		},
		date(iso) {
			return iso ? iso.slice(0, 10) : "";
		},
		dateTime(iso) {
			return iso ? iso.slice(0, 16).replace("T", " ") : "—";
		},
	});
});
