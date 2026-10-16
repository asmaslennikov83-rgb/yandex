import unittest
import test_homes
from test_homes import update
from bot import key


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_homes.HomesTests.asyncSetUp
    asyncTearDown = test_homes.HomesTests.asyncTearDown
    buttons = test_homes.HomesTests.buttons

    async def test_device_membership_from_room_list(self):
        d = self.api.inventory['devices'][0]
        d.pop('household_id'); d.pop('room')
        self.api.inventory['rooms'][0]['devices'] = ['a']
        await self.bot.refresh(force=True)
        self.assertEqual(self.bot.devices[key('a')]['household_id'], 'h1')
        self.assertEqual(self.bot.devices[key('a')]['room'], 'r1')
        self.store.select_home(1, 'h1')
        await self.bot.lights(update(), key('r1'), 0)
        self.assertIn('device:' + key('a'), self.buttons())

    async def test_device_membership_from_group_list(self):
        d = self.api.inventory['devices'][0]
        d.pop('household_id'); d.pop('room')
        self.api.inventory['groups'] = [{'id': 'g', 'household_id': 'h2', 'devices': ['a']}]
        await self.bot.refresh(force=True)
        self.assertEqual(self.bot.devices[key('a')]['household_id'], 'h2')

    async def test_ambiguous_membership_stays_unassigned(self):
        d = self.api.inventory['devices'][0]
        d.pop('household_id'); d.pop('room')
        self.api.inventory['groups'] = [{'id': 'g1', 'household_id': 'h1', 'devices': ['a']},
            {'id': 'g2', 'household_id': 'h2', 'devices': ['a']}]
        await self.bot.refresh(force=True)
        self.assertEqual(self.bot.devices[key('a')]['household_id'], '__unassigned__')

    async def test_diagnostics_whitelist_and_switch_filter(self):
        d=self.api.inventory['devices'][0]
        d['token']='SECRET_NOT_TO_PRINT'
        d['type']='devices.types.switch'
        await self.bot.refresh(force=True)
        await self.bot.diagnostics(update(),0)
        self.assertIn('devices.types.switch',self.bot.text)
        self.assertIn('Свет: нет',self.bot.text)
        self.assertNotIn('SECRET_NOT_TO_PRINT',self.bot.text)
        self.assertNotIn(key('a'),self.bot.devices)
        self.store.select_home(1,'h1')
        await self.bot.lights(update(),'fav',0)
        self.assertIn('diag:0',self.buttons())

    async def test_command_requires_whitelist(self):
        u=update(99)
        # guard sends denial through Telegram; capture it without network.
        class Message:
            async def reply_text(self,text): pass
        u.effective_message=Message()
        await self.bot.diagnostics_command(u,None)
        self.assertFalse(hasattr(self.bot,'text'))

if __name__=='__main__': unittest.main()
