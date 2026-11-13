import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from china_commodities.chat_export import encode, export_chat_data, restore, sha


class ChatExportTests(unittest.TestCase):
    def test_lossless_small_pages_and_removed_option_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {'products': [{'exchange': 'DCE', 'product': f'P{i}', 'value': None,
                                    'text': '中文' * 100} for i in range(50)],
                      'quality': {'limitations': ['research_only']},
                      'empty': [], 'unusual/key': {'~': {}},
                      'requested_date': '2026-11-13',
                      'physical': {'requested_date': '2026-11-12'}}
            (root / 'report_input_latest.json').write_bytes(encode(report))
            (root / 'options').mkdir()
            (root / 'options/latest.json').write_bytes(encode({'shards': [
                {'exchange': 'DCE', 'product': 'I', 'path': 'I.json.gz'}]}))
            (root / 'options/I.json.gz').write_bytes(gzip.compress(encode({'records': [{'price': None}]})))
            with patch('china_commodities.chat_export.MAX_BYTES', 8192):
                export_chat_data(root)
                for folder in (root / 'chat').iterdir():
                    if not folder.is_dir():
                        continue
                    index = json.loads((folder / 'index.json').read_bytes())
                    pages = []
                    for entry in index['parts']:
                        raw = (root / entry['path'].removeprefix('data/')).read_bytes()
                        self.assertEqual(sha(raw), entry['sha256'])
                        self.assertLessEqual(len(raw), 8192)
                        pages.append(json.loads(raw))
                    original = (root / index['source_path'].removeprefix('data/')).read_bytes()
                    if index['source_path'].endswith('.gz'):
                        original = gzip.decompress(original)
                    self.assertEqual(restore(pages), json.loads(original))
                before = {p: p.read_bytes() for p in (root / 'chat').rglob('*.json')}
                export_chat_data(root)
                self.assertEqual(before, {p: p.read_bytes() for p in (root / 'chat').rglob('*.json')})
                (root / 'options/latest.json').write_bytes(encode({'shards': []}))
                export_chat_data(root)
                self.assertFalse((root / 'chat/options_DCE_I/index.json').exists())

    def test_missing_declared_option_file_is_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'report_input_latest.json').write_bytes(b'{}')
            (root / 'options').mkdir()
            (root / 'options/latest.json').write_bytes(encode({'shards': [
                {'exchange': 'DCE', 'product': 'I', 'path': 'missing.json.gz'}]}))
            with self.assertRaisesRegex(ValueError, 'Missing referenced'):
                export_chat_data(root)


if __name__ == '__main__':
    unittest.main()
