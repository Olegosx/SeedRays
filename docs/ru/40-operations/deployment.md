[Оглавление](../index.md) · [Наблюдение](monitoring.md) · [English version](../../en/40-operations/deployment.md)

# Развёртывание

Развёртывание шлюза на боевом сервере: установка, первый запуск, служба systemd,
схемы публикации и обновление. Шлюз — один Python-процесс
([ADR-0003](../20-architecture/decisions/0003-single-process-supervised.md)),
обслуживающий HTTP API, статику фронтенда и watcher; всё его состояние живёт в одном
каталоге данных (SQLite в режиме WAL, журнал безопасности).

**Сначала о границе доверия**: онлайн-часть — только наблюдение
([ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md)) — сервер
хранит лишь публичные xpub и тратить средства не может. Компрометация сервера всё же
раскрывает почты пользователей, адреса, балансы и панель — относиться как к боевой
системе.

## Требования

- Linux-сервер; Python **3.13 или новее** (`requires-python` пакета).
- Исходящий HTTPS-доступ: к провайдеру TRON (TronGrid) и к почтовому сервису
  (Resend, [ADR-0020](../20-architecture/decisions/0020-mail-provider.md)).
- Входящий HTTPS: **через обратный прокси** — см. «Схемы публикации». Сам процесс
  шлюза в текущей версии TLS не расшифровывает, а куки сессий помечены `Secure` —
  браузер не сохранит их по нешифрованному HTTP (кроме localhost); боевое
  развёртывание без HTTPS неработоспособно.
- Диск: базы растут с пользователями и историей транзакций; журнал безопасности
  ограничен настройками ротации (по умолчанию — не более ~1,1 ГБ в худшем случае).

## Установка

```bash
sudo useradd --system --home /var/lib/seedrays --create-home seedrays
sudo git clone git@github.com:Olegosx/SeedRays.git /opt/seedrays
cd /opt/seedrays/backend
sudo -u seedrays python3 -m venv .venv
sudo -u seedrays .venv/bin/pip install -e .
```

Установка в режиме разработки (`-e`) оставляет пакет работать из копии
репозитория — так процесс и находит каталог `frontend/` рядом с собой, там же он ищет
конфигурационный файл рядом с кодом. При обычной установке (без `-e`) путь к статике
нужно задать явно настройкой `frontend_dir` конфигурации.

Каталог данных должен принадлежать служебному пользователю и не читаться остальными
(в нём базы и журнал безопасности):

```bash
sudo install -d -o seedrays -g seedrays -m 700 /var/lib/seedrays
```

## Конфигурация

Развёрточный уровень [ADR-0016](../20-architecture/decisions/0016-config-layers.md) и
[ADR-0026](../20-architecture/decisions/0026-configuration-file.md): файл TOML с тем, что
процессу нужно до открытия какой-либо базы. Всё остальное — настройки реестра, управляемые
из панели оператора без перезапуска.

Шлюз читает первый найденный файл:

1. `seedrays.toml` в копии репозитория — при раскладке выше это
   `/opt/seedrays/seedrays.toml`; место для разработки;
2. `/etc/seedrays/seedrays.toml` — место для сервера.

Аргумент `--config <путь>` перекрывает оба. Прочитанный файл называется в журнале при старте,
поэтому `journalctl -u seedrays | grep 'configuration read from'` всегда отвечает, какой из
них выиграл.

На сервере используйте `/etc/seedrays/seedrays.toml` и **убедитесь, что в каталоге кода не
остался `seedrays.toml`** — он молча перебьёт `/etc`. Через `git pull` он появиться не может:
в репозиторий коммитится только `seedrays.example.toml`.

```bash
sudo install -d -o seedrays -g seedrays -m 750 /etc/seedrays
sudo install -o seedrays -g seedrays -m 600 \
    /opt/seedrays/seedrays.example.toml /etc/seedrays/seedrays.toml
sudoedit /etc/seedrays/seedrays.toml
```

```toml
[gateway]
# The data directory: the registry database, the per-user databases, the archive of
# deleted users (archive/) and logs/security.log. Absolute on a server.
data_dir = "/var/lib/seedrays"

# API bind address; optional, defaults to 127.0.0.1:8080. Keep it on localhost —
# the reverse proxy is the public face.
bind = "127.0.0.1:8080"

# Static frontend directory; optional, defaults to the frontend/ directory of the
# checkout the package runs from.
# frontend_dir = "/opt/seedrays/frontend"
```

