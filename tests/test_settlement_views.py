import json
import unittest

from _bootstrap import (
    m, gl, make_contract, set_caller, call_payable, reset_transfers, create_deal, open_deal, load, patch_deal,
    pass_deadline, resolve, FakePyPI, release_payload, iso, CLIENT, DEV, STRANGER, SHA_WHEEL,
)


def settle_with(outcome, stake=1000, value=None):
    c = make_contract()
    reset_transfers()
    deal = open_deal(c, value=stake)
    patch_deal(c, deal, final_outcome=outcome, status="finalized")
    d = load(c, deal)
    c._settle(deal, d)
    return c, deal


class SettlementTable(unittest.TestCase):
    def test_delivered_pays_the_developer_the_whole_pot(self):
        c, _ = settle_with("DELIVERED")
        self.assertEqual(c.get_pending_withdrawal(DEV), "2000")
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "0")

    def test_not_delivered_pays_the_client_the_whole_pot(self):
        c, _ = settle_with("NOT_DELIVERED")
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "2000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "0")

    def test_partial_splits_evenly(self):
        c, _ = settle_with("PARTIALLY_DELIVERED")
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "1000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "1000")

    def test_partial_with_an_odd_pot_loses_no_wei(self):
        c, _ = settle_with("PARTIALLY_DELIVERED", stake=7)
        total = int(c.get_pending_withdrawal(CLIENT)) + int(c.get_pending_withdrawal(DEV))
        self.assertEqual(total, 14)

    def test_undetermined_refunds_each_party_its_own_stake(self):
        c, _ = settle_with("UNDETERMINED")
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "1000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "1000")

    def test_settlement_runs_exactly_once(self):
        c, deal = settle_with("DELIVERED")
        d = load(c, deal)
        d["payout_settled"] = True
        c._settle(deal, d)
        self.assertEqual(c.get_pending_withdrawal(DEV), "2000")

    def test_finalize_unappealed_waits_for_the_window(self):
        c = make_contract()
        deal = open_deal(c)
        resolve(c, deal, FakePyPI().release().index())
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            c.finalize_unappealed(deal)
        patch_deal(c, deal, appeal_deadline=iso(-5))
        d = json.loads(c.finalize_unappealed(deal))
        self.assertEqual((d["status"], d["final_outcome"]), ("finalized", "DELIVERED"))
        self.assertEqual(c.get_pending_withdrawal(DEV), "2000")
        with self.assertRaises(gl.vm.UserError):
            c.finalize_unappealed(deal)

    def test_finalize_unappealed_needs_a_resolved_deal(self):
        c = make_contract()
        deal = open_deal(c)
        with self.assertRaises(gl.vm.UserError):
            c.finalize_unappealed(deal)


class Withdraw(unittest.TestCase):
    def setUp(self):
        self.c, self.deal = settle_with("DELIVERED")
        self.c.balance = m.u256(2000)

    def test_pays_out_the_whole_balance_once(self):
        set_caller(DEV)
        self.c.withdraw()
        self.assertEqual(gl.evm.transfers, [{"to": DEV, "value": 2000}])
        self.assertEqual(self.c.get_pending_withdrawal(DEV), "0")
        with self.assertRaises(gl.vm.UserError):
            self.c.withdraw()
        self.assertEqual(len(gl.evm.transfers), 1)

    def test_someone_with_nothing_cannot_withdraw(self):
        for who in (CLIENT, STRANGER):
            set_caller(who)
            with self.assertRaises(gl.vm.UserError):
                self.c.withdraw()
        self.assertEqual(gl.evm.transfers, [])

    def test_ledger_is_zeroed_before_the_transfer(self):
        seen = {}
        original = gl.evm.transfers

        class Spy(list):
            def append(inner, item):
                seen["balance_at_transfer"] = self.c.get_pending_withdrawal(DEV)
                super().append(item)
        namespace = type(gl.evm)
        namespace.transfers = Spy()
        try:
            set_caller(DEV)
            self.c.withdraw()
        finally:
            namespace.transfers = original
        self.assertEqual(seen["balance_at_transfer"], "0")

    def test_withdraw_is_case_insensitive_on_the_address_key(self):
        c = make_contract()
        c._credit(DEV.upper().replace("0X", "0x"), m.u256(5))
        self.assertEqual(c.get_pending_withdrawal(DEV), "5")


