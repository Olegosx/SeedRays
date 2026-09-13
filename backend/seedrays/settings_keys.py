"""Registry settings: the key names and how a switch is read.

Settings live in the registry as name/value pairs (ADR-0016): the operator
panel writes them, other parts of the gateway read them. While each side
spelled the name itself, they could drift apart without a word: the panel
would report "saved", the reader would find nothing under the new name and
fall back to its default. For the provider key that means requests without a
key and running into the provider's rate limit — that is, deposits noticed
late — with the panel showing a perfectly saved setting all along.

Defining the names here makes the drift impossible rather than unlikely. The
module deliberately depends on nothing: the chains layer, the watcher, the
orchestrator and the API all import it, and none of them gains a dependency
on another.
"""

# Провайдер данных цепочки. Ключ и темп запросов — общий ресурс шлюза, а не
# собственность watcher: биллинг обращается к тому же провайдеру.
PROVIDER_API_KEY = "provider.trongrid.api_key"
PROVIDER_RATE_PER_SEC = "provider.trongrid.rate_per_sec"

# Watcher: частота проходов, перекрытие при повторном чтении, момент, с
# которого начинается первое сканирование сети.
WATCHER_INTERVAL = "watcher.interval_seconds"
WATCHER_OVERLAP = "watcher.overlap_minutes"
WATCHER_SCAN_START = "watcher.scan_start"

# Почта и внешний адрес шлюза (ссылки в письмах без него не собрать).
MAIL_API_KEY = "mail.resend.api_key"
MAIL_FROM = "mail.from"
MAIL_DEV_AUTOCONFIRM = "mail.dev_autoconfirm"
GATEWAY_BASE_URL = "gateway.base_url"
GATEWAY_TRUSTED_PROXIES = "gateway.trusted_proxies"

# Журнал безопасности: ротация по размеру и число хранимых архивов.
SECLOG_ROTATE_MB = "seclog.rotate_mb"
SECLOG_BACKUPS = "seclog.backups"

# Вознаграждение владельца шлюза (ADR-0027).
BILLING_ENABLED = "billing.enabled"
BILLING_RATE_PERCENT = "billing.rate_percent"
BILLING_THRESHOLD_USDT = "billing.threshold_usdt"
BILLING_DUE_DAYS = "billing.due_days"
# Списки активов задаются по сети, поэтому это префиксы, а не готовые ключи.
BILLING_ASSETS_PREFIX = "billing.assets."
BILLING_PAYMENT_ASSETS_PREFIX = "billing.payment_assets."


# Переключатель хранится строкой. Признанные значения перечислены здесь — по
# одному набору на весь шлюз: пока каждый читатель понимал «включено» по-своему,
# одно и то же слово в одном месте включало настройку, а в другом нет.
FLAG_ON = "1"
FLAG_OFF = ""
FLAG_TRUE_VALUES = (FLAG_ON, "true", "yes", "on")
FLAG_FALSE_VALUES = (FLAG_OFF, "0", "false", "no", "off")


def is_on(raw: str | None) -> bool:
	"""Whether a switch setting is on; anything unrecognized counts as off."""
	return (raw or "").strip().lower() in FLAG_TRUE_VALUES
