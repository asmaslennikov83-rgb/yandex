import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import httpx
from core import APIError, Availability, Yandex, action, is_light, validate_result
from bot import HomeBot, Store, authorized, key


def lamp():
    return {'id':'light-1', 'name':'Лампа', 'type':'devices.types.light', 'capabilities':[
        {'type':'devices.capabilities.on_off'},
        {'type':'devices.capabilities.range','parameters':{'instance':'brightness','random_access':True,
            'range':{'min':10,'max':90,'precision':5}}},
        {'type':'devices.capabilities.color_setting','parameters':{'color_model':'rgb','temperature_k':{'min':2700,'max':6500}}}]}


class CoreTests(unittest.TestCase):
    def test_capabilities(self):
        d=lamp()
        self.assertIs(action(d,'on','0')['state']['value'],False)
        self.assertEqual(action(d,'brightness','52')['state']['value'],50)
        for kind,value in [('brightness','100'),('brightness','nan'),('temperature_k','1000'),('rgb','-1'),('on','2')]:
            with self.assertRaises(APIError): action(d,kind,value)
        d['capabilities']=[]
        with self.assertRaises(APIError): action(d,'on','1')

    def test_action_results(self):
        sent=action(lamp(),'on','1')
        def response(status):
            return {'devices':[{'id':'light-1','capabilities':[{'type':sent['type'],
                'state':{'instance':'on','action_result':{'status':status,'error_code':'DEVICE_UNREACHABLE'}}}]}]}
        validate_result(response('DONE'),'light-1',sent)
        for body in (response('ERROR'),{'devices':[]}):
            with self.assertRaises(APIError): validate_result(body,'light-1',sent)

    def test_availability_no_spam(self):
        a=Availability()
        self.assertIsNone(a.observe('x','online'))
        self.assertIsNone(a.observe('x','offline'))
        self.assertIsNone(a.observe('x',None))
        self.assertEqual(a.observe('x','offline'),'offline')
        self.assertIsNone(a.observe('x','offline'))
        self.assertEqual(a.observe('x','online'),'online')
        self.assertIsNone(a.observe('x','online'))
        self.assertIsNone(a.observe('y','offline'))
        self.assertEqual(a.observe('y','offline'),'offline')

    def test_access(self):
        for uid, chat, ok in [(1,'private',True),(2,'private',False),(1,'group',False)]:
            u=SimpleNamespace(effective_user=SimpleNamespace(id=uid),effective_chat=SimpleNamespace(type=chat))
            self.assertEqual(authorized(u,{1}),ok)
        self.assertFalse(is_light({'id':'socket','type':'devices.types.socket'}))
        self.assertTrue(is_light({'id':'socket','type':'devices.types.socket'},{'socket'}))

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'bot.sqlite3'
            s=Store(p); s.join(1); s.join(2); s.toggle(2); s.favorite(1,'x'); s.db.close()
            s=Store(p)
            self.assertEqual(s.favorites(1),{'x'})
            self.assertEqual(s.recipients({1,2}),[1])
            self.assertEqual(s.recipients({2}),[])
            s.db.close()


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_and_bearer(self):
        def handler(request):
            self.assertEqual(request.headers['Authorization'],'Bearer fake')
            import json
            data=json.loads(request.content)
            self.assertEqual(data['devices'][0]['actions'][0]['state'],{'instance':'on','value':True})
            return httpx.Response(200,json={'status':'ok','devices':[{'id':'light-1','capabilities':[
                {'type':'devices.capabilities.on_off','state':{'instance':'on','action_result':{'status':'DONE'}}}]}]})
        async with httpx.AsyncClient(base_url='https://api.iot.yandex.net/v1.0/',headers={'Authorization':'Bearer fake'},
            transport=httpx.MockTransport(handler)) as client:
            await Yandex('fake',client).set(lamp(),'on','1')

    async def test_post_timeout_not_retried(self):
        calls=[]
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('timeout',request=request)
        async with httpx.AsyncClient(base_url='https://example.com/', transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(APIError): await Yandex('fake',client).set(lamp(),'on','1')
        self.assertEqual(len(calls),1)

    async def test_http_errors(self):
        for status in (401,403,429,500):
            async with httpx.AsyncClient(base_url='https://example.com/',transport=httpx.MockTransport(
                lambda r: httpx.Response(status,json={}))) as client:
                with self.assertRaises(APIError): await Yandex('fake',client).set(lamp(),'on','1')

    async def test_unauthorized_callback_never_calls_api(self):
        class ForbiddenAPI:
            async def info(self): raise AssertionError('API must not be used')
        class Query:
            async def answer(self,*args,**kwargs): pass
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/'db')
            b=HomeBot(ForbiddenAPI(),{1},store)
            u=SimpleNamespace(effective_user=SimpleNamespace(id=2), effective_chat=SimpleNamespace(type='private'),callback_query=Query())
            await b.callback(u,None)
            store.db.close()

    async def test_large_menu_and_keyboard(self):
        class FakeAPI:
            async def device(self,did): return lamp()
        class Capture(HomeBot):
            async def show(self,update,text,rows):
                self.rows=rows
        with tempfile.TemporaryDirectory() as td:
            s=Store(Path(td)/'db'); s.join(1)
            b=Capture(FakeAPI(),{1},s)
            b.homes={'h1':'Дом'}
            b.devices={str(i):dict(lamp(),id=str(i),household_id='h1') for i in range(25)}
            u=SimpleNamespace(effective_user=SimpleNamespace(id=1))
            await b.lights(u,'all',0)
            self.assertEqual(len(b.rows),12)
            b.devices={key('light-1'):dict(lamp(),household_id='h1')}
            await b.panel(u,key('light-1'))
            for row in b.rows:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()),64)
            s.db.close()

if __name__=='__main__': unittest.main()
