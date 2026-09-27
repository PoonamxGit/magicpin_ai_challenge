"""Generalization and conversation tests for evidence-based engagement rules."""
import copy
import unittest
import bot
from test_bot import CATEGORIES, MERCHANTS, CUSTOMERS, TRIGGERS, NOW


class EngagementTests(unittest.TestCase):
    def setUp(self):
        self.e = bot.Engine()

    def prepare(self, merchant, trigger, customer=None):
        for scope, cid, payload in [("category", merchant["category_slug"], CATEGORIES[merchant["category_slug"]]),
                                    ("merchant", merchant["merchant_id"], merchant),
                                    ("trigger", trigger["id"], trigger)]:
            self.e.push(dict(scope=scope, context_id=cid, payload=payload, version=1, delivered_at=NOW))
        if customer:
            self.e.push(dict(scope="customer", context_id=customer["customer_id"], payload=customer, version=1, delivered_at=NOW))
        actions = self.e.tick(dict(now=NOW, available_triggers=[trigger["id"]]))["actions"]
        self.assertEqual(1, len(actions))
        self.action = actions[0]
        return self.action

    def reply(self, message, turn=2):
        return self.e.reply(dict(conversation_id=self.action["conversation_id"], from_role="merchant",
                                 message=message, turn_number=turn, received_at=NOW))

    def test_new_merchant_planning_handoff_has_real_copy(self):
        m=copy.deepcopy(MERCHANTS[5]);m['merchant_id']='fresh_bistro';m['identity']['name']='New Bistro'
        t=copy.deepcopy(TRIGGERS[12]);t.update(id='fresh_plan',merchant_id=m['merchant_id'])
        t['payload']['intent_topic']='team breakfast boxes'
        a=self.prepare(m,t)
        self.assertIn('team breakfast boxes',a['body'])
        self.assertIn('group size',a['body'])
        r=self.reply("let's do it")
        self.assertEqual('send',r['action'])
        self.assertIn('New Bistro',r['body'])
        self.assertIn('quote need confirmation',r['body'])
        self.assertNotIn('would you',r['body'].lower())
        self.assertIn('nothing has been published',r['body'])
        self.assertNotIn('Proposed enquiry copy',r['body'])

    def test_program_does_not_reuse_unrelated_membership_price(self):
        body=bot.planning_draft(MERCHANTS[7],TRIGGERS[15])
        self.assertIn('age group',body)
        self.assertNotIn('499',body)

    def test_refill_details_and_no_dispatch_claim(self):
        m,c,t=MERCHANTS[8],CUSTOMERS[12],TRIGGERS[18]
        body=bot.compose(CATEGORIES['pharmacies'],m,t,c)['body']
        self.assertIn('metformin',body)
        self.assertIn('atorvastatin',body)
        self.assertIn('morning delivery',body)
        result,_=self.e.response_text(CATEGORIES['pharmacies'],m,t,c,'YES','en',{})
        self.assertIn('prescription',result)
        self.assertIn('Dispatch is not confirmed',result)
        self.assertNotIn('1420',result)

    def test_winback_offer_but_no_invented_class(self):
        body=bot.compose(CATEGORIES['gyms'],MERCHANTS[6],TRIGGERS[14],CUSTOMERS[9])['body']
        self.assertIn('3 FREE Trial Classes',body)
        self.assertIn('eligibility',body)
        self.assertIn('weekday evening',body)
        self.assertNotIn('HIIT',body)
        c=copy.deepcopy(CUSTOMERS[9]);c['consent']['scope']=['appointment_reminders']
        self.assertFalse(bot.compose(CATEGORIES['gyms'],MERCHANTS[6],TRIGGERS[14],c)['body'])

    def test_injected_event_terms_flow_to_opening_and_yes(self):
        t=copy.deepcopy(TRIGGERS[21]);t['id']='new_event';t['payload'].update(credits=7,fee='members_only')
        a=self.prepare(MERCHANTS[0],t)
        self.assertIn('7',a['body']);self.assertIn('members only',a['body'])
        reply=self.reply('YES')['body']
        self.assertIn('7 credits',reply);self.assertIn('No registration',reply)

    def test_no_unrelated_research_citation_for_compliance(self):
        t=copy.deepcopy(TRIGGERS[1]);t['payload']={}
        self.assertFalse(bot.compose(CATEGORIES['dentists'],MERCHANTS[0],t)['body'])

    def test_seasonal_yes_delivers_stock_checklist(self):
        self.prepare(MERCHANTS[8],TRIGGERS[19])
        r=self.reply('YES')['body']
        self.assertIn('Shelf-review',r);self.assertIn('expiry dates',r)
        self.assertNotIn('profile update',r)

    def test_milestone_below_target_never_congratulates(self):
        self.prepare(MERCHANTS[5],TRIGGERS[11])
        r=self.reply('YES')['body']
        self.assertIn('honest feedback',r)
        self.assertNotIn('reach 150',r)
        t=copy.deepcopy(TRIGGERS[11]);t['payload']['metric']='visits'
        r=bot.outreach_draft(CATEGORIES['restaurants'],MERCHANTS[5],t)
        self.assertIn('still ahead',r);self.assertNotIn('helping us reach',r)

    def test_match_yes_does_not_extend_restricted_offer(self):
        self.prepare(MERCHANTS[4],TRIGGERS[9])
        r=self.reply('YES')['body']
        self.assertIn('DC vs MI',r)
        self.assertNotIn('Buy 1',r)
        self.assertNotIn('delivery-only',r)

    def test_repeated_auto_replies_end_without_human_session(self):
        self.prepare(MERCHANTS[0],TRIGGERS[0])
        for turn in range(2,6):
            self.assertEqual('end',self.reply('Thank you for contacting us. Our team will respond shortly.',turn)['action'])
        self.assertEqual({},self.e.sessions)

    def test_stop_blocks_fresh_conversation(self):
        self.prepare(MERCHANTS[0],TRIGGERS[0])
        self.assertEqual('end',self.reply('STOP')['action'])
        self.assertEqual('end',self.e.reply(dict(conversation_id='new',merchant_id=MERCHANTS[0]['merchant_id'],
            from_role='merchant',message='YES',turn_number=2,received_at=NOW))['action'])

    def test_time_price_and_already_done_objections(self):
        self.prepare(MERCHANTS[1],TRIGGERS[4])
        r=self.reply('too expensive')['body']
        self.assertIn('4999',r);self.assertNotIn('Cleaning',r)
        self.assertEqual('wait',self.reply('no time',3)['action'])
        self.assertIn('dobara',self.reply('already updated',4)['body'])

    def test_perf_spike_yes_has_customer_enquiry_copy(self):
        self.prepare(MERCHANTS[7],TRIGGERS[23])
        r=self.reply('YES')['body']
        self.assertIn('Enquiry-response draft',r)
        self.assertIn('class',r)


if __name__=='__main__':
    unittest.main()
