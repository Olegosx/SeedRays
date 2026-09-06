// Шаги генерации кошелька в шлюзе (ADR-0002).
// Фраза приходит из API один раз и живёт только в памяти страницы;
// после проверки трёх слов кошельки прикрепляются обычным путём
// «семейство + xpub» — сид повторно по сети не передаётся.

import { errorText } from "./i18n.js";

document.addEventListener("alpine:init", () => {
	window.Alpine.data("genFlow", () => ({
		step: 1,
		words: null, // 12 | 24 — явный выбор, значения по умолчанию нет
		familyOptions: [], // список семейств отдаёт бэкенд
		families: {},
		usePassphrase: false,
		passphrase: "",
		phrase: [],
		material: [], // [{family, xpub}] — из ответа генерации
		created: [],
		checkIndexes: [],
		answers: ["", "", ""],
		mismatch: false,
		error: "",

		async init() {
			try {
				const { api } = await import("./api.js");
				this.familyOptions = (await api("GET", "/v1/user/networks")).families;
			} catch (e) {
				this.error = this._message(e);
			}
		},

		canGenerate() {
			return (this.words === 12 || this.words === 24)
				&& this.selectedFamilies().length > 0;
		},

		selectedFamilies() {
			return Object.keys(this.families).filter((f) => this.families[f]);
		},

		async generate() {
			this.error = "";
			try {
				const { api } = await import("/js/api.js");
				const data = await api("POST", "/v1/user/wallets/generate", {
					words: this.words,
					families: this.selectedFamilies(),
					passphrase: this.usePassphrase ? this.passphrase : "",
				});
				this.phrase = data.phrase;
				this.material = data.wallets;
				const indexes = new Set();
				while (indexes.size < 3) {
					indexes.add(Math.floor(Math.random() * this.phrase.length));
				}
				this.checkIndexes = [...indexes].sort((a, b) => a - b);
				this.answers = ["", "", ""];
				this.step = 2;
			} catch (e) {
				this.error = this._message(e);
			}
		},

		confirmWritten() {
			this.step = 3;
		},

		async check() {
			this.mismatch = this.checkIndexes.some(
				(wordIndex, i) =>
					this.answers[i].trim().toLowerCase() !== this.phrase[wordIndex],
			);
			if (this.mismatch) {
				return;
			}
			this.error = "";
			try {
				const { api } = await import("/js/api.js");
				// Созданное сразу вычёркивается: повтор после частичного сбоя
				// досоздаёт только недостающие семейства, без дублей.
				for (const wallet of [...this.material]) {
					const result = await api("POST", "/v1/user/wallets", {
						family: wallet.family,
						xpub: wallet.xpub,
						label: "",
					});
					this.created.push(result.wallet);
					this.material = this.material.filter((w) => w.family !== wallet.family);
				}
				// Фраза больше не нужна — убираем её из состояния страницы.
				this.phrase = [];
				this.step = 4;
			} catch (e) {
				this.error = this._message(e);
			}
		},

		createdWallets() {
			return this.created.map((wallet) => ({
				family: wallet.family.toUpperCase(),
				xpub: wallet.xpub.slice(0, 12) + "…",
			}));
		},

		_message(e) {
			return errorText(e);
		},
	}));
});
