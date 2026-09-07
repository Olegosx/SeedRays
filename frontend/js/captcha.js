// Невидимая проверка перебора (proof-of-work, виджет ALTCHA): страница держит
// один <altcha-widget display="invisible">, а solveCaptcha() решает свежую
// задачу и возвращает payload для поля "captcha" JSON-запроса. Решения
// одноразовые, поэтому перед каждым вызовом виджет сбрасывается.
import { ApiError } from "./api.js";

const SOLVE_TIMEOUT_MS = 30_000;

export function solveCaptcha(root) {
	const widget = (root || document).querySelector("altcha-widget");
	if (!widget || typeof widget.verify !== "function") {
		// Скрипт виджета не загрузился — честная ошибка вместо зависшей формы.
		return Promise.reject(new ApiError("captcha_failed", "captcha widget unavailable"));
	}
	return new Promise((resolve, reject) => {
		let done = false;
		const finish = (fn, value) => {
			if (!done) {
				done = true;
				clearTimeout(timer);
				widget.removeEventListener("verified", onVerified);
				widget.removeEventListener("statechange", onState);
				fn(value);
			}
		};
		const onVerified = (ev) => finish(resolve, ev.detail.payload);
		const onState = (ev) => {
			if (ev.detail.state === "error") {
				finish(reject, new ApiError("captcha_failed", "captcha verification failed"));
			}
		};
		const timer = setTimeout(
			() => finish(reject, new ApiError("captcha_failed", "captcha timed out")),
			SOLVE_TIMEOUT_MS,
		);
		widget.addEventListener("verified", onVerified);
		widget.addEventListener("statechange", onState);
		widget.reset();
		widget.verify();
	});
}
