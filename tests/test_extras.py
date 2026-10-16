import unittest
import test_homes
from test_homes import update
from bot import Store, key

class ExtraTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_homes.HomesTests.asyncSetUp
    asyncTearDown = test_homes.HomesTests.asyncTearDown
    buttons = test_homes.HomesTests.buttons

    async def prepare(self):
        d = self.api.inventory['devices'][0]
        d['type']='devices.types.socket'
        d['capabilities']=[{'type':'devices.capabilities.on_off'}]
        self.store.select_home(1,'h1')
        await self.bot.refresh(force=True)

    async def test_add_remove_and_persist_without_device_command(self):
        await self.prepare()
        await self.bot.extras_page(update(),0)
        self.assertIn('extraedit:'+key('a'),self.buttons())
        await self.bot.callback(update(data='extraset:'+key('a')+':1'),None)
        self.assertIn(key('a'),self.bot.devices)
        self.assertEqual(self.api.writes,[])
        other=Store(self.path)
        self.assertIn('a',other.extra_lights()); other.db.close()
        await self.bot.refresh(force=True)
        self.assertIn(key('a'),self.bot.devices)
        await self.bot.callback(update(data='set:'+key('a')+':on:1'),None)
        self.assertEqual(self.api.writes,['a'])
        await self.bot.callback(update(data='extraset:'+key('a')+':0'),None)
        self.assertNotIn(key('a'),self.bot.devices)
        await self.bot.callback(update(data='set:'+key('a')+':on:1'),None)
        self.assertEqual(self.api.writes,['a'])

    async def test_other_home_and_sensor_cannot_be_added(self):
        await self.prepare()
        self.store.select_home(1,'h2')
        await self.bot.callback(update(data='extraset:'+key('a')+':1'),None)
        self.assertNotIn('a',self.store.extra_lights())
        self.store.select_home(1,'h1')
        d=self.api.inventory['devices'][0]; d['type']='devices.types.sensor'
        await self.bot.refresh(force=True)
        await self.bot.callback(update(data='extraset:'+key('a')+':1'),None)
        self.assertNotIn('a',self.store.extra_lights())

    async def test_all_diagnostics_not_truncated(self):
        d=self.api.inventory['devices'][0]
        self.api.inventory['devices']=[dict(d,id=f'device-{i}',name=f'Устройство {i}',
            token='SECRET_NOT_TO_PRINT') for i in range(36)]
        await self.bot.refresh(force=True)
        text=self.bot.full_diagnostics(1)
        self.assertIn('36. Устройство 35',text)
        self.assertNotIn('SECRET_NOT_TO_PRINT',text)
        await self.bot.diagnostics(update(),0)
        self.assertIn('страница 1/5',self.bot.text)
        self.assertIn('diagall',self.buttons())
        class Message:
            async def reply_document(inner,document,filename,caption):
                inner.data=document.read().decode('utf-8')
                inner.filename=filename
        u=update(data='diagall'); u.effective_message=Message()
        await self.bot.callback(u,None)
        self.assertEqual(u.effective_message.data,text)
        self.assertEqual(u.effective_message.filename,'diagnostics_all.txt')

    async def test_env_extra_cannot_be_removed_in_menu(self):
        await self.prepare()
        self.bot.env_extra.add('a')
        await self.bot.callback(update(data='extraset:'+key('a')+':0'),None)
        self.assertIn('EXTRA_LIGHT_IDS',self.bot.text)
        self.assertIn('a',self.bot.extra)

if __name__=='__main__': unittest.main()
