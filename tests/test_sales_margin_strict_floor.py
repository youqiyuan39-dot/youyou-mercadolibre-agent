import unittest
from src.market_follow_v3 import solve


class StrictSalesMarginFloor(unittest.TestCase):
    def test_strict_floor_uses_sales_and_unrounded_ratio(self):
        p={'margin_policy_status':'CONFIRMED','confirmed_margin_basis':'BUYER_PRICE',
           'minimum_margin_pct':'10','minimum_margin_operator':'GT','other_cny':'0',
           'ads_net_pct':'0','tax_reserve_net_pct':'0','returns_base_pct':'0'}
        below=solve(100,80,'70.004',0,0,0,1,p)
        equal=solve(100,80,70,0,0,0,1,p)
        above=solve(100,80,'69.996',0,0,0,1,p)
        self.assertEqual([x['sales_margin_pct'] for x in [below,equal,above]],['10.00']*3)
        self.assertEqual([x['floor_passed'] for x in [below,equal,above]],[False,False,True])
        self.assertEqual(equal['margin_basis'],'BUYER_PRICE')
        self.assertEqual(equal['decision'],'BELOW_FLOOR')
        self.assertIsNone(solve(100,80,70,0,0,0,1,{**p,'confirmed_margin_basis':'NET_PROCEEDS'})['floor_passed'])


if __name__=='__main__':unittest.main()
