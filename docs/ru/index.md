# Документация SeedRays

Точка входа в документацию проекта. Каждый документ этой ветки перечислен здесь.

[English version](../en/index.md)

## Содержание

- [Обзор](00-overview.md)
- Требования
  - [Функциональные требования](10-requirements/functional.md)
  - [Нефункциональные требования](10-requirements/non-functional.md)
- Архитектура
  - [Обзор архитектуры](20-architecture/overview.md)
  - [Модель данных](20-architecture/data-model.md)
  - Компоненты
    - [Оркестратор](20-architecture/components/orchestrator.md)
    - [Генератор ключа](20-architecture/components/key-generator.md)
    - [Модуль деривации](20-architecture/components/derivation.md)
    - [Watcher](20-architecture/components/watcher.md)
    - [Абстракция блокчейна](20-architecture/components/chain-abstraction.md)
    - [Слой хранения](20-architecture/components/storage.md)
    - [HTTP API](20-architecture/components/http-api.md)
  - [Журнал архитектурных решений](20-architecture/decisions/index.md)
    - [ADR-0001: Ядро-библиотека с тонкими обёртками](20-architecture/decisions/0001-library-first-core.md)
    - [ADR-0002: Наблюдающая онлайн-часть; пути генерации ключа](20-architecture/decisions/0002-watch-only-online-part.md)
    - [ADR-0003: Один процесс бэкенда с оркестратором-надсмотрщиком](20-architecture/decisions/0003-single-process-supervised.md)
    - [ADR-0004: Две группы маршрутов API с разными правами](20-architecture/decisions/0004-two-api-groups.md)
    - [ADR-0005: Многопользовательская модель — база и каталог на пользователя, три роли](20-architecture/decisions/0005-multi-user-model.md)
    - [ADR-0006: Абстракция хранения на уровне операций предметной области](20-architecture/decisions/0006-storage-abstraction.md)
    - [ADR-0007: Адресоцентричный проход watcher](20-architecture/decisions/0007-address-centric-watcher.md)
    - [ADR-0008: Общая реестровая база данных](20-architecture/decisions/0008-shared-registry-db.md)
    - [ADR-0009: Постоянные привязки адресов как основной режим](20-architecture/decisions/0009-address-bindings-primary-mode.md)
    - [ADR-0010: Сети, активы и структуры финансовых данных](20-architecture/decisions/0010-networks-assets-financial-data.md)
    - [ADR-0011: Принципы API приложений](20-architecture/decisions/0011-application-api-principles.md)
    - [ADR-0012: Стек фронтенда — статика без сборки с завендоренными библиотеками](20-architecture/decisions/0012-frontend-stack.md)
    - [ADR-0013: Стек бэкенда](20-architecture/decisions/0013-backend-stack.md)
    - [ADR-0014: Стандарты генерации ключей и деривации](20-architecture/decisions/0014-key-standards.md)
    - [ADR-0015: Провайдер данных TRON — TronGrid; интерфейс источника цепочки](20-architecture/decisions/0015-tron-provider.md)
    - [ADR-0016: Уровни конфигурации](20-architecture/decisions/0016-config-layers.md)
    - [ADR-0017: Универсальная модель транзакций — две таблицы по физическому местоположению](20-architecture/decisions/0017-universal-tx-model.md)
    - [ADR-0018: Диапазонное сканирование как основной съём watcher](20-architecture/decisions/0018-range-scanning.md)
    - [ADR-0019: Локализация фронтенда — клиентские словари](20-architecture/decisions/0019-frontend-localization.md)
    - [ADR-0020: Отправка почты — сторонний сервис за абстракцией](20-architecture/decisions/0020-mail-provider.md)
    - [ADR-0021: Двухфазное сканирование — финализированная истина и предварительный показ](20-architecture/decisions/0021-two-phase-scanning.md)
    - [ADR-0022: Анонимные маршруты аутентификации за самодостаточной proof-of-work капчей](20-architecture/decisions/0022-pow-captcha.md)
    - [ADR-0023: Журнал событий безопасности — точность внутри, нейтральность наружу](20-architecture/decisions/0023-security-journal.md)
    - [ADR-0024: Удаление пользователя — двухшаговое, через серверный архив](20-architecture/decisions/0024-user-deletion-archive.md)
    - [ADR-0025: Экземпляры приложения — пространство имён пользователей приложения](20-architecture/decisions/0025-application-instances.md)
    - [ADR-0026: Конфигурационный файл как развёрточный уровень](20-architecture/decisions/0026-configuration-file.md)
    - [ADR-0027: Вознаграждение владельца шлюза — оборот, счета и приостановка доступа](20-architecture/decisions/0027-gateway-fee-billing.md)
- Безопасность
  - [Управление ключами](30-security/key-management.md)
  - [Модель угроз](30-security/threat-model.md)
- Эксплуатация
  - [Развёртывание](40-operations/deployment.md)
  - [Мониторинг](40-operations/monitoring.md)
- Фронтенд
  - [Сценарии кабинета пользователя](50-frontend/user-cabinet.md)
  - [Сценарии панели оператора](50-frontend/operator-panel.md)
- [Словарь терминов](glossary.md)
