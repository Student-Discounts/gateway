# gateway

Единая точка входа платформы студенческих скидок на Kong 3.9.4 без базы данных: вся конфигурация в [kong.yml](kong.yml).

## Запуск

Нужен Docker. Сервисы запускаются на хосте как обычно, Kong обращается к ним через `host.docker.internal`.

```bash
docker compose up -d --wait   # http://localhost:8000
./scripts/smoke.sh            # проверка маршрутов, закрытых путей, CORS и лимитов
```

После изменения `kong.yml` нужен `docker compose restart kong`. Проверить файл без запуска: `docker compose run --rm --no-deps kong kong config parse /kong/kong.yml`.

## Маршруты

| Путь | Сервис | Порт на хосте |
|---|---|---|
| `/api/v1/auth`, `/api/v1/me`, `/api/v1/users`, `/.well-known/jwks.json` | auth | 8080 |
| `/api/v1/submissions`, `/api/v1/location-suggestions` | submission | 8081 |
| `/api/v1/moderation`, `/api/v1/reports` | moderation | 8082 |
| `/api/v1/media-assets` | media | 8083 |
| `/api/v1/catalog` | catalog | 8084 |
| `/api/v1/search` | search | 8085 |

Пути передаются сервисам без изменений. Пока порты catalog и search не поменяны в самих сервисах, их нужно запускать с `CATALOG_PORT=8084` и `SERVER_PORT=8085` соответственно.

Всё остальное Kong не маршрутизирует и отвечает `404`. Это закрывает снаружи внутренние API (`/catalog/v1`, `/media/v1`, `/internal`), `/actuator` и спецификации OpenAPI сервисов. Заголовок `X-Service-Token` из внешних запросов удаляется.

## Политики

| Что | Где | Значение |
|---|---|---|
| Лимит частоты (FR-053) | `POST /api/v1/auth/login`, `POST /api/v1/auth/register`, у каждого свой счётчик | 10 запросов в минуту с одного IP, затем `429` с заголовком `Retry-After` |
| Размер тела (FR-053) | все маршруты | 1 МБ, `413` при превышении |
| | `/api/v1/media-assets` | 11 МБ, как `max-request-size` в media |
| CORS | все маршруты | origin `http://localhost:5173`; заголовки `Authorization`, `Content-Type`, `If-Match`, `Idempotency-Key`, `X-Correlation-ID`; без cookies |
| Корреляционный идентификатор (NFR-030) | все маршруты | `X-Correlation-ID` от клиента передаётся сервису, если подходит под `[A-Za-z0-9._-]{1,64}` (формат, который принимают auth и submission); если заголовка нет или он недопустим, Kong генерирует UUID. Значение возвращается в ответе |

Лимит считается в памяти Kong (`policy: local`): при нескольких экземплярах шлюза каждый считает свой лимит. Для одного экземпляра этого достаточно. Лимиты — значения по умолчанию до ответа заказчика (вопрос З-9).

Ошибки самого Kong (`404`, `413`, `429`) приходят в формате `{"message": "..."}`, а не `application/problem+json`, как у сервисов.

Admin API Kong выключен. Готовность: `http://localhost:8100/status/ready`.

## Что пока не сделано

- Порты catalog (сейчас 8082, как у moderation) и search (сейчас 8080, как у auth) в самих сервисах не изменены: это предложение команде.
- Сервисы за шлюзом не запускаются в этом compose: каждый сервис поднимается из своего репозитория.
- TLS: для локального запуска не нужен, в развёртывании его снимает Ingress или балансировщик перед Kong.
