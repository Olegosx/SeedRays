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

function sidebar(active) {
	const items = MENU.map(
		([key, href, icon, labelKey]) => `
					<li class="nav-item${key === active ? " active" : ""}">
						<a class="nav-link" href="${href}">
							<span class="nav-link-icon"><i class="ti ${icon}"></i></span>
							<span class="nav-link-title" x-text="$store.i18n.t('${labelKey}')"></span>
						</a>
					</li>`,
	).join("");
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

function topbar() {
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
						<a class="dropdown-item" href="settings.html"
							x-text="$store.i18n.t('menu.settings')"></a>
						<a class="dropdown-item" href="login.html" data-logout
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
	"lang-corner": () => langCorner(),
};

for (const el of document.querySelectorAll("[data-layout]")) {
	const render = TEMPLATES[el.dataset.layout];
	if (render) {
		el.outerHTML = render(el);
	}
}

// Общие помощники отображения (статусы операций, даты) — одна точка
// вместо копий в каждой странице.
document.addEventListener("alpine:init", () => {
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
