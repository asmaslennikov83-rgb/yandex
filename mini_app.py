"""Same-origin Telegram Mini App. Secrets and Yandex calls stay on the server."""
import asyncio
from collections import OrderedDict, deque
import hashlib
import hmac
import json
import logging
from pathlib import Path
import time
from urllib.parse import parse_qsl
from aiohttp import web
from core import APIError, capability, is_light

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / 'web'
THEMES = {'light', 'dark', 'cozy'}


def short(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def authenticate(raw, token, allowed, now=None):
    """Telegram HMAC validation, then expiry and server-side allowlist."""
    now = time.time() if now is None else now
    try:
        if not raw or len(raw) > 16384:
            raise ValueError()
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError()
        received = data.pop('hash')
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received, expected):
            raise ValueError()
        age = now - int(data['auth_date'])
        if age < -30 or age > 3600:
            raise ValueError()
        user = json.loads(data['user'])
        uid = user['id']
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise ValueError()
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise web.HTTPUnauthorized(text='Откройте панель заново кнопкой в Telegram.') from None
    if uid not in allowed:
        raise web.HTTPForbidden(text='Доступ закрыт: Telegram ID отсутствует в белом списке.')
    return uid


def device_view(service, device, favorites):
    def value(kind, instance=None):
        c = capability(device, kind, instance)
        return ((c or {}).get('state') or {}).get('value')
    brightness = capability(device, 'range', 'brightness')
    bp = (brightness or {}).get('parameters') or {}
    cp = (capability(device, 'color_setting') or {}).get('parameters') or {}
    return {'id': short(device['id']), 'name': device.get('name', 'Свет'),
        'room': short(device.get('room') or '__none__'),
        'type': device.get('type', ''), 'on': value('on_off'),
        'reachable': device.get('state'), 'favorite': device['id'] in favorites,
        'can_switch': capability(device, 'on_off') is not None,
        'brightness': value('range', 'brightness'),
        'brightness_range': (bp.get('range') or {'min': 1, 'max': 100, 'precision': 1})
            if brightness and bp.get('random_access', True) else None,
        'temperature_range': cp.get('temperature_k'), 'rgb': cp.get('color_model') == 'rgb'}


