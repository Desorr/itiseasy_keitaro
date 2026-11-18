# Менеджер кампаний Keitaro

Создание кампаний через Keitaro Admin API и редактирование офферов в потоках. Трафик распределяет Keitaro; PostgreSQL хранит состояние, черновики, закрепления и архив привязок. Интерфейс — на английском.

Стек: Python 3.11 / FastAPI / httpx, PostgreSQL 17 / SQLAlchemy / Alembic, React / TypeScript / Vite.

## Запуск

Нужны [Git](https://git-scm.com/install/), запущенный [Docker с Compose](https://docs.docker.com/get-started/get-docker/), адрес Keitaro и API-ключ. Python и Node.js для Docker-запуска отдельно не нужны.

```sh
git clone https://github.com/Desorr/itiseasy_keitaro.git
cd itiseasy_keitaro
```

Создайте `.env` в корне проекта (`nano .env` на macOS/Linux или `notepad .env` на Windows). Замените шаблоны своими значениями:

```dotenv
KEITARO_BASE_URL=https://your-tracker.example
KEITARO_API_KEY=replace_with_api_key
KEITARO_TIMEOUT_SECONDS=20
POSTGRES_DB=keitaro
POSTGRES_USER=keitaro
POSTGRES_PASSWORD=replace_with_random_hex_password
POSTGRES_PORT=55432
DATABASE_URL=postgresql+asyncpg://keitaro:replace_with_random_hex_password@127.0.0.1:55432/keitaro
```

Пароль в `DATABASE_URL` должен совпадать с `POSTGRES_PASSWORD`; специальные символы нужно URL-кодировать. Удобно использовать hex-пароль (`openssl rand -hex 24`). Compose формирует подключение из `POSTGRES_*`, а `DATABASE_URL` нужен для локального Python. Таймаут Keitaro — больше 0 и до 120 секунд, по умолчанию 20.

Ключу нужны права чтения кампаний, потоков, офферов, доменов, источников и групп, создания кампаний/потоков/группы, активации и архивирования кампаний, изменения привязок потоков. Ключ используется только backend. Не публикуйте `.env` и секреты.

```sh
docker compose up -d --build
docker compose ps
```

Миграции применяются автоматически. Сервисы `postgres`, `backend`, `frontend` должны работать; PostgreSQL и backend — иметь статус healthy.

- [Приложение](http://localhost:8080)
- [Swagger API](http://localhost:8000/docs)
- [Health](http://localhost:8000/api/health) — проверяет БД и наличие конфигурации, но не доступ к Keitaro.

Порты доступны только на этом компьютере (`127.0.0.1`). PostgreSQL опубликован на порту 55432.

## Обновление и диагностика

Все команды выполняются из корня проекта.

```sh
# Остановка с сохранением данных
docker compose down
# Повторный запуск
docker compose up -d
# Обновление из GitHub
git pull --ff-only
docker compose up -d --build
# Состояние и логи
docker compose ps
docker compose logs --tail=100 backend frontend postgres
```

После изменения `.env` также выполните `docker compose up -d --build`. Только frontend: `docker compose up -d --build --no-deps frontend`.

При проблемах проверьте Docker, заполнение `.env`, свободные порты и права API-ключа. Доступ к кампаниям не гарантирует доступ к офферам: 401/403 и пустой список проверяются отдельно.

Данные лежат в томе `postgres_data`: `docker compose down -v` удаляет их. Изменение `POSTGRES_PASSWORD` в `.env` не меняет пароль уже созданного пользователя БД. Для другого Keitaro-трекера нужна отдельная БД.

## Работа с кампаниями

Сначала показывается список из БД, затем идёт фоновая синхронизация Keitaro. `Refresh` обновляет вручную. Приложение работает со своими кампаниями по alias `tm_<16 hex>`; удаление архивирует кампанию, сохраняя группу и офферы.

Для создания нужны название 5–255 символов, один двухбуквенный GEO и оффер из dropdown. При каждом открытии формы офферы загружаются свежим запросом из Keitaro; поиск локальный по ID, при ошибке доступна `Retry`. Отдельной проверки `GET /offers/{id}` при Create нет.

Домен и источник — первые активные в ответе API; их кэш действует до 60 секунд. Группы при Create читаются заново: приоритет `FORTESTS`, затем `test_markovskiy_group`; если нет обеих, создаётся последняя.

Кампания создаётся выключенной (`position`, `ip_ua`, USD, `cost_auto=true`) с двумя потоками:

- `Flow 1`: выбранный GEO → HTTP-редирект на `https://google.com`.
- `Flow 2`: остальные посетители → выбранный оффер 100%, без фильтров и лендингов.

Настройки проверяются до и после активации. Дополнительные потоки создаются в Keitaro. При неизвестном результате создания сохраняются данные для сверки; автоматически повторять POST после таймаута нельзя.

## Редактор потоков

После `Edit` нажмите `Fetch streams from KT`. До этого список пуст, сохранённые черновики и архив остаются в БД. Потоки с известными офферами идут первыми. Редактируются привязки потоков со схемой `landings` и известными офферами.

| Действие | Результат |
|---|---|
| `Add` | Добавить активный оффер в черновик; дубликаты запрещены. |
| `Remove` | Архивировать привязку с 0%, включая последнюю. Сам оффер не удаляется. |
| `Bring back` | Вернуть привязку без закрепления. |
| `Pin` / `Unpin` | Закрепить текущую долю; вне черновика публикация не требуется. |
| `Push to KT` | Записать только офферы выбранного потока и проверить результат. |
| `Cancel` | Отменить черновик и принять подтверждённое состояние. |

Список офферов загружается один раз при первом фокусе поля и используется во всех потоках до выхода из редактора. После ошибки следующий фокус повторяет загрузку. Add/Bring back отдельно проверяют доступность и активность оффера на backend.

Свободная доля делится поровну целыми процентами со стабильным остатком (например, 34/33/33). Активные доли дают 100%; без активных офферов можно опубликовать `offers=[]`. Удаление снимает pin. Черновики, архив и закрепления сохраняются после перезапуска.

Импорт принимает фактические доли без пересчёта и сохраняет черновик. Внешние изменения вызывают конфликт и блокируют публикацию до его разрешения. Ревизии защищают от устаревших изменений (HTTP 409); атомарного CAS в Keitaro нет.

После таймаута публикация автоматически не повторяется. Сверка чтением или синхронизацией доступна через `2 × timeout + 10` секунд от начала (по умолчанию 50 секунд); до неё Push и Cancel блокируются. Черновик пропавшего потока можно отменить после сверки.

## API и разработка

Полные маршруты, схемы и ошибки — в [Swagger](http://localhost:8000/docs) и [OpenAPI](http://localhost:8000/openapi.json). Execute для изменяющих методов выполняет реальные операции.

Список, синхронизация и создание кампаний возвращают только `id`, `name`, `alias`, `state`. Создание требует UUID в `Idempotency-Key`; alias помогает выяснить результат и не является секретом или авторизацией. Изменения редактора требуют `expected_revision`.

Основной код — `backend/app` и `frontend/src`, миграции — `backend/migrations`. Правила проекта — в [AGENTS.md](AGENTS.md).

Для локальной разработки нужны Python 3.11, Node.js 24 и настроенный `.env`:

```sh
docker compose up -d postgres
python3 -m venv venv
venv/bin/pip install -r backend/requirements.txt
cd backend
../venv/bin/alembic upgrade head
../venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Во втором терминале из корня:

```sh
cd frontend
npm ci
npm run dev
```

Vite доступен на [localhost:5173](http://localhost:5173) и проксирует `/api` на backend.

Разрешённые проверки:

```sh
npm --prefix frontend run build
venv/bin/python -m compileall -q backend/app backend/migrations
git diff --check
```

Проверочные кампании — только `test_markovskiy_`, после проверки архивируются. Коммит и push — по запросу. Секреты, зависимости, сборки и данные БД исключены из Git.
