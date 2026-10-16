import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import test_homes
from bot import Store, key
from core import APIError
from mini_app import authenticate, build_app

TOKEN='12345:test-bot-secret'


def signed(uid=1, when=None, token=TOKEN, **extra):
    fields={'auth_date':str(int(time.time() if when is None else when)),
        'user':json.dumps({'id':uid,'first_name':'Тест'},ensure_ascii=False,separators=(',',':')),
        'query_id':'safe-query', **extra}
    check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthTests(unittest.TestCase):
    def test_valid_and_signature_field(self):
        self.assertEqual(authenticate(signed(signature='extra-signed-value'),TOKEN,{1}),1)

    def test_tampered_wrong_bot_missing_and_duplicate(self):
        for raw in ('',signed().replace('safe-query','forged-query'),signed(token='wrong'),signed()+'&auth_date=1'):
            with self.assertRaises(web.HTTPUnauthorized):authenticate(raw,TOKEN,{1})

    def test_age_future_and_allowlist(self):
        for age in (-120,3601):
            with self.assertRaises(web.HTTPUnauthorized):authenticate(signed(when=time.time()-age),TOKEN,{1})
        with self.assertRaises(web.HTTPForbidden):authenticate(signed(uid=2),TOKEN,{1})


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'db')
        self.api=test_homes.FakeAPI()
        for d in self.api.inventory['devices']:
            d['state']='online'
            d['capabilities']=[{'type':'devices.capabilities.on_off','state':{'instance':'on','value':False}}]
        self.service=test_homes.Capture(self.api,{1,2},self.store)
        await self.service.refresh()
        self.store.join(1); self.store.join(2)
        self.store.select_home(1,'h1');self.store.select_home(2,'h2')
        self.client=TestClient(TestServer(build_app(self.service,TOKEN)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();self.store.db.close();self.tmp.cleanup()

    async def call(self,path,body=None,uid=1,raw=None):
        headers={'X-Telegram-Init-Data':signed(uid) if raw is None else raw}
        if body is None:return await self.client.get('/api/'+path,headers=headers)
        return await self.client.post('/api/'+path,json=body,headers=headers)

    async def test_state_filters_home_and_no_secrets(self):
        r=await self.call('state');self.assertEqual(r.status,200)
        state=await r.json()
        self.assertEqual({d['id'] for d in state['devices']},{key('a'),key('c')})
        self.assertNotIn(TOKEN,json.dumps(state))
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertEqual(state['theme'],'dark')
        r=await self.call('state',uid=2);state=await r.json()
        self.assertEqual({d['id'] for d in state['devices']},{key('b'),key('d')})

    async def test_http_rejects_invalid_auth_and_revoked_user(self):
        r=await self.call('state',raw='invalid');self.assertEqual(r.status,401)
        self.service.allowed.remove(1)
        r=await self.call('state');self.assertEqual(r.status,403)
        self.assertEqual(self.api.writes,[])

    async def test_theme_alerts_and_home_are_personal(self):
        r=await self.call('settings',{'theme':'cozy','alerts':False});self.assertEqual(r.status,200)
        self.assertEqual(self.store.theme(1),'cozy');self.assertEqual(self.store.theme(2),'dark')
        self.assertFalse(self.store.enabled(1));self.assertTrue(self.store.enabled(2))
        r=await self.call('home',{'home':key('h2')});self.assertEqual(r.status,200)
        self.assertEqual(self.store.home(1),'h2')
        other=Store(Path(self.tmp.name)/'db');self.assertEqual(other.theme(1),'cozy');other.db.close()

    async def test_invalid_settings_and_unknown_house(self):
        for data in ({'theme':'neon'},{'theme':[]},{'alerts':'yes'}):
            r=await self.call('settings',data);self.assertEqual(r.status,400)
        r=await self.call('home',{'home':'missing'});self.assertEqual(r.status,404)

    async def test_favorite_is_idempotent_and_shared_with_bot(self):
        body={'home':key('h1'),'device':key('a'),'enabled':True}
        for _ in range(2):
            r=await self.call('favorite',body);self.assertEqual(r.status,200)
        self.assertEqual(self.store.favorites(1),{'a'})
        self.assertEqual(self.store.favorites(2),set())

    async def test_command_and_replay_send_only_once(self):
        body={'home':key('h1'),'device':key('a'),'kind':'on','value':1,'request_id':'unique-request-00001'}
        for _ in range(2):
            r=await self.call('action',body);self.assertEqual(r.status,200)
        self.assertEqual(self.api.writes,['a'])
        body['value']=0
        r=await self.call('action',body);self.assertEqual(r.status,409)
        self.assertEqual(self.api.writes,['a'])

    async def test_wrong_house_and_changed_selection_never_control(self):
        body={'home':key('h1'),'device':key('b'),'kind':'on','value':1,'request_id':'unique-request-00002'}
        r=await self.call('action',body);self.assertEqual(r.status,404)
        body['device']=key('a');self.store.select_home(1,'h2')
        r=await self.call('action',body);self.assertEqual(r.status,409)
        self.assertEqual(self.api.writes,[])

    async def test_failed_command_replay_is_not_sent_again(self):
        calls=[]
        async def timeout(*args):
            calls.append(1);raise APIError('Неизвестный результат: проверьте состояние.')
        self.api.set=timeout
        body={'home':key('h1'),'device':key('a'),'kind':'on','value':1,'request_id':'unique-request-00003'}
        for _ in range(2):
            r=await self.call('action',body);self.assertEqual(r.status,502)
        self.assertEqual(len(calls),1)

    async def test_add_socket_without_command_and_remove(self):
        d=self.api.inventory['devices'][0];d['type']='devices.types.socket'
        await self.service.refresh(force=True)
        body={'home':key('h1'),'device':key('a'),'enabled':True}
        r=await self.call('extra',body);self.assertEqual(r.status,200)
        self.assertIn(key('a'),self.service.devices);self.assertEqual(self.api.writes,[])
        body['enabled']=False
        r=await self.call('extra',body);self.assertEqual(r.status,200)
        self.assertNotIn(key('a'),self.service.devices)

    async def test_static_no_source_or_config_disclosure(self):
        for name in ('','app.js','style.css','art.svg'):
            r=await self.client.get('/'+name);self.assertEqual(r.status,200)
        for name in ('bot.py','.env','core.py','mini_app.py'):
            r=await self.client.get('/'+name);self.assertEqual(r.status,404)
        r=await self.client.get('/health');self.assertEqual(r.status,200)

if __name__=='__main__':unittest.main()
