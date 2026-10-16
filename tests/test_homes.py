import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from bot import HomeBot, Store, key
from core import APIError


class FakeAPI:
    def __init__(self):
        self.reads=[]
        self.writes=[]
        self.inventory={
            'households':[{'id':'h1','name':'Квартира'},{'id':'h2','name':'Дача'}],
            'rooms':[{'id':'r1','name':'Кухня','household_id':'h1'},
                     {'id':'r2','name':'Кухня','household_id':'h2'}],
            'devices':[
                {'id':'a','name':'Лампа','type':'devices.types.light','household_id':'h1','room':'r1'},
                {'id':'b','name':'Лампа','type':'devices.types.light','household_id':'h2','room':'r2'},
                {'id':'c','name':'Без комнаты 1','type':'devices.types.light','household_id':'h1','room':None},
                {'id':'d','name':'Без комнаты 2','type':'devices.types.light','household_id':'h2','room':None}]}

    async def info(self):
        return self.inventory

    async def device(self,did):
        self.reads.append(did)
        # The detail endpoint can omit household_id.
        d=next(dict(d) for d in self.inventory['devices'] if d['id']==did)
        d.pop('household_id',None)
        return d

    async def set(self,d,kind,value):
        self.writes.append(d['id'])


class Capture(HomeBot):
    async def show(self,update,text,rows):
        self.text,self.rows=text,rows


class Query:
    def __init__(self,data): self.data=data
    async def answer(self,*args,**kwargs): pass


def update(uid=1, data=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(type='private'), callback_query=Query(data) if data else None)


class HomesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'db'
        # Simulate the existing v1 database, retaining existing preferences.
        db=sqlite3.connect(self.path)
        db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
        db.execute('INSERT INTO users VALUES(1,0)')
        db.commit(); db.close()
        self.store=Store(self.path)
        self.store.join(2)
        self.api=FakeAPI()
        self.bot=Capture(self.api,{1,2},self.store)
        await self.bot.refresh()

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def buttons(self):
        return [b.callback_data for row in self.bot.rows for b in row]

    async def test_first_start_requires_choice_and_home_switch(self):
        await self.bot.menu(update(),None)
        self.assertIn('choosehome:'+key('h1'),self.buttons())
        self.assertIn('choosehome:'+key('h2'),self.buttons())
        self.assertIsNone(self.store.home(1))
        await self.bot.callback(update(data='choosehome:'+key('h2')),None)
        self.assertEqual(self.store.home(1),'h2')
        self.assertIn('Дача',self.bot.text)
        self.assertIn('homes:0',self.buttons())

    async def test_separate_home_choices_and_db_migration(self):
        self.store.select_home(1,'h1'); self.store.select_home(2,'h2')
        self.assertFalse(self.store.enabled(1))
        other=Store(self.path)
        self.assertEqual(other.home(1),'h1'); self.assertEqual(other.home(2),'h2')
        other.db.close()
        await self.bot.lights(update(1),'all',0)
        self.assertIn('device:'+key('a'),self.buttons())
        self.assertNotIn('device:'+key('b'),self.buttons())
        await self.bot.lights(update(2),'all',0)
        self.assertIn('device:'+key('b'),self.buttons())
        self.assertNotIn('device:'+key('a'),self.buttons())

    async def test_rooms_favorites_and_roomless_filtered(self):
        self.store.select_home(1,'h1')
        self.store.favorite(1,'a'); self.store.favorite(1,'b')
        await self.bot.rooms_page(update(),0)
        self.assertIn('list:'+key('r1')+':0',self.buttons())
        self.assertNotIn('list:'+key('r2')+':0',self.buttons())
        await self.bot.lights(update(),'none',0)
        self.assertIn('device:'+key('c'),self.buttons())
        self.assertNotIn('device:'+key('d'),self.buttons())
        await self.bot.lights(update(),'fav',0)
        self.assertIn('device:'+key('a'),self.buttons())
        self.assertNotIn('device:'+key('b'),self.buttons())

    async def test_old_device_buttons_cannot_control_other_home(self):
        self.store.select_home(1,'h2')
        for data in ('device:'+key('a'),'fav:'+key('a'),'set:'+key('a')+':on:1'):
            await self.bot.callback(update(data=data),None)
            self.assertIn('другому дому',self.bot.text)
        self.assertEqual(self.api.reads,[])
        self.assertEqual(self.api.writes,[])
        self.assertEqual(self.store.favorites(1),set())

    async def test_detail_keeps_membership_and_current_home_command_works(self):
        self.store.select_home(1,'h1')
        await self.bot.panel(update(),key('a'))
        self.assertEqual(self.bot.devices[key('a')]['household_id'],'h1')
        await self.bot.callback(update(data='set:'+key('a')+':on:1'),None)
        self.assertEqual(self.api.writes,['a'])
        self.assertIn('Квартира',self.bot.text)

    async def test_removed_home_requires_new_choice(self):
        self.store.select_home(1,'deleted')
        await self.bot.menu(update(),None)
        self.assertIn('Выберите дом',self.bot.text)
        await self.bot.callback(update(data='choosehome:'+key('deleted')),None)
        self.assertIn('Дом удалён',self.bot.text)

    async def test_single_home_autoselect_and_room_fallback(self):
        self.api.inventory['households']=self.api.inventory['households'][:1]
        self.api.inventory['rooms']=self.api.inventory['rooms'][:1]
        self.api.inventory['devices']=self.api.inventory['devices'][:1]
        self.api.inventory['devices'][0].pop('household_id')
        await self.bot.refresh(force=True)
        await self.bot.menu(update(),None)
        self.assertEqual(self.store.home(1),'h1')
        self.assertEqual(self.bot.devices[key('a')]['household_id'],'h1')
        self.assertIn('Квартира',self.bot.text)

if __name__=='__main__': unittest.main()
