# ilya_safronov_hw2

ДЗ № 2, вариант 3: нагрузочное испытание сервиса записи на приём из ДЗ № 1.
Репозиторий: https://github.com/dumerpostpunk/ilya_safronov_hw2

Python 3.14, PostgreSQL 18, k6 2.4.0. Код сервиса и версии библиотек сохранены из ДЗ № 1. Нагрузочные сценарии находятся в [load/](load/).

Создать PostgreSQL-базы `ilya_safronov_hw2` и `ilya_safronov_hw2_test`, в корне записать `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:ПАРОЛЬ@localhost:5432/ilya_safronov_hw2
DB_SCHEMA=ilya_safronov
APP_USERNAME=demo
APP_PASSWORD=demo123
APP_PORT=8020
```

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
New-Item -ItemType Directory -Force .runtime | Out-Null
python -m scripts.seed --size large
python -m scripts.run
```

Интерфейс: http://127.0.0.1:8020, вход `demo` / `demo123`. Схема API: http://127.0.0.1:8020/docs. Автоматический запуск испытания сам поднимает сервер, поэтому отдельный сервер перед ним нужно остановить.

Установить [k6 для Windows](https://github.com/grafana/k6/releases/tag/v2.4.0), добавить его в PATH. Допускается положить `k6.exe` в `.tools/`. Проверка тестов: `python -m scripts.test_postgres`.

```powershell
python -m load.run --mode all
python -m load.run --mode profile --rate 16
python -m load.run --mode tuning --rate 32
```

`all` выполняет смешанный и тяжёлый сценарии ступенями 1, 2, 4, 8 … запросов/с, по 30 секунд, до первого нарушения p95 < 500 мс или ошибок < 1 %. Перед каждой ступенью заново наполняется только `ilya_safronov_hw2`; основная БД ДЗ № 1 не затрагивается. Между ступенями проверяется возврат CPU к простою. В `tuning` проверяется тяжёлая операция с двумя workers, в остальных режимах — с одним; после выхода сервер останавливается.

Отдельная ступень после подготовки фикстур:

```powershell
python -m load.run --mode prepare
k6 run -e MODE=mixed -e RATE=8 -e DURATION=30s -e BASE_URL=http://127.0.0.1:8020 load/test.js
```

Для неё сервер запускается отдельно. Для одиночной нагрузки `MODE=heavy`: сводка, самая тяжёлая операция по p95 ДЗ № 1. Все запросы авторизованы; проверка учётной записи выполняется в `setup` вне учитываемых метрик. Одна итерация — один HTTP-запрос. Смешанный профиль: список 30 %, карточка 20 %, слоты 15 %, специалисты и услуги по 10 %, сводка, создание и отмена по 5 %. Обоснования: [load/profile.json](load/profile.json). 409 создания и отмены считаются ожидаемыми отказами.

Результаты — [load/results/](load/results/): `stages.csv`, `resources.csv`, `operations.csv`, профиль функций, журнал 40 пройденных тестов. Исходные точки k6, сводки, логи всех 19 прогонов, два профиля py-spy и SQL-планы собраны в `raw_data.zip`. Для повторной проверки распаковать его в `load/results/` и выполнить `python -m load.analyze`. CPU приложения и PostgreSQL указан в процентах одного ядра, RSS — сумма RSS процессов, включая общие страницы. Интервал мониторинга — 1–2 с.

В основной серии последний проходящий порог: смешанный профиль 32 запрос/с, сводка 16 запрос/с. Повторная сводка на 32 запрос/с прошла порог; влияние состояния процесса, профилировщика и workers разобрано в [отчёте](report/REPORT_DZ2.docx). Графики — [screenshots/](screenshots/).

Парный опыт с workers (выполнить как Python-код из корня проекта; для сводки заменить `mixed, 64` на `heavy, 32`):

```python
from load.run import start_server, stage, stop_server
for workers in (1, 2):
    server = start_server(workers)
    try:
        stage(server, 'mixed', 64, name=f'workers_{workers}_mixed', workers=workers)
    finally:
        stop_server(server)
```

## API

Все восемь операций требуют HTTP Basic. Параметры и ответы сервиса сохранены из ДЗ № 1; время UTC, даты `YYYY-MM-DD`, период `[start_date, end_date)`.

| Метод и путь | Параметры | Ответ | Ошибки |
|---|---|---|---|
| GET `/api/specialists` | — | `[{id, full_name, specialization}]` | 401 |
| GET `/api/services` | — | `[{id, name, duration_minutes}]` | 401 |
| GET `/api/slots` | `service_id>0`; необязательные `date`, `specialist_id>0`, `limit=100` (1–500) | `[{id, specialist_id, specialist, starts_at, ends_at}]` | 401, 404, 422 |
| GET `/api/appointments` | `page=1` ≥1, `size=20` (1–100), необязательные `status=booked/cancelled`, `specialist_id>0` | `{items:[Appointment], total, page, size}` | 401, 422 |
| GET `/api/appointments/{id}` | Целочисленный ID | `Appointment` | 401, 404, 422 |
| POST `/api/appointments` | `{slot_id>0, service_id>0, client_name}`; имя после trim 2–120 символов | 201, `Appointment` | 401, 404, 409, 422 |
| POST `/api/appointments/{id}/cancel` | Целочисленный ID | `{message, appointment:Appointment}` | 401, 404, 409, 422 |
| GET `/api/summary` | `start_date=2026-10-12`, `end_date=2026-11-12`; конец позже начала | `{start_date, end_date, items:[Summary]}` | 401, 422 |

Чтение и отмена возвращают 200. `Appointment`: `id`, `client_name`, `slot_id`, `service_id`, `service`, `duration_minutes`, `specialist_id`, `specialist`, `specialization`, `starts_at`, `ends_at`, `status`, `created_at`. `Summary`: `specialist_id`, `specialist`, `specialization`, `total_appointments`, `booked_appointments`, `cancelled_appointments`, `cancelled_percent`, `booked_minutes`, `available_minutes`, `load_percent`.

Ошибки: `{"detail":"сообщение"}`, для валидации 422 `detail` — список. Все ответы API содержат `X-SQL-Count`, `X-SQL-Time-Ms` и `Cache-Control: no-store`. Отмена сохраняет историю; пересечения проверяются по специалисту на интервале услуги, соприкасающиеся границы разрешены.
