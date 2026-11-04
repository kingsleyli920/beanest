import unittest

from local_agent_coordinator.block_contract import (
    BLOCK_CAUSES, LEGACY_BLOCK_CAUSE, BlockContractError, normalize_block_cause,
)


class BlockContractTests(unittest.TestCase):
    def test_missing_and_legacy_null_remain_unclassified(self):
        self.assertEqual(normalize_block_cause(), LEGACY_BLOCK_CAUSE)
        self.assertEqual(normalize_block_cause(None), 'unclassified')

    def test_only_explicit_causes_are_accepted_without_translation(self):
        expected = {'technical', 'user_decision', 'permission_required', 'external_dependency', 'coordination_required'}
        self.assertEqual(BLOCK_CAUSES, expected)
        self.assertIsInstance(BLOCK_CAUSES, frozenset)
        for value in expected:
            with self.subTest(value=value):
                self.assertEqual(normalize_block_cause(value), value)

    def test_bad_values_rejected_with_a_stable_error(self):
        for value in ('', 'unclassified', 'Technical', 'technical ', ' technical', 'quota',
                      True, False, 0, 1, [], {}, {'cause': 'technical'}):
            with self.subTest(value=value):
                with self.assertRaises(BlockContractError) as caught:
                    normalize_block_cause(value)
                self.assertEqual(caught.exception.code, 'invalid_block_cause')
                self.assertEqual(str(caught.exception), 'invalid_block_cause')

    def test_prose_does_not_turn_a_human_prohibition_into_technical_authority(self):
        for value in ('technical issue: user forbids this change', '用户禁止修改，需要停止',
                      'permission_required: use Full Access', 'external dependency is broken'):
            with self.subTest(value=value), self.assertRaises(BlockContractError):
                normalize_block_cause(value)
        self.assertEqual(normalize_block_cause(), 'unclassified')