Права `600` здесь не для красоты: с переходом на PostgreSQL в этот файл ляжет строка
подключения к реестру вместе с паролем. По этой же причине развёрточный уровень — файл, а не
окружение процесса, которое `systemctl show` печатает любому пользователю
([ADR-0026](../20-architecture/decisions/0026-configuration-file.md)).

## Первый запуск

1. **Создать оператора** (регистрации операторов не существует —
   [ADR-0004](../20-architecture/decisions/0004-two-api-groups.md)):

   ```bash
   cd /opt/seedrays/backend
   sudo -u seedrays .venv/bin/python -m seedrays.cli operator-create --login admin
   ```

   Пароль запрашивается интерактивно (скрытый ввод). Миграции базы выполняются
   автоматически при каждом старте службы — отдельного шага миграций нет.

2. **Запустить службу** (unit systemd ниже) и войти в панель по адресу
   `https://ваш-домен/operator-login.html`.

3. **Заполнить настройки шлюза** на странице настроек панели:
   - `gateway.base_url` — публичный адрес шлюза; ссылки в письмах строятся
     **только** из этого значения (заголовку Host доверия нет). Без него настроенный
     отправитель почты не используется.
   - Почта: ключ API Resend и адрес отправителя (`mail.from`). Ограничение самого
     Resend: пока ваш домен отправки там не подтверждён, письма доставляются только
     на почту владельца учётной записи Resend.
   - Провайдер TRON: ключ API TronGrid и при необходимости темп запросов.
   - `gateway.trusted_proxies` — см. «Схемы публикации».
   - Ротация журнала безопасности, интервал/перекрытие watcher — если умолчания
     не подходят.
   - Наблюдаемые токен-контракты: настройка `watcher.contracts.<сеть>` (JSON-список
     адресов контрактов) сознательно не вынесена в панель и вносится вручную —
     решение владельца.
4. **Проверить**: зарегистрировать пользователя кабинета, прикрепить кошелёк,
   смотреть блок состояния watcher в панели (см. [Наблюдение](monitoring.md)).

## Служба systemd

`/etc/systemd/system/seedrays.service`:

```ini
[Unit]
Description=SeedRays crypto payment gateway
After=network-online.target
Wants=network-online.target

[Service]
User=seedrays
Group=seedrays
WorkingDirectory=/opt/seedrays/backend
ExecStart=/opt/seedrays/backend/.venv/bin/python -m seedrays.cli serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now seedrays
```

Сбои компонентов внутри процесса (API-сервер, watcher) перезапускает собственный
надсмотрщик шлюза, не трогая второй компонент; `Restart=always` покрывает гибель
процесса целиком. `SIGTERM` (его посылает `systemctl stop`) — плавная остановка:
открытые соединения API дорабатываются, watcher отменяется между проходами.

## Схемы публикации

Процесс шлюза слушает localhost; обратный прокси расшифровывает TLS и пересылает
запросы. Прокси же — место, где сохраняется реальный адрес посетителя: перечислите
всех доверенных посредников в настройке `gateway.trusted_proxies` (адреса и
CIDR-диапазоны через запятую; применяется при перезапуске) — тогда шлюз берёт адрес
клиента из `X-Forwarded-For`, но только у соединений с перечисленных прокси, так что
подделать заголовок снаружи нельзя. Без этого тормоз перебора и журнал безопасности
([ADR-0023](../20-architecture/decisions/0023-security-journal.md)) видят у всех
адрес прокси.

### nginx

```nginx
server {
	listen 443 ssl http2;
	server_name gateway.example.com;

	ssl_certificate     /etc/letsencrypt/live/gateway.example.com/fullchain.pem;
	ssl_certificate_key /etc/letsencrypt/live/gateway.example.com/privkey.pem;

	location / {
		proxy_pass http://127.0.0.1:8080;
		proxy_set_header Host $host;
		proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
		proxy_set_header X-Forwarded-Proto $scheme;
	}
}

server {
	listen 80;
	server_name gateway.example.com;
	return 301 https://$host$request_uri;
}
```

Сертификат: `certbot --nginx -d gateway.example.com` (Let's Encrypt; продление
устанавливается автоматически). Прокси на той же машине покрыт доверием по
умолчанию (`127.0.0.1`) — `gateway.trusted_proxies` можно не заполнять; прокси на
другой машине нужно вписать его адресом.

### Apache

Модули: `proxy`, `proxy_http`, `ssl`, `headers` (`a2enmod proxy proxy_http ssl headers`).

