# tg-gift-sniper

Снайпер коллекционных подарков Telegram — мониторит маркет ресейла и автоматически выкупает подарки ниже заданной цены.

## Как это работает

1. Подключается к Telegram через MTProto (Telethon) от имени вашего userbot-аккаунта.
2. В цикле поллит `payments.getResaleStarGifts` по каждой коллекции из конфига, сортируя по цене (дешёвые первыми).
3. Если находит подарок с ценой ≤ вашего порога — покупает через `payments.getPaymentForm` + `payments.sendStarsForm`.
4. Опционально шлёт уведомление в указанный чат.

## Требования

- Python 3.10+
- Отдельный Telegram-аккаунт (настоятельно рекомендуется, **не** основной)
- Баланс Stars на аккаунте
- *(Опционально)* `api_id` и `api_hash` с [my.telegram.org](https://my.telegram.org) — если не указать, используются публичные ключи Telegram Desktop

## Быстрый старт

### 1. Клонировать и установить

```bash
git clone https://github.com/v93298402-a11y/tg-gift-sniper.git
cd tg-gift-sniper
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Настроить `.env`

```bash
cp .env.example .env
```

API-ключи вписывать **не обязательно** — по умолчанию используются публичные ключи Telegram Desktop. Если хотите свои — раскомментируйте `API_ID` и `API_HASH` в `.env`.

### 3. Настроить `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Отредактировать `config.yaml`:

| Параметр | Описание |
|---|---|
| `poll_interval` | Интервал поллинга в секундах (рекомендуется 2-5) |
| `dry_run` | `true` — только логирует, не покупает. **Начните с `true`!** |
| `max_spend_per_buy` | Максимум Stars за одну покупку |
| `targets` | Список коллекций: `gift_id`, `max_price`, `name` |
| `notify_chat_id` | ID чата для уведомлений о покупках (опционально) |

### 4. Авторизоваться

При первом запуске Telethon попросит ввести номер телефона и код из Telegram:

```bash
python -m sniper
```

Сессия сохранится в файл `sniper.session` — при следующих запусках логин не потребуется.

### 5. Запустить

```bash
# Dry-run (по умолчанию) — смотрим что ловится
python -m sniper

# Боевой режим — поменять dry_run: false в config.yaml
python -m sniper
```

## Конфиг на лету

Конфиг перечитывается каждые ~60 секунд (20 циклов). Можно менять `targets`, `dry_run`, `poll_interval`, `max_spend_per_buy` без перезапуска.

## Как найти gift_id

`gift_id` — это ID базового подарка (не уникального). Его можно узнать:
- Из API: вызвать `payments.getStarGifts` и найти нужный по названию
- Из ссылки на подарок в Telegram

## Структура проекта

```
tg-gift-sniper/
├── sniper/
│   ├── __init__.py
│   ├── __main__.py    # Точка входа
│   ├── config.py      # Загрузка конфига и .env
│   ├── buyer.py       # Логика покупки (getPaymentForm + sendStarsForm)
│   └── poller.py      # Поллинг getResaleStarGifts + основной цикл
├── config.example.yaml
├── .env.example
├── pyproject.toml
└── README.md
```

## Whale-feed mode (`--whale`)

Отдельный режим: мониторит **продажи всех коллекций** Telegram Resale и публикует
в указанный канал каждую продажу с ценой ≥ заданного порога (по умолчанию 100 TON).
Режим формата как у [@giftwhalefeed](https://t.me/giftwhalefeed).

### Как работает

1. Раз в ~60 секунд получает список всех коллекций (`payments.getStarGifts`).
2. Для каждой коллекции читает топ-100 самых свежих листингов (default sort —
   по unixtime изменения цены, новейшие первыми).
3. Запоминает листинги с ценой ≥ порога вместе с адресом продавца.
4. Когда такой листинг исчезает из топа — вызывает
   `payments.getUniqueStarGift(slug)` и сравнивает текущего владельца со
   снапшотом продавца. Если адрес сменился — публикует продажу в канал.

### Настройка

1. Создайте отдельного бота через [@BotFather](https://t.me/BotFather).
2. Создайте публичный канал, добавьте бота админом с правом публикации.
3. Заполните `.env`:

   ```env
   WHALE_BOT_TOKEN=123456:ABC...
   WHALE_CHANNEL=@your_channel
   ```

4. Запуск:

   ```bash
   python -m sniper --whale
   # или с другим порогом
   python -m sniper --whale --whale-threshold 50
   ```

## Риски

- **ToS Telegram.** Userbot-ы формально нарушают ToS. Используйте отдельный аккаунт.
- **FLOOD_WAIT.** При агрессивном поллинге Telegram выдаёт временные баны на методы. Бот автоматически ждёт указанное время.
- **Конкуренция.** Другие снайперы (включая gift_satellite_bot) работают на серверах рядом с Telegram DC. Для минимальной задержки разверните на VPS в Нидерландах.
- **Потеря средств.** Убедитесь, что пороги цен выставлены правильно. Начинайте с `dry_run: true`.

## Деплой на VPS

```bash
# На VPS (Ubuntu)
sudo apt update && sudo apt install -y python3.12 python3.12-venv
git clone https://github.com/v93298402-a11y/tg-gift-sniper.git && cd tg-gift-sniper
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env && cp config.example.yaml config.yaml
# Отредактировать config.yaml (gift_id, max_price)
python -m sniper  # Первый раз — авторизация
```

Для автозапуска через systemd:

```bash
sudo tee /etc/systemd/system/tg-sniper.service << 'EOF'
[Unit]
Description=Telegram Gift Sniper
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/tg-gift-sniper
ExecStart=/home/ubuntu/tg-gift-sniper/.venv/bin/python -m sniper
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now tg-sniper
```

## Лицензия

MIT
