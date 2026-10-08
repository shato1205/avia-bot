# Бот дешёвых авиабилетов

Каждые 2 часа проверяет цены через Travelpayouts (данные Aviasales) и присылает
в Telegram сообщение вида «🔥 ГОША СРОЧНО ТАИЛАНД (Пхукет) 18 500 ₽», если билет
дешевле порога из `config.json` или на 30% дешевле медианы за последние 30 дней.

## Настройка (один раз)

1. **Токен Travelpayouts.** Зарегистрируйся на travelpayouts.com, открой
   «Профиль → API токен» и скопируй его.
2. **Telegram-бот.** Напиши @BotFather команду `/newbot`, получи токен бота.
   Затем напиши своему боту любое сообщение и открой
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates`: число `chat.id` это твой chat id.
3. **Секреты в GitHub.** В репозитории: Settings → Secrets and variables → Actions →
   New repository secret. Добавь `TRAVELPAYOUTS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. **Первый запуск.** Вкладка Actions → «Проверка цен» → Run workflow.

## Направления

Редактируй `config.json`: `origin` это город вылета (MOW = Москва, LED = Питер),
в `routes` код аэропорта назначения и порог `max_price` в рублях.