```apache
<VirtualHost *:443>
	ServerName gateway.example.com
	SSLEngine on
	SSLCertificateFile /etc/letsencrypt/live/gateway.example.com/fullchain.pem
	SSLCertificateKeyFile /etc/letsencrypt/live/gateway.example.com/privkey.pem

	ProxyPreserveHost On
	ProxyPass / http://127.0.0.1:8080/
	ProxyPassReverse / http://127.0.0.1:8080/
	RequestHeader set X-Forwarded-Proto "https"
</VirtualHost>
```

`mod_proxy_http` добавляет `X-Forwarded-For` сам; `X-Forwarded-Proto` задаётся явно
(выше). Сертификат: `certbot --apache -d gateway.example.com`.

### За Cloudflare

Cloudflare — посредник всегда: соединения приходят на сервер с адресов Cloudflare,
а IP посетителя едет в пересылаемых заголовках. Два дополнения к любой из схем выше:

- В панели Cloudflare — режим SSL/TLS **Full (strict)**: серверу всё равно нужен
  собственный действительный сертификат (Let's Encrypt или сертификат Cloudflare
  Origin CA); более слабые режимы оставляют плечо «Cloudflare → сервер» открытым.
- Добавьте официальные диапазоны Cloudflare (публикуются на
  <https://www.cloudflare.com/ips/>) в `gateway.trusted_proxies`, сохранив и
  локальный прокси, например: `127.0.0.1, 173.245.48.0/20, 103.21.244.0/22, …` —
  цепочка заголовка разбирается справа налево по всем доверенным звеньям, так что
  «Cloudflare → локальный nginx → шлюз» даёт реальный адрес посетителя. Диапазоны
  Cloudflare меняются редко, но меняются — сверяйте при обновлениях.
- В идеале порт 443 сервера принимает соединения только с диапазонов Cloudflare
  (правило межсетевого экрана): иначе атакующий, узнавший прямой IP сервера,
  подключится в обход.

### Ограничение панели оператора

Операторские маршруты живут на том же порту, что и кабинет. Если панель не должна
быть доступна из открытого интернета — ограничьте её на прокси, например в nginx:

```nginx
	location ~ ^/(v1/operator/|operator-) {
		allow 203.0.113.10;   # адреса администратора
		deny all;
		proxy_pass http://127.0.0.1:8080;
		proxy_set_header Host $host;
		proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
		proxy_set_header X-Forwarded-Proto $scheme;
	}
```

## Обновление

```bash
cd /opt/seedrays
sudo -u seedrays git pull
sudo -u seedrays backend/.venv/bin/pip install -e ./backend
sudo systemctl restart seedrays
```

Миграции базы выполняются автоматически при старте службы. Настройки с пометкой
«применяется после перезапуска» (доверенные прокси, ротация журнала) подхватываются
здесь же.

## Резервное копирование

Что копировать: весь каталог данных — реестровую базу, базы пользователей, архив
удалённых пользователей (`archive/`,
[ADR-0024](../20-architecture/decisions/0024-user-deletion-archive.md)) и, по
желанию, журнал безопасности. Сид-фраза **не** входит ни в какой бэкап: шлюз её
никогда не хранит ([ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md));
потеря сервера не теряет средств, а балансы пересчитываемы из блокчейна свежей
установкой с теми же xpub.

Базы — SQLite в режиме WAL; простое копирование файла живой базы может застать её
посреди записи. Два безопасных пути:

```bash
# 1. Холодная копия — остановить, скопировать, запустить:
sudo systemctl stop seedrays && cp -a /var/lib/seedrays /backup/seedrays-$(date +%F) && sudo systemctl start seedrays

# 2. Копия одной базы на ходу штатной командой SQLite:
sqlite3 /var/lib/seedrays/registry.db ".backup /backup/registry.db"
```

Восстановление — в обратном порядке: остановить службу, вернуть каталог, запустить.

## Связанные документы

- [Наблюдение](monitoring.md)
- [Обзор архитектуры](../20-architecture/overview.md)
- [ADR-0016: Слои конфигурации](../20-architecture/decisions/0016-config-layers.md)
- [ADR-0026: Конфигурационный файл как развёрточный уровень](../20-architecture/decisions/0026-configuration-file.md)
- [ADR-0023: Журнал событий безопасности](../20-architecture/decisions/0023-security-journal.md)
- [Сценарии панели оператора](../50-frontend/operator-panel.md)
- [Модель угроз](../30-security/threat-model.md)
