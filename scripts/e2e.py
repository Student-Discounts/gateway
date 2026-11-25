#!/usr/bin/env python3
"""Сквозная проверка прав через шлюз на запущенных сервисах.

Нужны шлюз, auth, submission (со своими заглушками catalog и geo) и moderation.
Media, catalog и search проверяются, если запущены, иначе их проверки пропускаются.
Скрипт создаёт новых пользователей на каждый запуск и назначает модератора через API auth,
поэтому нужен существующий старший модератор: SD_E2E_SENIOR_EMAIL и SD_E2E_SENIOR_PASSWORD.
Созданные пользователи и заявка остаются в базах: запускать только в dev-окружении.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

GATEWAY = os.environ.get('GATEWAY_URL', 'http://localhost:8000').rstrip('/')
RUN = uuid.uuid4().hex[:12]
PASSWORD = 'e2e-' + uuid.uuid4().hex
failures = []
skipped = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = urllib.request.build_opener(NoRedirect())


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def code(self):
        return self.body.get('code') if isinstance(self.body, dict) else None


def call(method, path, token=None, body=None, headers=None):
    sent = {'X-Correlation-ID': 'e2e-' + RUN}
    if token:
        sent['Authorization'] = 'Bearer ' + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        sent['Content-Type'] = 'application/json'
    sent.update(headers or {})
    request = urllib.request.Request(GATEWAY + path, data=data, headers=sent, method=method)
    try:
        response = HTTP.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        parsed = raw
        if raw and 'json' in response.headers.get('Content-Type', ''):
            parsed = json.loads(raw)
        return Response(response.status, response.headers, parsed)


def check(description, condition, detail=''):
    if condition:
        print('ok    ' + description)
    else:
        failures.append(description)
        print('FAIL  ' + description + (f' ({detail})' if detail else ''))
    return condition


def expect(description, response, status, code=None):
    ok = response.status == status and (code is None or response.code == code)
    got = f'получено {response.status}' + (f', code {response.code}' if response.code else '')
    return check(description, ok, got)


def skip(description, reason):
    skipped.append(description)
    print(f'skip  {description}: {reason}')


def up(path):
    """Сервис за шлюзом отвечает: 502-504 означают, что он не запущен."""
    return call('GET', path).status not in (502, 503, 504)


def eventually(description, action, predicate, timeout=45):
    deadline = time.monotonic() + timeout
    value = action()
    while not predicate(value) and time.monotonic() < deadline:
        time.sleep(0.5)
        value = action()
    return check(description, predicate(value))


def limited(method, path, body):
    """Вход и регистрация ограничены шлюзом: при 429 ждём Retry-After и повторяем."""
    for _ in range(3):
        response = call(method, path, body=body)
        if response.status != 429:
            return response
        time.sleep(int(response.headers.get('Retry-After') or 10) + 1)
    return response


def login(email, password):
    response = limited('POST', '/api/v1/auth/login', {'email': email, 'password': password})
    if response.status != 200:
        raise SystemExit(f'Не удалось войти как {email}: {response.status} {response.code}')
    return response.body


def new_user(name):
    email = f'e2e-{RUN}-{name}@example.com'
    created = limited('POST', '/api/v1/auth/register', {'email': email, 'password': PASSWORD, 'displayName': name})
    if created.status != 201:
        raise SystemExit(f'Не удалось зарегистрировать {email}: {created.status} {created.code}')
    return created.body['id'], login(email, PASSWORD)


def forged(token):
    """Тот же токен с испорченной подписью."""
    header, payload, signature = token.split('.')
    raw = bytearray(base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4)))
    raw[0] ^= 0xFF
    return '.'.join([header, payload, base64.urlsafe_b64encode(bytes(raw)).rstrip(b'=').decode()])


def submission_payload():
    return {
        'place': {'name': 'E2E ' + RUN, 'city': 'Санкт-Петербург', 'addressLine': 'Кронверкский пр., 49',
                  'categoryCode': 'cafe', 'location': {'lat': 59.9571, 'lon': 30.3083}},
        'offers': [{'conditions': 'Скидка по студенческому', 'discountType': 'PERCENT', 'discountValue': 10,
                    'source': {'type': 'WEBSITE', 'url': 'https://example.com/discount'}}],
    }


def main():
    senior_email = os.environ.get('SD_E2E_SENIOR_EMAIL')
    senior_password = os.environ.get('SD_E2E_SENIOR_PASSWORD')
    if not senior_email or not senior_password:
        raise SystemExit('Задайте SD_E2E_SENIOR_EMAIL и SD_E2E_SENIOR_PASSWORD')

    for name, path in [('auth', '/api/v1/me'), ('submission', '/api/v1/submissions'),
                       ('moderation', '/api/v1/moderation/queue')]:
        if not up(path):
            raise SystemExit(f'{name} не отвечает через шлюз {GATEWAY}')
    media_up = up('/api/v1/media-assets/' + str(uuid.uuid4()))
    catalog_up = up('/api/v1/catalog/places/' + str(uuid.uuid4()))
    search_up = up('/api/v1/search/places')

    senior = login(senior_email, senior_password)['accessToken']
    author_id, author_tokens = new_user('author')
    other_id, other_tokens = new_user('other')
    moderator_id, moderator_tokens = new_user('moderator')
    author, other = author_tokens['accessToken'], other_tokens['accessToken']

    print('\n# Без токена и с поддельным токеном')
    protected = [('GET', '/api/v1/me'), ('GET', '/api/v1/submissions'), ('GET', '/api/v1/moderation/queue'),
                 ('GET', '/api/v1/reports'), ('GET', '/api/v1/users/' + author_id)]
    if media_up:
        protected.append(('GET', '/api/v1/media-assets/' + str(uuid.uuid4())))
    for method, path in protected:
        expect(f'{method} {path} без токена: 401', call(method, path), 401)
        expect(f'{method} {path} с поддельной подписью: 401', call(method, path, forged(author)), 401)
    expect('ответ 401 от auth в формате problem+json с code', call('GET', '/api/v1/me'), 401, 'unauthorized')

    print('\n# Студент не получает чужих прав')
    expect('студент: очередь модерации 403', call('GET', '/api/v1/moderation/queue', author), 403)
    expect('студент: жалобы для модераторов 403', call('GET', '/api/v1/moderation/reports', author), 403)
    expect('студент: поиск пользователя 403', call('GET', '/api/v1/users/' + other_id, author), 403, 'access_denied')
    expect('студент: блокировка другого 403', call('POST', f'/api/v1/users/{other_id}/block', author,
                                                    {'reason': 'e2e'}), 403, 'access_denied')
    expect('студент: назначить себя модератором 403',
           call('PUT', f'/api/v1/users/{author_id}/roles/MODERATOR', author), 403)

    print('\n# Назначение модератора')
    expect('старший модератор назначает модератора',
           call('PUT', f'/api/v1/users/{moderator_id}/roles/MODERATOR', senior), 204)
    refreshed = call('POST', '/api/v1/auth/refresh', body={'refreshToken': moderator_tokens['refreshToken']})
    expect('модератор обновляет токен', refreshed, 200)
    moderator_tokens = refreshed.body
    moderator = moderator_tokens['accessToken']
    expect('модератор видит очередь', call('GET', '/api/v1/moderation/queue', moderator), 200)
    expect('модератор не назначает модераторов', call('PUT', f'/api/v1/users/{other_id}/roles/MODERATOR',
                                                     moderator), 403, 'access_denied')
    expect('модератор не блокирует пользователей', call('POST', f'/api/v1/users/{other_id}/block', moderator,
                                                       {'reason': 'e2e'}), 403, 'access_denied')

    print('\n# Заявка видна только автору и модератору')
    created = call('POST', '/api/v1/submissions', author, {'type': 'NEW_PLACE', 'payload': submission_payload()},
                   {'Idempotency-Key': RUN + '-create'})
    if not expect('автор создаёт заявку', created, 201):
        return
    submission_id = created.body['id']
    path = '/api/v1/submissions/' + submission_id
    expect('автор видит свою заявку', call('GET', path, author), 200)
    expect('другой студент не видит чужую заявку: 404', call('GET', path, other), 404)
    expect('другой студент не меняет чужую заявку: 404',
           call('PATCH', path, other, {'place': {'name': 'Чужое'}},
                {'If-Match': created.headers['ETag'], 'Content-Type': 'application/merge-patch+json'}), 404)
    def listed(token):
        body = call('GET', '/api/v1/submissions', token).body
        return [item.get('id') for item in body['items']] if isinstance(body, dict) and 'items' in body else None
    check('автор видит заявку в своём списке', submission_id in (listed(author) or []))
    check('чужая заявка не попадает в список другого студента', listed(other) == [])
    expect('автор отправляет заявку на модерацию',
           call('POST', path + '/submit', author, headers={'If-Match': created.headers['ETag'],
                                                            'Idempotency-Key': RUN + '-submit'}), 202)
    eventually('заявка доходит до PENDING_REVIEW', lambda: call('GET', path, author).body,
               lambda s: isinstance(s, dict) and s.get('status') == 'PENDING_REVIEW')

    print('\n# Решение по заявке принимает только модератор')
    review_path = '/api/v1/moderation/reviews/' + submission_id
    eventually('заявка появляется в очереди модерации',
               lambda: call('GET', '/api/v1/moderation/queue?limit=100', moderator).body,
               lambda queue: isinstance(queue, list) and any(i.get('submissionId') == submission_id for i in queue))
    review = call('GET', review_path, moderator)
    expect('модератор открывает заявку', review, 200)
    expect('автор не открывает карточку модерации', call('GET', review_path, author), 403)
    claim = {'action': 'CLAIM', 'versionId': review.body['versionId'], 'versionNo': review.body['versionNo']}
    expect('автор не может взять свою заявку в работу',
           call('POST', review_path + '/commands', author, claim,
                {'If-Match': review.headers['ETag'], 'Idempotency-Key': RUN + '-author-claim'}), 403)
    expect('модератор берёт заявку в работу',
           call('POST', review_path + '/commands', moderator, claim,
                {'If-Match': review.headers['ETag'], 'Idempotency-Key': RUN + '-claim'}), 202)
    eventually('заявка в работе у модератора', lambda: call('GET', review_path, moderator).body,
               lambda r: r.get('status') == 'IN_REVIEW' and r.get('pendingCommandId') is None)
    review = call('GET', review_path, moderator)
    approve = {'action': 'APPROVE', 'versionId': review.body['versionId'], 'versionNo': review.body['versionNo']}
    expect('модератор одобряет заявку',
           call('POST', review_path + '/commands', moderator, approve,
                {'If-Match': review.headers['ETag'], 'Idempotency-Key': RUN + '-approve'}), 202)
    eventually('автор видит APPROVED', lambda: call('GET', path, author).body,
               lambda s: isinstance(s, dict) and s.get('status') == 'APPROVED')

    print('\n# Снятие роли и блокировка')
    expect('старший модератор снимает роль модератора',
           call('DELETE', f'/api/v1/users/{moderator_id}/roles/MODERATOR', senior), 204)
    check('выданный access-токен действует до своего срока (контракт, раздел 6)',
          call('GET', '/api/v1/moderation/queue', moderator).status == 200)
    refreshed = call('POST', '/api/v1/auth/refresh', body={'refreshToken': moderator_tokens['refreshToken']})
    expect('бывший модератор обновляет токен', refreshed, 200)
    expect('новый токен без роли: очередь 403',
           call('GET', '/api/v1/moderation/queue', refreshed.body['accessToken']), 403)
    expect('старший модератор блокирует студента',
           call('POST', f'/api/v1/users/{other_id}/block', senior, {'reason': 'e2e ' + RUN}), 204)
    expect('блокировка сразу завершает сессии: refresh-токен недействителен',
           call('POST', '/api/v1/auth/refresh', body={'refreshToken': other_tokens['refreshToken']}), 401,
           'invalid_refresh_token')
    expect('выданный до блокировки access-токен действует до своего срока',
           call('GET', '/api/v1/me', other), 200)
    expect('заблокированный не входит', limited('POST', '/api/v1/auth/login',
                                               {'email': f'e2e-{RUN}-other@example.com', 'password': PASSWORD}),
           403, 'account_blocked')

    print('\n# Заголовки через шлюз')
    services = [('auth', '/api/v1/me'), ('submission', '/api/v1/submissions'),
                ('moderation', '/api/v1/moderation/queue')]
    if media_up:
        services.append(('media', '/api/v1/media-assets/' + str(uuid.uuid4())))
    for name, service_path in services:
        response = call('GET', service_path, author, headers={'Origin': 'http://localhost:5173'})
        origins = response.headers.get_all('Access-Control-Allow-Origin') or []
        check(f'{name}: ровно один Access-Control-Allow-Origin', origins == ['http://localhost:5173'], str(origins))
        check(f'{name}: X-Correlation-ID клиента возвращается', response.headers.get('X-Correlation-ID') == 'e2e-' + RUN,
              str(response.headers.get('X-Correlation-ID')))

    print('\n# Публичные сервисы')
    if catalog_up:
        expect('каталог без токена: неизвестная карточка 404, а не 401',
               call('GET', '/api/v1/catalog/places/' + str(uuid.uuid4())), 404)
    else:
        skip('каталог без токена', 'catalog не запущен')
    if search_up:
        response = call('GET', '/api/v1/search/places')
        check('поиск без токена не требует входа', response.status not in (401, 403), f'получено {response.status}')
    else:
        skip('поиск без токена', 'search не запущен')


if __name__ == '__main__':
    main()
    print(f'\nпровалено: {len(failures)}, пропущено: {len(skipped)}')
    sys.exit(1 if failures else 0)