class MiniApp:
    def __init__(self, service, token):
        self.service, self.token = service, token
        self.locks = {}
        self.rates = {}
        self.results = OrderedDict()

    def throttle(self, uid):
        now = time.monotonic()
        q = self.rates.setdefault(uid, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= 90:
            raise web.HTTPTooManyRequests(text='Слишком много запросов. Подождите минуту.')
        q.append(now)

    def state(self, uid):
        s = self.service
        home = s.selected_home(uid)
        favorites = s.store.favorites(uid)
        return {'theme': s.store.theme(uid), 'alerts': s.store.enabled(uid),
            'home': short(home) if home else None,
            'homes': [{'id': short(hid), 'name': name,
                'count': sum(d.get('household_id') == hid for d in s.devices.values())} for hid, name in s.homes.items()],
            'rooms': [{'id': short(rid), 'name': name.split(' / ', 1)[-1]}
                for rid, name in s.rooms.values() if s.room_homes.get(rid) == home] +
                [{'id': short('__none__'), 'name': 'Без комнаты'}],
            'devices': [device_view(s, d, favorites) for d in s.devices.values()
                if s.in_selected_home(uid, d)],
            'candidates': [{'id': short(d['id']), 'name': d.get('name', 'Устройство'),
                'added': d['id'] in s.extra, 'locked': d['id'] in s.env_extra}
                for d in s.inventory if s.light_candidate(d) and s.in_selected_home(uid, d)]}

    def get_device(self, uid, device_key, home_key):
        s = self.service
        if not isinstance(device_key, str) or not isinstance(home_key, str):
            raise web.HTTPBadRequest(text='Некорректный идентификатор устройства или дома.')
        home = s.selected_home(uid)
        if not home or short(home) != home_key:
            raise web.HTTPConflict(text='Дом изменился. Обновите экран и повторите действие.')
        d = s.devices.get(device_key)
        if not d or not s.in_selected_home(uid, d):
            raise web.HTTPNotFound(text='Устройство не найдено в выбранном доме.')
        return d

    async def handle(self, request):
        uid = authenticate(request.headers.get('X-Telegram-Init-Data', ''), self.token, self.service.allowed)
        self.throttle(uid)
        s = self.service
        async with self.locks.setdefault(uid, asyncio.Lock()):
            s.store.join(uid)
            await s.refresh()
            if request.method == 'GET' and request.path == '/api/state':
                if request.query.get('refresh') == '1' and time.monotonic() - s.refreshed > 2:
                    await s.refresh(force=True)
                return web.json_response(self.state(uid))
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text='Некорректный JSON.') from None
            op = request.path.removeprefix('/api/')
            if op == 'settings':
                if 'theme' in body:
                    if not isinstance(body['theme'], str) or body['theme'] not in THEMES:
                        raise web.HTTPBadRequest(text='Неизвестная тема.')
                    s.store.set_theme(uid, body['theme'])
                if 'alerts' in body:
                    if type(body['alerts']) is not bool:
                        raise web.HTTPBadRequest(text='Некорректная настройка уведомлений.')
                    s.store.set_alerts(uid, body['alerts'])
            elif op == 'home':
                hid = next((h for h in s.homes if short(h) == body.get('home')), None)
                if not hid:
                    raise web.HTTPNotFound(text='Дом не найден. Обновите список.')
                s.store.select_home(uid, hid)
            elif op == 'favorite':
                d = self.get_device(uid, body.get('device'), body.get('home'))
                if type(body.get('enabled')) is not bool:
                    raise web.HTTPBadRequest(text='Некорректная команда.')
                if (d['id'] in s.store.favorites(uid)) != body['enabled']:
                    s.store.favorite(uid, d['id'])
            elif op == 'extra':
                home = s.selected_home(uid)
                if not home or short(home) != body.get('home'):
                    raise web.HTTPConflict(text='Выбранный дом изменился.')
                d = next((d for d in s.inventory if short(d['id']) == body.get('device')), None)
                if not d or not s.in_selected_home(uid, d) or not s.light_candidate(d):
                    raise web.HTTPNotFound(text='Розетка или выключатель не найдены в выбранном доме.')
                if d['id'] in s.env_extra:
                    raise web.HTTPConflict(text='Измените EXTRA_LIGHT_IDS на хостинге.')
                if type(body.get('enabled')) is not bool:
                    raise web.HTTPBadRequest(text='Некорректная команда.')
                s.store.set_extra_light(d['id'], body['enabled'])
                s.rebuild_lights()
            elif op == 'action':
                return await self.command(uid, body)
            else:
                raise web.HTTPNotFound()
            return web.json_response(self.state(uid))

    async def command(self, uid, body):
        s = self.service
        request_id = body.get('request_id', '')
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 80:
            raise web.HTTPBadRequest(text='Отсутствует идентификатор команды.')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cache_key = (uid, request_id)
        cached = self.results.get(cache_key)
        if cached:
            if cached['fingerprint'] != fingerprint:
                raise web.HTTPConflict(text='Идентификатор уже использован для другой команды.')
            return web.json_response(cached['result'], status=cached['status'])
        d = self.get_device(uid, body.get('device'), body.get('home'))
        kind = body.get('kind')
        value = body.get('value')
        if kind not in ('on', 'brightness', 'temperature_k', 'rgb') or not isinstance(value, (int, float, str, bool)):
            raise web.HTTPBadRequest(text='Некорректная команда.')
        try:
            fresh = {**d, **await s.api.device(d['id'])}
            self.get_device(uid, body.get('device'), body.get('home'))
            if not s.in_selected_home(uid, fresh) or not is_light(fresh, s.extra):
                raise APIError('Устройство изменилось. Обновите список.')
            await s.api.set(fresh, kind, value)
            log.info('Mini App command user=%s device=%s kind=%s', uid, body['device'], kind)
            note = 'Яндекс подтвердил выполнение.'
            try:
                fresh = {**fresh, **await s.api.device(d['id'])}
                s.devices[body['device']] = fresh
                # Keep refreshed states for the next state response too.
                s.inventory = [fresh if x['id'] == d['id'] else x for x in s.inventory]
            except APIError:
                note += ' Не удалось обновить состояние; проверьте его кнопкой обновления.'
            result, status = {'state': self.state(uid), 'message': note}, 200
        except (APIError, ValueError, TypeError, OverflowError) as e:
            result, status = {'error': str(e) if isinstance(e, APIError) else 'Некорректное значение команды.'}, 502
        self.results[cache_key] = {'fingerprint': fingerprint, 'result': result, 'status': status}
        while len(self.results) > 500:
            self.results.popitem(last=False)
        return web.json_response(result, status=status)


def build_app(service, token):
    mini = MiniApp(service, token)

    @web.middleware
    async def middleware(request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = web.json_response({'error': e.text}, status=e.status)
        except APIError as e:
            response = web.json_response({'error': str(e)}, status=502)
        except Exception as e:
            log.error('Mini App failure: %s', type(e).__name__)
            response = web.json_response({'error': 'Ошибка сервера. Обновите экран.'}, status=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org")
        return response

    app = web.Application(middlewares=[middleware], client_max_size=32768)
    app.router.add_get('/api/state', mini.handle)
    for path in ('home', 'settings', 'favorite', 'extra', 'action'):
        app.router.add_post('/api/' + path, mini.handle)

    async def index(request):
        return web.FileResponse(WEB / 'index.html')

    async def asset(request):
        name = request.match_info['name']
        if name not in ('app.js', 'style.css', 'art.svg'):
            raise web.HTTPNotFound()
        return web.FileResponse(WEB / name)

    async def health(request):
        return web.json_response({'ok': True})

    app.router.add_get('/', index)
    app.router.add_get('/health', health)
    app.router.add_get('/{name}', asset)
    return app


async def start_web(service, token, port):
    runner = web.AppRunner(build_app(service, token), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, '0.0.0.0', port).start()
    except Exception:
        await runner.cleanup()
        raise
    log.info('Mini App server listening on port %s', port)
    return runner
