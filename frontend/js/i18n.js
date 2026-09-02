// Локализация кабинета: одна вёрстка + словари (ES-модули).
// Язык: сохранённый выбор → язык браузера → английский. Выбор живёт в localStorage.
import { ru } from "../i18n/ru.js";
import { en } from "../i18n/en.js";

const DICTS = { ru, en };
const STORAGE_KEY = "seedrays.lang";
const FALLBACK = "en";

function detectLang() {
	let stored = null;
	try {
		stored = localStorage.getItem(STORAGE_KEY);
	} catch (_e) {
		// Хранилище недоступно (приватный режим и т.п.) — просто не запоминаем выбор.
	}
	if (stored && DICTS[stored]) {
		return stored;
	}
	const nav = (navigator.language || "").toLowerCase();
	return nav.startsWith("ru") ? "ru" : FALLBACK;
}

export const lang = detectLang();

export function t(key) {
	return DICTS[lang][key] ?? DICTS[FALLBACK][key] ?? key;
}

export function setLang(code) {
	if (!DICTS[code] || code === lang) {
		return;
	}
	try {
		localStorage.setItem(STORAGE_KEY, code);
	} catch (_e) {
		// Не сохранилось — язык сменится на одну загрузку, без запоминания.
	}
	location.reload();
}

// Атрибут lang и заголовок вкладки — сразу, не дожидаясь Alpine.
document.documentElement.lang = lang;
const titleKey = document.documentElement.dataset.titleKey;
if (titleKey) {
	document.title = t(titleKey);
}

// Модульные скрипты исполняются до отложенного (defer) alpine.min.js,
// поэтому подписка на alpine:init успевает всегда.
document.addEventListener("alpine:init", () => {
	window.Alpine.store("i18n", { lang, t, set: setLang });
});
