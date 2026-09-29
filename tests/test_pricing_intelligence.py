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
    def test_inventory_uses_1688_or_default(self):
        draft = {"payload": {}, "evidence": {"supplier_page": {"skus": [{"inventory": 7}, {"inventory": 8}]}}}
        apply_inventory_rule(draft)
        self.assertEqual(draft["payload"]["available_quantity"], 15)
        fallback = {"payload": {}, "evidence": {"supplier_page": {"skus": []}}}
        apply_inventory_rule(fallback)
        self.assertEqual(fallback["payload"]["available_quantity"], 1000)

    def test_profit_range_requires_complete_costs(self):
        incomplete = profit_methodology({"purchase_cost_cny": 28})
        self.assertEqual(incomplete["status"], "REAL_PROFIT_UNVERIFIED")
        complete = profit_methodology({
            "mode": "net_proceeds",
            "purchase_cost_cny": 28, "domestic_shipping_cny": 7,
            "exchange_rate_cny_per_usd": 7, "packaging_cost_usd": 0.5,
            "cross_border_freight_usd": 3, "other_cost_usd": 1,
        })
        self.assertEqual(complete["known_variable_cost_usd"], 6.59)
        self.assertEqual(complete["recommended_net_proceeds_range_usd"], [7.32, 9.41])
        self.assertEqual(complete["preferred_target_net_proceeds_usd"], 9.41)
        self.assertEqual(complete["platform_cost_treatment"], "REMOTE_NET_PROCEEDS_INCLUDES_PLATFORM_SHIPPING_AND_SALE_FEE")

    def test_manual_price_mode_still_requires_cross_border_freight(self):
        incomplete = profit_methodology({
            "mode": "price", "purchase_cost_cny": 28, "domestic_shipping_cny": 7,
            "exchange_rate_cny_per_usd": 7, "packaging_cost_usd": 0.5, "other_cost_usd": 1,
        })
        self.assertIn("cross_border_freight_usd", incomplete["missing_inputs"])

    def test_blank_other_cost_uses_explicit_zero_estimate(self):
        result = profit_methodology({
            "mode": "net_proceeds", "purchase_cost_cny": 23, "domestic_shipping_cny": 5.6,
            "exchange_rate_cny_per_usd": 6.708258, "packaging_cost_usd": 0.6,
        })
        self.assertEqual(result["status"], "PROVISIONAL_CONTRIBUTION_RANGE")
        self.assertEqual(result["recommended_net_proceeds_range_usd"], [5.5, 7.08])
        self.assertIn("暂按 0 USD", result["assumptions"][0])

    def test_forwarder_confirmed_service_and_oversize_rules(self):
        normal = forwarder_unit_cost({})
        self.assertEqual(normal["total_cny"], 0.6)
        stocked = forwarder_unit_cost({"forwarder_stocking": True})
        self.assertEqual(stocked["total_cny"], 0.7)
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
