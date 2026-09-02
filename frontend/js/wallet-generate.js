// Логика шагов генерации кошелька в шлюзе (макет: данные подставные).
// Шаги: 1 — параметры; 2 — показ фразы; 3 — проверка трёх слов; 4 — готово.

// Подставная фраза макета. В боевой версии фраза приходит из API и
// существует только в памяти страницы до ухода с неё.
const MOCK_WORDS = [
	"ripple", "canyon", "vault", "ladder", "spice", "orbit",
	"meadow", "pilot", "copper", "lunar", "velvet", "anchor",
	"cabin", "sunset", "walnut", "ember", "piano", "stone",
	"garden", "mimic", "harvest", "ocean", "tiger", "blend",
];

document.addEventListener("alpine:init", () => {
	window.Alpine.data("genFlow", () => ({
		step: 1,
		words: null, // 12 | 24 — явный выбор, значения по умолчанию нет
		families: { tron: false, evm: false },
		usePassphrase: false,
		passphrase: "",
		phrase: [],
		checkIndexes: [],
		answers: ["", "", ""],
		mismatch: false,

		canGenerate() {
			return (this.words === 12 || this.words === 24)
				&& (this.families.tron || this.families.evm);
		},

		generate() {
			this.phrase = MOCK_WORDS.slice(0, this.words);
			const indexes = new Set();
			while (indexes.size < 3) {
				indexes.add(Math.floor(Math.random() * this.phrase.length));
			}
			this.checkIndexes = [...indexes].sort((a, b) => a - b);
			this.answers = ["", "", ""];
			this.step = 2;
		},

		confirmWritten() {
			this.step = 3;
		},

		check() {
			this.mismatch = this.checkIndexes.some(
				(wordIndex, i) =>
					this.answers[i].trim().toLowerCase() !== this.phrase[wordIndex],
			);
			if (!this.mismatch) {
				this.step = 4;
			}
		},

		createdWallets() {
			const wallets = [];
			if (this.families.tron) {
				wallets.push({ family: "TRON", xpub: "xpub6BtronMOCK…" });
			}
			if (this.families.evm) {
				wallets.push({ family: "EVM", xpub: "xpub6CevmMOCK…" });
			}
			return wallets;
		},
	}));
});
