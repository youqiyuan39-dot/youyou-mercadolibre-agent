import unittest

from src.pricing_intelligence import apply_inventory_rule, collect_competitor_pricing, forwarder_unit_cost, profit_methodology


class StubClient:
    def search_competitors(self, site, query, limit):
        return {"results": [
            {"id": "1", "title": "A", "price": 10, "currency_id": "USD", "condition": "new", "shipping": {"free_shipping": True}},
            {"id": "2", "title": "B", "price": 14, "currency_id": "USD", "condition": "new", "shipping": {"free_shipping": False}},
            {"id": "3", "title": "C", "price": 18, "currency_id": "USD", "condition": "new", "shipping": {}},
        ]}


class PricingIntelligenceTests(unittest.TestCase):
    def test_inventory_uses_store_default_instead_of_supplier_stock(self):
        draft = {"payload": {}, "evidence": {"supplier_page": {"skus": [{"inventory": 7}, {"inventory": 8}]}}}
        apply_inventory_rule(draft)
        self.assertEqual(draft["payload"]["available_quantity"], 200)
        fallback = {"payload": {}, "evidence": {"supplier_page": {"skus": []}}}
        apply_inventory_rule(fallback)
        self.assertEqual(fallback["payload"]["available_quantity"], 200)

    def test_profit_range_requires_complete_costs(self):
        incomplete = profit_methodology({"purchase_cost_cny": 28})
        self.assertEqual(incomplete["status"], "REAL_PROFIT_UNVERIFIED")
        complete = profit_methodology({
            "mode": "net_proceeds",
            "purchase_cost_cny": 28, "domestic_shipping_cny": 7,
            "exchange_rate_cny_per_usd": 7, "packaging_cost_usd": 0.5,
            "cross_border_freight_usd": 3, "other_cost_usd": 1,
            "package_weight_g":200,"package_length_cm":11,"package_width_cm":14,"package_height_cm":3,
            "forwarder_packing_service_cny":0.6,"forwarder_packaging_material_cny":0,"forwarder_value_added_cny":0,"forwarder_label_cny":0,
            "scenario_margin_pcts":[10,30],
        })
        self.assertEqual(complete["known_variable_cost_usd"], 6.59)
        self.assertEqual([r['net_proceeds_usd'] for r in complete['scenario_targets']], [7.32,9.41])
        self.assertEqual(complete['status'],'COST_SCENARIO_UNVERIFIED')
        self.assertNotIn('acceptable_margin_range_pct',complete)
        self.assertEqual(complete["platform_cost_treatment"], "REMOTE_NET_PROCEEDS_INCLUDES_PLATFORM_SHIPPING_AND_SALE_FEE")

    def test_manual_price_mode_still_requires_cross_border_freight(self):
        incomplete = profit_methodology({
            "mode": "price", "purchase_cost_cny": 28, "domestic_shipping_cny": 7,
            "exchange_rate_cny_per_usd": 7, "packaging_cost_usd": 0.5, "other_cost_usd": 1,
        })
        self.assertIn("cross_border_freight_usd", incomplete["missing_inputs"])

    def test_blank_other_cost_remains_unknown_without_explicit_scenario(self):
        result = profit_methodology({
            "mode": "net_proceeds", "purchase_cost_cny": 23, "domestic_shipping_cny": 5.6,
            "exchange_rate_cny_per_usd": 6.708258, "packaging_cost_usd": 0.6,
        })
        self.assertEqual(result["status"], "REAL_PROFIT_UNVERIFIED")
        self.assertIn('other_cost_usd',result['missing_inputs'])
        self.assertNotIn('current_target',result)

    def test_forwarder_confirmed_service_and_oversize_rules(self):
        normal = forwarder_unit_cost({})
        self.assertIsNone(normal["total_cny"])
        self.assertIsNone(normal['over_5kg_surcharge_cny'])
        stocked = forwarder_unit_cost({"forwarder_stocking": True})
        self.assertIsNone(stocked["total_cny"])
        self.assertIn('forwarder_label_cny',stocked['missing_inputs'])
        oversize = forwarder_unit_cost({"package_weight_g": 6000, "package_length_cm": 60,
                                        "package_width_cm": 40, "package_height_cm": 30})
        self.assertEqual(oversize["billable_weight_kg"], 6.0)
        self.assertEqual(oversize["over_5kg_surcharge_cny"], 1.0)

    def test_competitor_summary_uses_observed_prices(self):
        draft = {"payload": {"title": "Carburetor FS120", "sites_to_sell": ["MLB"]}, "pricing_plan": {}}
        result = collect_competitor_pricing(draft, StubClient())
        self.assertEqual(result["status"], "COMPETITOR_DATA_READY")
        self.assertEqual(result["sites"][0]["price_band_local"]["median"], 14)


if __name__ == "__main__":
    unittest.main()
