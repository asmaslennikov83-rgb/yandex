"""Yandex API and light capability validation. No Telegram dependency."""
import asyncio
import logging
import math
import time
from urllib.parse import quote
import httpx

CAP = 'devices.capabilities.'
log = logging.getLogger(__name__)

class APIError(Exception):
    pass


def is_light(device, extra=()):
    kind = device.get('type', '')
    return kind == 'devices.types.light' or kind.startswith('devices.types.light.') or device.get('id') in extra


def capability(device, kind, instance=None):
    for c in device.get('capabilities', []):
        if c.get('type') == CAP + kind:
            p = c.get('parameters') or {}
            if instance is None or p.get('instance') == instance:
                return c
    return None


def action(device, kind, value):
    instance = kind
    if kind == 'on':
        c = capability(device, 'on_off')
        value = bool(int(value)) if str(value) in ('0', '1') else None
        if value is None:
            raise APIError('Некорректная команда.')
        typ = 'on_off'
    elif kind == 'brightness':
        c = capability(device, 'range', kind)
        p = (c or {}).get('parameters') or {}
        if not p.get('random_access', True):
            raise APIError('Устройство не поддерживает точную установку яркости.')
        r = p.get('range') or {'min': 1, 'max': 100, 'precision': 1}
        value = float(value)
        lo, hi, step = r.get('min', 1), r.get('max', 100), r.get('precision', 1)
        if not math.isfinite(value) or not lo <= value <= hi:
            raise APIError('Яркость вне диапазона устройства.')
        value = min(hi, lo + round((value - lo) / step) * step) if step > 0 else value
        typ = 'range'
    elif kind in ('rgb', 'temperature_k'):
        c = capability(device, 'color_setting')
        p = (c or {}).get('parameters') or {}
        value = int(value)
        if kind == 'rgb' and (p.get('color_model') != 'rgb' or not 0 <= value <= 0xFFFFFF):
            raise APIError('RGB-цвет не поддерживается.')
        if kind == 'temperature_k':
            r = p.get(kind) or {}
            if not r or not r.get('min', 0) <= value <= r.get('max', 0):
                raise APIError('Температура цвета вне диапазона устройства.')
        typ = 'color_setting'
    else:
        raise APIError('Неизвестная команда.')
    if not c:
        raise APIError('Устройство не поддерживает эту функцию.')
    return {'type': CAP + typ, 'state': {'instance': instance, 'value': value}}


def validate_result(data, device_id, sent):
    for d in data.get('devices', []):
        if d.get('id') != device_id:
            continue
        for c in d.get('capabilities', []):
            s = c.get('state') or {}
            if c.get('type') == sent['type'] and s.get('instance') == sent['state']['instance']:
                result = s.get('action_result') or {}
                if result.get('status') == 'DONE':
                    return
                raise APIError('Яндекс не выполнил команду: ' + str(result.get('error_code', 'ERROR')))
    raise APIError('Нет подтверждения выполнения команды от Яндекса.')


class Yandex:
    def __init__(self, token, client=None):
        self.client = client or httpx.AsyncClient(base_url='https://api.iot.yandex.net/v1.0/',
            headers={'Authorization': 'Bearer ' + token}, timeout=20)
        self.lock = asyncio.Lock()
        self.next_request = 0

    async def request(self, method, path, body=None):
        # One request at a time; GET retries only. Never repeat an uncertain POST.
        async with self.lock:
            for attempt in range(3):
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + 0.35
                try:
                    r = await self.client.request(method, path, json=body)
                except httpx.RequestError:
                    if method == 'GET' and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise APIError('Нет ответа API. Если отправлялась команда, проверьте состояние перед повтором.') from None
                log.info('Yandex HTTP %s', r.status_code)
                if r.status_code in (401, 403):
                    raise APIError('Проверьте токен Яндекса и права iot:view / iot:control.')
                if r.status_code == 429 or r.status_code >= 500:
                    try:
                        delay = min(60, max(1, float(r.headers.get('Retry-After', 2 ** (attempt + 1)))))
                    except ValueError:
                        delay = 4
                    self.next_request = time.monotonic() + delay
                    if method == 'GET' and attempt < 2:
                        continue
                    raise APIError('API временно недоступен или превышен лимит. Повторите позже.')
                if r.status_code >= 400:
                    raise APIError(f'Ошибка API Яндекса: HTTP {r.status_code}.')
                try:
                    data = r.json()
                except ValueError:
                    raise APIError('Некорректный ответ Яндекса.') from None
                log.info('Yandex request_id=%s', data.get('request_id', '-'))
                if data.get('status') != 'ok':
                    raise APIError('Яндекс вернул ошибку обработки запроса.')
                return data

    async def info(self):
        return await self.request('GET', 'user/info')

    async def device(self, device_id):
        return await self.request('GET', 'devices/' + quote(device_id, safe=''))

    async def set(self, device, kind, value):
        sent = action(device, kind, value)
        data = await self.request('POST', 'devices/actions', {'devices': [{'id': device['id'], 'actions': [sent]}]})
        validate_result(data, device['id'], sent)


class Availability:
    """Two consecutive confirmed offline samples; API errors are not samples."""
    def __init__(self):
        self.states = {}
        self.misses = {}

    def observe(self, key, state):
        if state not in ('online', 'offline'):
            return None
        old = self.states.get(key)
        if state == 'offline':
            self.misses[key] = self.misses.get(key, 0) + 1
            if self.misses[key] < 2:
                return None
        else:
            self.misses[key] = 0
        self.states[key] = state
        if state == 'offline' and old != 'offline':
            return 'offline'
        if state == 'online' and old == 'offline':
            return 'online'
        return None
