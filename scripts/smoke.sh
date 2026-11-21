#!/usr/bin/env bash
# Проверка конфигурации Kong на запущенном шлюзе: маршруты, закрытые пути, CORS, лимиты,
# корреляционный идентификатор. Сервисы за шлюзом запускать не обязательно: проверки
# маршрутов смотрят только на то, что Kong нашёл маршрут. Если auth запущен, дополнительно
# проверяются ответы самого auth.
#
# Лимит на вход проверяется последним: после него вход с этого адреса закрыт на минуту.
set -uo pipefail

GATEWAY="${GATEWAY:-http://localhost:8000}"
ORIGIN="${ORIGIN:-http://localhost:5173}"
NO_ROUTE='no Route matched'
failures=0

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

# Тело и код ответа: печатает тело, последней строкой код.
request() { curl -s -o - -w '\n%{http_code}' "$@"; }
status_of() { request "$@" | tail -n 1; }
body_of() { request "$@" | sed '$d'; }

routed() { # метод путь сервис
  local body
  body=$(body_of -X "$1" "$GATEWAY$2")
  if grep -q "$NO_ROUTE" <<<"$body"; then fail "$1 $2 не маршрутизируется в $3"; else pass "$1 $2 -> $3"; fi
}

closed() { # путь
  local response
  response=$(request "$GATEWAY$1")
  if [[ $(tail -n 1 <<<"$response") == 404 ]] && grep -q "$NO_ROUTE" <<<"$response"; then
    pass "$1 закрыт снаружи (404)"
  else
    fail "$1 доступен снаружи: $(tail -n 1 <<<"$response")"
  fi
}

echo "Маршруты"
routed GET  /.well-known/jwks.json auth
routed POST /api/v1/auth/refresh auth
routed GET  /api/v1/me auth
routed GET  /api/v1/users/00000000-0000-0000-0000-000000000000 auth
routed GET  /api/v1/submissions submission
routed GET  /api/v1/location-suggestions submission
routed GET  /api/v1/moderation/queue moderation
routed POST /api/v1/reports moderation
routed GET  /api/v1/media-assets/00000000-0000-0000-0000-000000000000 media
routed GET  /api/v1/catalog/places/00000000-0000-0000-0000-000000000000 catalog
routed GET  /api/v1/search/places search

echo "Закрытые пути"
closed /catalog/v1/places/00000000-0000-0000-0000-000000000000
closed /media/v1/assets/00000000-0000-0000-0000-000000000000
closed /internal/v1/users/00000000-0000-0000-0000-000000000000/authz
closed /actuator/health
closed /v3/api-docs
closed /api/v1/mexico      # префикс /api/v1/me не должен захватывать соседние пути
closed /api/v1/searchable

echo "Корреляционный идентификатор"
headers=$(curl -s -o /dev/null -D - -H 'X-Correlation-ID: smoke-check-1' "$GATEWAY/api/v1/search/places")
if grep -qi '^x-correlation-id: smoke-check-1' <<<"$headers"; then pass "переданный идентификатор возвращается"; else fail "переданный идентификатор не вернулся"; fi
headers=$(curl -s -o /dev/null -D - "$GATEWAY/api/v1/search/places")
if grep -qiE '^x-correlation-id: [0-9a-f-]{36}' <<<"$headers"; then pass "без заголовка Kong генерирует UUID"; else fail "идентификатор не сгенерирован"; fi
headers=$(curl -s -o /dev/null -D - -H 'X-Correlation-ID: bad id!!' "$GATEWAY/api/v1/search/places")
if grep -qiE '^x-correlation-id: [0-9a-f-]{36}' <<<"$headers"; then pass "недопустимый идентификатор заменяется на UUID"; else fail "недопустимый идентификатор не заменён"; fi

echo "CORS"
headers=$(curl -s -o /dev/null -D - -X OPTIONS "$GATEWAY/api/v1/submissions" \
  -H "Origin: $ORIGIN" -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: Authorization, Content-Type, Idempotency-Key')
if grep -qi "^access-control-allow-origin: $ORIGIN" <<<"$headers"; then pass "preflight с $ORIGIN разрешён"; else fail "preflight с $ORIGIN не разрешён"; fi
headers=$(curl -s -o /dev/null -D - -X OPTIONS "$GATEWAY/api/v1/submissions" \
  -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST')
if grep -qi '^access-control-allow-origin: https://evil.example' <<<"$headers"; then fail "чужой origin разрешён"; else pass "чужой origin не разрешён"; fi

echo "Размер запроса"
big=$(mktemp); trap 'rm -f "$big"' EXIT
head -c 1100000 /dev/zero >"$big"
code=$(status_of -X POST "$GATEWAY/api/v1/submissions" -H 'Content-Type: application/json' --data-binary "@$big")
if [[ $code == 413 ]]; then pass "тело больше 1 МБ отклоняется (413)"; else fail "тело больше 1 МБ: $code вместо 413"; fi
code=$(status_of -X POST "$GATEWAY/api/v1/media-assets" -H 'Content-Type: application/octet-stream' --data-binary "@$big")
if [[ $code != 413 ]]; then pass "media принимает тело больше 1 МБ ($code)"; else fail "media отклоняет тело больше 1 МБ"; fi

echo "Сервис auth"
if [[ $(status_of "$GATEWAY/.well-known/jwks.json") == 200 ]]; then
  pass "jwks доступен через шлюз"
  code=$(status_of -X POST "$GATEWAY/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d '{"email":"nobody@example.com","password":"wrong-password"}')
  if [[ $code == 401 ]]; then pass "неверный вход через шлюз: 401 от auth"; else fail "неверный вход через шлюз: $code"; fi
else
  echo "  skip  auth не запущен на :8080"
fi

echo "Лимит на вход"
last=''
for _ in $(seq 1 11); do
  last=$(status_of -X POST "$GATEWAY/api/v1/auth/login" -H 'Content-Type: application/json' -d '{}')
  [[ $last == 429 ]] && break
done
if [[ $last == 429 ]]; then pass "после 10 попыток в минуту вход отклоняется (429)"; else fail "лимит на вход не сработал: $last"; fi
code=$(status_of -X POST "$GATEWAY/api/v1/auth/register" -H 'Content-Type: application/json' -d '{}')
if [[ $code != 429 ]]; then pass "у регистрации свой счётчик ($code)"; else fail "регистрация заблокирована лимитом входа"; fi

echo
if ((failures)); then echo "Провалено проверок: $failures"; exit 1; fi
echo "Все проверки прошли"
