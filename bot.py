import asyncio
import contextlib
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import time
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from core import APIError, Availability, Yandex, capability, is_light

log = logging.getLogger(__name__)


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def authorized(update, allowed):
    return bool(update.effective_user and update.effective_user.id in allowed
                and update.effective_chat and update.effective_chat.type == 'private')


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
        self.db.execute('CREATE TABLE IF NOT EXISTS favorites (uid INTEGER, device TEXT, PRIMARY KEY(uid,device))')
        self.db.execute('CREATE TABLE IF NOT EXISTS selected_homes (uid INTEGER PRIMARY KEY, home TEXT NOT NULL)')
        self.db.commit()

    def join(self, uid):
        self.db.execute('INSERT OR IGNORE INTO users(id) VALUES (?)', (uid,))
        self.db.commit()

    def enabled(self, uid):
        row = self.db.execute('SELECT alerts FROM users WHERE id=?', (uid,)).fetchone()
        return bool(row and row[0])

    def toggle(self, uid):
        self.db.execute('UPDATE users SET alerts=1-alerts WHERE id=?', (uid,))
        self.db.commit()

    def favorites(self, uid):
        return {x[0] for x in self.db.execute('SELECT device FROM favorites WHERE uid=?', (uid,))}

    def favorite(self, uid, did):
        if did in self.favorites(uid):
            self.db.execute('DELETE FROM favorites WHERE uid=? AND device=?', (uid, did))
        else:
            self.db.execute('INSERT INTO favorites VALUES (?,?)', (uid, did))
        self.db.commit()

    def recipients(self, allowed):
        return [r[0] for r in self.db.execute('SELECT id FROM users WHERE alerts=1') if r[0] in allowed]

    def home(self, uid):
        row = self.db.execute('SELECT home FROM selected_homes WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else None

    def select_home(self, uid, home):
        self.db.execute('INSERT OR REPLACE INTO selected_homes VALUES (?,?)', (uid, home))
        self.db.commit()


class HomeBot:
    def __init__(self, api, allowed, store, extra=(), interval=120):
        self.api, self.allowed, self.store = api, allowed, store
        self.extra, self.interval = extra, interval
        self.devices, self.rooms = {}, {}
        self.homes, self.room_homes = {}, {}
        self.refresh_lock = asyncio.Lock()
        self.refreshed = 0
        self.availability = Availability()
        self.monitor_task = None
        self.api_failed = False

    async def refresh(self, force=False):
        async with self.refresh_lock:
            if not force and time.monotonic() - self.refreshed < 30:
                return
            info = await self.api.info()
            self.devices = {key(d['id']): d for d in info.get('devices', []) if is_light(d, self.extra)}
            homes = {h['id']: h['name'] for h in info.get('households', [])}
            self.room_homes = {r['id']: r.get('household_id') for r in info.get('rooms', [])}
            # Normalize membership once from the full inventory. Device detail
            # responses may omit household_id, so preserve inventory metadata.
            for d in self.devices.values():
                d['household_id'] = d.get('household_id') or self.room_homes.get(d.get('room')) or '__unassigned__'
                homes.setdefault(d['household_id'], 'Без привязки к дому' if d['household_id'] == '__unassigned__' else 'Дом')
            self.homes = homes
            self.rooms = {key(r['id']): (r['id'], homes.get(r.get('household_id'), 'Дом') + ' / ' + r['name'])
                          for r in info.get('rooms', [])}
            self.refreshed = time.monotonic()

    async def guard(self, update):
        if authorized(update, self.allowed):
            self.store.join(update.effective_user.id)
            return True
        if update.callback_query:
            await update.callback_query.answer('Доступ закрыт.', show_alert=True)
        elif update.effective_chat and update.effective_chat.type == 'private':
            await update.effective_message.reply_text(f'Доступ закрыт. Ваш Telegram ID: {update.effective_user.id}')
        return False

    async def show(self, update, text, rows):
        markup = Keyboard(rows)
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text[:4000], reply_markup=markup)
            except BadRequest as e:
                if 'message is not modified' not in str(e).lower():
                    raise
        else:
            await update.effective_message.reply_text(text[:4000], reply_markup=markup)

    async def menu(self, update, context):
        if not await self.guard(update):
            return
        try:
            await self.refresh()
        except APIError as e:
            return await self.show(update, '⚠️ ' + str(e), [[Button('Повторить', callback_data='home')]])
        home = self.selected_home(update.effective_user.id)
        if home is None:
            return await self.homes_page(update, 0)
        await self.show(update, '🏠 ' + self.homes[home][:180] + '\nУправление светом. Выберите раздел.', [
            [Button('🏘 Выбрать дом', callback_data='homes:0')],
            [Button('💡 Весь свет', callback_data='list:all:0'), Button('🏠 Комнаты', callback_data='rooms:0')],
            [Button('⭐ Избранное', callback_data='list:fav:0')],
            [Button('🔔 Уведомления', callback_data='alerts')],
            [Button('🔄 Обновить список', callback_data='refresh')]])

    def selected_home(self, uid):
        home = self.store.home(uid)
        if home in self.homes:
            return home
        if len(self.homes) == 1:
            home = next(iter(self.homes))
            self.store.select_home(uid, home)
            return home
        return None

    def in_selected_home(self, uid, device):
        home = self.selected_home(uid)
        return home is not None and device.get('household_id') == home

    async def homes_page(self, update, page):
        items = sorted(self.homes.items(), key=lambda x: (x[1], x[0]))
        rows = [[Button(name[:60], callback_data='choosehome:' + key(hid))]
                for hid, name in items[page*10:(page+1)*10]]
        rows += self.pages('homes', page, len(items))
        rows.append([Button('🔄 Обновить', callback_data='refreshhomes')])
        await self.show(update, '🏘 Выберите дом' if items else 'Дома не найдены в аккаунте Яндекса.', rows)

    async def rooms_page(self, update, page):
        home = self.selected_home(update.effective_user.id)
        items = [('none', (None, 'Без комнаты'))] + sorted(
            ((k, value) for k, value in self.rooms.items() if self.room_homes.get(value[0]) == home), key=lambda x: x[1][1])
        rows = [[Button(name[:60], callback_data=f'list:{k}:0')] for k, (_, name) in items[page*10:(page+1)*10]]
        rows += self.pages('rooms', page, len(items))
        rows.append([Button('⬅️ Главное меню', callback_data='home')])
        await self.show(update, '🏠 Комнаты', rows)

    @staticmethod
    def pages(prefix, page, count):
        row = []
        if page:
            row.append(Button('⬅️', callback_data=f'{prefix}:{page-1}'))
        if (page+1)*10 < count:
            row.append(Button('➡️', callback_data=f'{prefix}:{page+1}'))
        return [row] if row else []

    async def lights(self, update, scope, page):
        if scope not in ('all', 'fav', 'none') and scope not in self.rooms:
            raise APIError('Комната удалена. Обновите список.')
        favorites = self.store.favorites(update.effective_user.id)
        room = self.rooms.get(scope, (None, 'Без комнаты'))[0]
        items = sorted(((k, d) for k, d in self.devices.items()
            if self.in_selected_home(update.effective_user.id, d) and
            (scope == 'all' or (scope == 'fav' and d['id'] in favorites)
            or (scope not in ('all', 'fav') and d.get('room') == room))), key=lambda x: x[1]['name'])
        rows = [[Button(d['name'][:60], callback_data='device:'+k)] for k,d in items[page*10:(page+1)*10]]
        rows += self.pages('list:'+scope, page, len(items))
        rows.append([Button('⬅️ Главное меню', callback_data='home')])
        await self.show(update, f'💡 Свет — {len(items)} устройств\nВыберите устройство.' if items else
            'Свет не найден. Проверьте устройства в «Доме с Алисой».\nДля выключателей света предусмотрена настройка EXTRA_LIGHT_IDS.', rows)

    async def panel(self, update, k, prefix=''):
        if not self.in_selected_home(update.effective_user.id, self.devices[k]):
            raise APIError('Эта кнопка относится к другому дому. Откройте меню выбранного дома.')
        d = {**self.devices[k], **await self.api.device(self.devices[k]['id'])}
        if not self.in_selected_home(update.effective_user.id, d):
            raise APIError('Устройство перемещено в другой дом. Обновите список.')
        if not is_light(d, self.extra):
            raise APIError('Устройство больше не относится к свету. Обновите список.')
        self.devices[k] = d
        c = capability(d, 'on_off')
        value = ((c or {}).get('state') or {}).get('value')
        state = 'включён' if value is True else 'выключен' if value is False else 'неизвестно'
        text = f'{prefix}🏠 {self.homes.get(d.get("household_id"), "Дом")[:180]}\n💡 {d["name"][:180]}\nСвязь: {d.get("state", "неизвестно")}\nСвет: {state}'
        rows = []
        if c:
            rows.append([Button('Включить', callback_data=f'set:{k}:on:1'), Button('Выключить', callback_data=f'set:{k}:on:0')])
        c = capability(d, 'range', 'brightness')
        if c:
            p = c.get('parameters') or {}
            text += '\nЯркость: ' + str((c.get('state') or {}).get('value', 'неизвестно'))
            if p.get('random_access', True):
                r = p.get('range') or {'min':1,'max':100}
                values = sorted({r['min'], r['max'], *[v for v in (25,50,75) if r['min'] <= v <= r['max']]})
                rows.append([Button(f'{v}%', callback_data=f'set:{k}:brightness:{v}') for v in values])
        c = capability(d, 'color_setting')
        p = (c or {}).get('parameters') or {}
        r = p.get('temperature_k')
        if r:
            vals = sorted({r['min'], r['max'], (r['min']+r['max'])//2})
            rows.append([Button(f'{v} K', callback_data=f'set:{k}:temperature_k:{v}') for v in vals])
        if p.get('color_model') == 'rgb':
            rows.append([Button(label, callback_data=f'set:{k}:rgb:{val}') for label,val in [('🔴',16711680),('🟢',65280),('🔵',255),('🟡',16776960)]])
        favorite = d['id'] in self.store.favorites(update.effective_user.id)
        rows += [[Button('⭐ Убрать из избранного' if favorite else '⭐ В избранное', callback_data='fav:'+k)],
                 [Button('🔄 Состояние', callback_data='device:'+k), Button('⬅️ Меню', callback_data='home')]]
        await self.show(update, text, rows)

    async def callback(self, update, context):
        if not await self.guard(update):
            return
        await update.callback_query.answer()
        try:
            parts = update.callback_query.data.split(':')
            cmd = parts[0]
            if cmd == 'home':
                return await self.menu(update, context)
            if cmd == 'alerts':
                enabled = self.store.enabled(update.effective_user.id)
                return await self.show(update, '🔔 Уведомления ' + ('включены' if enabled else 'выключены') +
                    '\nПроверяются все дома аккаунта; в сообщении указано название дома.\nПотеря связи со светом, восстановление, сбой API. Проверка каждые ' + str(self.interval) +
                    ' сек. Потеря связи — после двух проверок подряд.',
                    [[Button('Переключить', callback_data='togglealerts')], [Button('⬅️ Меню', callback_data='home')]])
            if cmd == 'togglealerts':
                self.store.toggle(update.effective_user.id)
                return await self.show(update, 'Уведомления ' + ('включены.' if self.store.enabled(update.effective_user.id) else 'выключены.'),
                    [[Button('⬅️ Меню', callback_data='home')]])
            await self.refresh(force=cmd in ('refresh', 'refreshhomes'))
            if cmd in ('homes', 'refreshhomes'):
                return await self.homes_page(update, max(0, int(parts[1])) if cmd == 'homes' else 0)
            if cmd == 'choosehome':
                home = next((hid for hid in self.homes if key(hid) == parts[1]), None)
                if home is None:
                    raise APIError('Дом удалён. Обновите список домов.')
                self.store.select_home(update.effective_user.id, home)
                return await self.menu(update, context)
            if self.selected_home(update.effective_user.id) is None:
                return await self.homes_page(update, 0)
            if cmd == 'refresh':
                count = sum(self.in_selected_home(update.effective_user.id, d) for d in self.devices.values())
                return await self.show(update, f'Список обновлён. Устройств света в выбранном доме: {count}', [[Button('💡 Свет', callback_data='list:all:0')], [Button('⬅️ Меню', callback_data='home')]])
            if cmd == 'rooms':
                return await self.rooms_page(update, max(0, int(parts[1])))
            if cmd == 'list':
                return await self.lights(update, parts[1], max(0, int(parts[2])))
            k = parts[1]
            if k not in self.devices:
                raise APIError('Устройство удалено или кнопка устарела. Обновите список.')
            if not self.in_selected_home(update.effective_user.id, self.devices[k]):
                raise APIError('Эта кнопка относится к другому дому. Откройте меню выбранного дома.')
            if cmd == 'fav':
                self.store.favorite(update.effective_user.id, self.devices[k]['id'])
            if cmd == 'set':
                d = {**self.devices[k], **await self.api.device(self.devices[k]['id'])}
                if not self.in_selected_home(update.effective_user.id, d):
                    raise APIError('Устройство перемещено в другой дом. Обновите список.')
                if not is_light(d, self.extra):
                    raise APIError('Устройство больше не относится к свету.')
                await self.api.set(d, parts[2], parts[3])
                log.info('Command user=%s device=%s kind=%s', update.effective_user.id, k, parts[2])
                try:
                    return await self.panel(update, k, '✅ Яндекс подтвердил выполнение.\n')
                except APIError:
                    return await self.show(update, '✅ Яндекс подтвердил выполнение. Свежее состояние сейчас получить не удалось.',
                        [[Button('🔄 Проверить', callback_data='device:'+k)]])
            return await self.panel(update, k)
        except (APIError, ValueError, IndexError) as e:
            text = str(e) if isinstance(e, APIError) else 'Кнопка устарела. Откройте /start.'
            await self.show(update, '⚠️ ' + text, [[Button('⬅️ Меню', callback_data='home')]])

    async def notify(self, app, text):
        for uid in self.store.recipients(self.allowed):
            try:
                await app.bot.send_message(uid, text[:4000])
            except TelegramError:
                log.warning('Notification delivery failed user=%s', uid)

    async def monitor(self, app):
        while True:
            try:
                await self.refresh(force=True)
                events = []
                samples = []
                for k,d in list(self.devices.items()):
                    fresh = await self.api.device(d['id'])
                    samples.append((d, fresh.get('state')))
                for d, state in samples:
                    event = self.availability.observe(d['id'], state)
                    if event:
                        name = self.homes.get(d.get('household_id'), 'Дом')[:120] + ' / ' + d['name'][:120]
                        events.append(('🔴 Нет связи: ' if event == 'offline' else '🟢 Связь восстановлена: ') + name)
                if self.api_failed:
                    await self.notify(app, '🟢 Доступ к API Яндекса восстановлен.')
                    self.api_failed = False
                if events:
                    for start in range(0, len(events), 10):
                        await self.notify(app, '\n'.join(events[start:start+10]))
            except APIError as e:
                if not self.api_failed:
                    await self.notify(app, '⚠️ Не удалось проверить умный дом.\n' + str(e))
                self.api_failed = True
            except Exception as e:
                log.error('Monitor failure: %s', type(e).__name__)
            await asyncio.sleep(self.interval)

    async def init(self, app):
        await app.bot.set_my_commands([('start','Управление светом')])
        self.monitor_task = asyncio.create_task(self.monitor(app))

    async def stop(self, app):
        if self.monitor_task:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task

    async def shutdown(self, app):
        await self.api.client.aclose()
        self.store.db.close()


async def error_handler(update, context):
    # Exception strings / HTTP debug logs can contain Telegram bot tokens.
    log.error('Handler failure: %s', type(context.error).__name__)
    if update and update.effective_chat and update.effective_chat.type == 'private':
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text('Ошибка обработки. Откройте /start и попробуйте ещё раз.')


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try:
        token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
        ytoken = os.environ['YANDEX_OAUTH_TOKEN'].strip()
        allowed = {int(x.strip()) for x in os.environ['ALLOWED_USER_IDS'].split(',') if x.strip()}
        interval = int(os.getenv('POLL_SECONDS', '120'))
        if not token or not ytoken or not allowed or min(allowed) <= 0 or interval < 60:
            raise ValueError()
    except (KeyError, ValueError):
        raise SystemExit('Заполните TELEGRAM_BOT_TOKEN, YANDEX_OAUTH_TOKEN, ALLOWED_USER_IDS. POLL_SECONDS >= 60.')
    extra = {x.strip() for x in os.getenv('EXTRA_LIGHT_IDS','').split(',') if x.strip()}
    service = HomeBot(Yandex(ytoken), allowed, Store(Path(os.getenv('DATA_DIR','data'))/'bot.sqlite3'), extra, interval)
    app = (Application.builder().token(token).post_init(service.init).post_stop(service.stop)
           .post_shutdown(service.shutdown).build())
    app.add_handler(CommandHandler('start', service.menu))
    app.add_handler(CallbackQueryHandler(service.callback))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=True)


if __name__ == '__main__':
    main()
