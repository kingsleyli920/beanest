"""Translation metadata must preserve platform provenance and opaque authored text."""
import copy
import json
import unittest

from local_agent_coordinator.collaboration_view import collaboration_view
from test_collaboration_view import snapshot


class CollaborationI18nTests(unittest.TestCase):
    def test_platform_waiting_and_mixed_remaining_have_explicit_literal_boundaries(self):
        value = snapshot(status='assigned', phase='working')
        task = value['team']['tasks'][0]
        task.update(title='等待独立审核', dependencies=['用户原文依赖'])
        projected = collaboration_view(value)
        self.assertEqual(copy.deepcopy(projected), projected)
        result = json.loads(json.dumps(projected))
        waiting = result['tasks'][0]
        self.assertEqual(waiting['title'], '等待独立审核')
        self.assertEqual(waiting['waiting_for_i18n']['parts'][-1], {'text': '用户原文依赖'})
        parts = result['outcome']['remaining_i18n'][0]['parts']
        self.assertEqual(parts[0], {'text': '等待独立审核'})
        self.assertTrue(any('key' in part for part in parts))
        self.assertIn('summary_i18n', result)
        self.assertIn('summary_i18n', result['outcome']['validation'])
        projected['summary_i18n']['parts'][0]['key'] = 'changed by a caller'
        self.assertNotEqual(collaboration_view(value)['summary_i18n']['parts'][0]['key'], 'changed by a caller')

    def test_model_question_and_unknown_block_reason_never_become_system_translation_keys(self):
        value = snapshot(status='blocked', phase='working')
        value['team']['tasks'][0]['blocked_reason'] = '等待独立审核'
        value['objective'] = {'status': 'needs_user', 'next_step': {'question': '伙伴正在按共享清单推进。'}}
        result = collaboration_view(value)
        self.assertNotIn('reason_i18n', result['outcome'])
        self.assertNotIn('waiting_for_i18n', result['tasks'][0])
        self.assertEqual(result['outcome']['reason'], value['objective']['next_step']['question'])
        self.assertIsNone(result['outcome']['remaining_i18n'][0])
        self.assertEqual(result['outcome']['remaining'][0], 'Deliver result：等待独立审核')