class Views(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        for i in range(5):
            create_deal(self.c, scope=f"scope {i}", version=f"1.0.{i}")

    def listing(self, offset, limit):
        return json.loads(self.c.list_deals(offset, limit))

    def test_newest_first_with_paging(self):
        page = self.listing(0, 2)
        self.assertEqual([d["deal_id"] for d in page["deals"]], ["deal_4", "deal_3"])
        self.assertEqual(page["total"], 5)
        page = self.listing(2, 2)
        self.assertEqual([d["deal_id"] for d in page["deals"]], ["deal_2", "deal_1"])
        page = self.listing(4, 10)
        self.assertEqual([d["deal_id"] for d in page["deals"]], ["deal_0"])

    def test_offset_beyond_the_end_is_empty(self):
        self.assertEqual(self.listing(99, 5)["deals"], [])

    def test_limit_is_clamped(self):
        self.assertEqual(len(self.listing(0, 0)["deals"]), 1)
        self.assertEqual(len(self.listing(0, 10_000)["deals"]), 5)
        self.assertEqual(len(self.listing(-3, 3)["deals"]), 3)

    def test_summary_is_compact_and_has_the_browse_fields(self):
        row = self.listing(0, 1)["deals"][0]
        for key in ("deal_id", "status", "package", "version", "party_a", "party_b", "stake_amount", "deadline",
                    "scope", "tier1_outcome", "final_outcome", "required_kinds"):
            self.assertIn(key, row)
        self.assertNotIn("evidence", row)
        self.assertNotIn("delivery_criteria", row)

    def test_summary_truncates_a_long_scope(self):
        create_deal(self.c, scope="x" * 2000)
        self.assertEqual(len(self.listing(0, 1)["deals"][0]["scope"]), 200)

    def test_stats_count_by_status(self):
        set_caller(DEV)
        call_payable(self.c, "accept_deal", 1000, "deal_0")
        set_caller(CLIENT)
        self.c.cancel_deal("deal_1")
        stats = json.loads(self.c.get_stats())
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["by_status"], {"created": 3, "open": 1, "cancelled": 1})

    def test_config_exposes_the_windows_for_the_frontend(self):
        cfg = json.loads(self.c.get_config())
        self.assertEqual(cfg["jury_size"], 5)
        self.assertEqual(cfg["evidence_host"], "pypi.org")
        self.assertEqual(cfg["absence_grace_seconds"], m.ABSENCE_GRACE_SECONDS)
        self.assertEqual(cfg["appeal_bond_bps"], 2000)

    def test_preview_evidence_urls(self):
        urls = json.loads(self.c.preview_evidence_urls("My_Pkg", "2.0.0"))
        self.assertEqual(urls["release"], "https://pypi.org/pypi/my-pkg/2.0.0/json")
        with self.assertRaises(gl.vm.UserError):
            self.c.preview_evidence_urls("a/b", "1.0.0")

    def test_get_deal_and_unknown_ids(self):
        self.assertEqual(json.loads(self.c.get_deal("deal_3"))["deal_id"], "deal_3")
        with self.assertRaises(gl.vm.UserError):
            self.c.get_deal("deal_99")

    def test_juror_view_defaults(self):
        rec = json.loads(self.c.get_juror(STRANGER))
        self.assertEqual((rec["stake"], rec["reputation"]), ("0", 0))

    def test_contract_balance_view(self):
        self.assertEqual(self.c.get_contract_balance(), "5000")

    def test_release_claim_view_for_unknown_release(self):
        self.assertEqual(self.c.get_release_claim("nothing", "9.9.9"), "")


class JurorRegistry(unittest.TestCase):
    def test_register_tops_up_and_keeps_first_registration_time(self):
        c = make_contract()
        set_caller(STRANGER)
        call_payable(c, "register_juror", 10)
        first = json.loads(c.get_juror(STRANGER))
        call_payable(c, "register_juror", 5)
        again = json.loads(c.get_juror(STRANGER))
        self.assertEqual(again["stake"], "15")
        self.assertEqual(again["registered_at"], first["registered_at"])

    def test_register_floor(self):
        m.ShipSeal.MIN_JUROR_STAKE = m.u256(50)
        try:
            c = make_contract()
            set_caller(STRANGER)
            with self.assertRaises(gl.vm.UserError):
                call_payable(c, "register_juror", 49)
            call_payable(c, "register_juror", 50)
        finally:
            m.ShipSeal.MIN_JUROR_STAKE = m.u256(1)


class EndToEndMoneyFlow(unittest.TestCase):
    def test_deliver_finalize_withdraw(self):
        c = make_contract()
        reset_transfers()
        deal = open_deal(c, value=5000)
        c.balance = m.u256(10000)
        resolve(c, deal, FakePyPI().release().index())
        patch_deal(c, deal, appeal_deadline=iso(-5))
        set_caller(STRANGER)
        c.finalize_unappealed(deal)
        set_caller(DEV)
        c.withdraw()
        self.assertEqual(gl.evm.transfers, [{"to": DEV, "value": 10000}])

    def test_absence_path_pays_the_client(self):
        from _bootstrap import pass_grace
        c = make_contract()
        reset_transfers()
        deal = open_deal(c)
        pass_grace(c, deal)
        resolve(c, deal, FakePyPI().missing().index())
        patch_deal(c, deal, appeal_deadline=iso(-5))
        set_caller(STRANGER)
        c.finalize_unappealed(deal)
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "2000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "0")

    def test_partial_path_splits(self):
        c = make_contract()
        deal = open_deal(c)
        pass_deadline(c, deal)
        resolve(c, deal, FakePyPI().release(release_payload(files=[("sdist", -100, "b" * 64)])).index())
        patch_deal(c, deal, appeal_deadline=iso(-5))
        set_caller(STRANGER)
        c.finalize_unappealed(deal)
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "1000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "1000")

    def test_unresolvable_deal_refunds_both_after_the_timeout(self):
        c = make_contract()
        deal = open_deal(c)
        patch_deal(c, deal, deadline=iso(-(m.ShipSeal.RESOLUTION_TIMEOUT_SECONDS + 5)))
        set_caller(STRANGER)
        c.expire_unresolved(deal)
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "1000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "1000")


if __name__ == "__main__":
    unittest.main()
