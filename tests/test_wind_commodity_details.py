from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from china_commodities import wind_commodity_details as details
from china_commodities.storage import write_json_gzip_atomic
from china_commodities.wind_daily import ZONE


DAY = '2026-11-26'
CODE = 'CU2701P84000.SHF'


def ticks(times, data):
    return dict(codes=[CODE], fields=details.BOOK_FIELDS.split(','), times=times, data=data)


class CommodityDetailsTests(unittest.TestCase):
    def test_last_tick_preserves_one_side_and_timestamp(self):
        api = Mock()
        api.call.return_value = ticks([DAY+'T14:59:50',DAY+'T14:59:59.500'],
                                      [[5,5],[6,1.7976931348623157e308],[1,2],[1,0]])
        book = details.option_book(api,CODE,DAY)
        self.assertEqual((book['bid'],book['bid_size'],book['ask'],book['book_state']),(5,2,None,'one_sided'))
        self.assertEqual(book['book_source_timestamp'],DAY+'T14:59:59.500000+08:00')
        api.call.assert_called_once()

    def test_no_ticks_in_both_windows_is_observed_absence(self):
        api = Mock()
        api.call.return_value = ticks([], [[],[],[],[]])
        book = details.option_book(api,CODE,DAY)
        self.assertEqual(api.call.call_count,2)
        self.assertEqual(book['book_state'],'no_day_ticks')
        self.assertIsNone(book['book_source_timestamp'])

    def test_wrong_source_day_and_crossed_book(self):
        api = Mock()
        api.call.return_value = ticks(['2026-11-25T14:59:59'], [[7],[6],[1],[2]])
        with self.assertRaises(ValueError):
            details.option_book(api,CODE,DAY)
        api.discard.assert_called_once()
        api.call.return_value['times'] = [DAY+'T14:59:59']
        book = details.option_book(api,CODE,DAY)
        self.assertEqual(book['book_state'],'crossed')
        self.assertIsNone(book['bid'])

    def test_daily_values_distinguish_zero_blank_and_signed_basis(self):
        api = Mock()
        api.call.return_value = dict(codes=['A','B','C'],fields=['ANAL_BASIS'],times=[DAY],data=[[0,None,-4]])
        self.assertEqual(details.daily_values(api,['C','A','B'],'anal_basis',DAY),{'A':0,'B':None,'C':-4})
        api.call.return_value['times'] = ['2026-11-25']
        with self.assertRaises(ValueError):
            details.daily_values(api,['C','A','B'],'anal_basis',DAY)

    def test_members_exclude_totals_and_preserve_missing_change(self):
        api = Mock()
        api.call.return_value = dict(fields=['date','member_name','ranks','long_position','long_position_increase'],
            data=[[DAY]*3,['total','A','B'],[None,1,2],[17,9,8],[1,None,3]])
        rows = details.member_list(api,'CU2701.SHF','long',DAY)
        self.assertEqual([r['member'] for r in rows],['A','B'])
        self.assertIsNone(rows[0]['change'])
        api.call.return_value['data'][2] = [None,1,1]
        with self.assertRaises(ValueError):
            details.member_list(api,'CU2701.SHF','long',DAY)

    def test_only_vendor_blanks_in_a_same_day_publication_get_a_later_recheck(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026,11,27,8,tzinfo=ZONE)
            self.assertTrue(details.needs_collection(root,DAY,now))
            product = dict(exchange='SHFE',product='CU',contract='CU2701',source_code='CU2701.SHF',
                           warehouse_quantity=None,previous_warehouse_quantity=0,vendor_basis=-3,long=[],short=[])
            api = Mock()
            api.call.return_value = ticks([], [[],[],[],[]])
            books = {CODE:details.option_book(api,CODE,DAY)}
            basis = {'CU2701.SHF':-3}
            payload = dict(schema_version=1,source_provider='wind',trade_date=DAY,previous_trade_date='2026-11-25',
                           checked_after_trade_date=False,execution_ready=False,products=[product],option_books=books,
                           contract_basis=basis,coverage=details.coverage([product],books,basis))
            path = details.dated_path(root,DAY)
            write_json_gzip_atomic(path,payload)
            from china_commodities.storage import write_json_atomic
            f = dict(trade_date=DAY, futures_contracts=[dict(product,source_trade_date=DAY,source_provider='wind')],
                     commodity_curves=[dict(exchange='SHFE',product='CU',main_contract={'contract':'CU2701'})])
            write_json_atomic(root/'snapshots'/(DAY+'.json'),f)
            write_json_gzip_atomic(root/'options/snapshots'/(DAY+'.json.gz'),{'records':[{'source_code':CODE}]})
            self.assertTrue(details.needs_collection(root,DAY,now))
            payload['checked_after_trade_date'] = True
            write_json_gzip_atomic(path,payload)
            self.assertFalse(details.needs_collection(root,DAY,now))
