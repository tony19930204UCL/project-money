"""Offline Q2 research queue regression coverage."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cio_market_lab.engine import paper_research_queue as q


def candidate(**changes):
    item = dict(id='q2-case', kind=q.KIND, paper_only=True, authorized=True,
                authorization='PRE_AUTHORIZED_PAPER_SANDBOX', sandboxed=True,
                live_orders=False, cost_model_escalation=False,
                authorization_basis='explicit-paper-approval', timeout_seconds=1200,
                output='synthetic-output')
    item.update(changes)
    return item


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_enqueue_ok(self):
        self.assertEqual(q.enqueue(candidate(), self.root)['status'], 'ENQUEUED')
        self.assertEqual(q.load(self.root)['items'][0]['state'], 'PENDING')

    def test_duplicate_no_second_entry(self):
        q.enqueue(candidate(), self.root)
        self.assertEqual(q.enqueue(candidate(), self.root)['status'], 'DUPLICATE')
        self.assertEqual(len(q.load(self.root)['items']), 1)

    def test_unauthorized_rejected(self):
        for change in (dict(authorized=False), dict(paper_only=False),
                       dict(sandboxed=False), dict(live_orders=True),
                       dict(cost_model_escalation=True), dict(authorization='OTHER')):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    q.enqueue(candidate(**change), self.root)
        self.assertFalse((self.root / 'queue.json').exists())

    def test_register(self):
        self.assertEqual(q.register(candidate(), self.root)['status'], 'ENQUEUED')
        self.assertEqual(len(q.load(self.root)['items']), 1)

    def test_atomic_persistence(self):
        q.enqueue(candidate(), self.root)
        self.assertTrue((self.root / 'queue.json').is_file())
        self.assertFalse((self.root / 'queue.json.tmp').exists())
        self.assertEqual(json.loads((self.root / 'queue.json').read_text())['items'][0]['id'], 'q2-case')

    def test_corrupt_file_refuses_and_preserves_bytes(self):
        p = self.root / 'queue.json'
        p.write_text('{bad-json')
        with self.assertRaises(ValueError):
            q.enqueue(candidate(), self.root)
        self.assertEqual(p.read_text(), '{bad-json')

    def test_invalid_schema_refuses(self):
        p = self.root / 'queue.json'
        p.write_text('{"version":1,"items":"bad"}')
        with self.assertRaises(ValueError):
            q.register(candidate(), self.root)
        self.assertEqual(json.loads(p.read_text())['items'], 'bad')

    def test_invalid_id_refuses(self):
        with self.assertRaises(ValueError):
            q.enqueue(candidate(id='../escape'), self.root)


if __name__ == '__main__':
    unittest.main()
