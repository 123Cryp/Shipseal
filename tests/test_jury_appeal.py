import json
import unittest

from _bootstrap import (
    m, gl, make_contract, set_caller, call_payable, reset_transfers, open_deal, load, store, patch_deal, resolve,
    model, FakePyPI, release_payload, age_jurors, draw_jury, commit_hash, iso, CLIENT, DEV, STRANGER,
    JUROR_ADDRESSES, SHA_WHEEL, pass_deadline,
)

DELIVERED_PAYLOAD_FOR_LATE = lambda: release_payload(files=[("bdist_wheel", -100, SHA_WHEEL), ("sdist", -100, "b" * 64)])


class JuryBase(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()

    def register(self, count=7, stake=100):
        for addr in JUROR_ADDRESSES[:count]:
            set_caller(addr)
            call_payable(self.c, "register_juror", stake)
        age_jurors(self.c)

    def delivered_deal(self, **kw):
        deal = open_deal(self.c, **kw)
        resolve(self.c, deal, FakePyPI().release().index())
        return deal

    def appeal(self, deal, who=CLIENT, bond=200):
        set_caller(who)
        call_payable(self.c, "appeal", bond, deal)

    def appealed_deal(self, **kw):
        self.register()
        deal = self.delivered_deal(**kw)
        self.appeal(deal)
        draw_jury(self.c, deal)
        return deal

    def commit_all(self, deal, votes, salt="s"):
        d = load(self.c, deal)
        for addr, vote in zip(d["jury"]["selected_jurors"], votes):
            set_caller(addr)
            self.c.commit_vote(deal, commit_hash(vote, salt, addr, deal))

    def close_commit(self, deal):
        d = load(self.c, deal)
        d["jury"]["commit_deadline"] = iso(-5)
        d["jury"]["reveal_deadline"] = iso(3600)
        store(self.c, deal, d)

    def reveal_all(self, deal, votes, salt="s", skip=()):
        d = load(self.c, deal)
        for i, (addr, vote) in enumerate(zip(d["jury"]["selected_jurors"], votes)):
            if i in skip:
                continue
            set_caller(addr)
            self.c.reveal_vote(deal, vote, salt)

    def close_reveal(self, deal):
        d = load(self.c, deal)
        d["jury"]["commit_deadline"] = iso(-3600)
        d["jury"]["reveal_deadline"] = iso(-5)
        store(self.c, deal, d)

    def finalize(self, deal, answer="DELIVERED"):
        set_caller(STRANGER)
        with FakePyPI().patch(), model(answer):
            self.c.finalize_jury(deal)
        return load(self.c, deal)


class AppealRules(JuryBase):
    def test_appeal_records_bond_pool_and_round(self):
        self.register()
        deal = self.delivered_deal()
        self.appeal(deal)
        d = load(self.c, deal)
        self.assertEqual(d["status"], "appeal_filed")
        self.assertEqual(d["appeal"]["appellant"], CLIENT)
        self.assertEqual(d["appeal"]["bond_amount"], "200")
        self.assertEqual(len(d["appeal"]["eligible_pool"]), 7)
        self.assertIsInstance(d["appeal"]["draw_round"], int)

    def test_only_a_party_may_appeal(self):
        self.register()
        deal = self.delivered_deal()
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "appeal", 200, deal)

    def test_bond_must_be_exact(self):
        self.register()
        deal = self.delivered_deal()
        set_caller(CLIENT)
        for bad in (199, 201, 0):
            with self.assertRaises(gl.vm.UserError):
                call_payable(self.c, "appeal", bad, deal)

    def test_needs_enough_jurors(self):
        self.register(count=4)
        deal = self.delivered_deal()
        set_caller(CLIENT)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "appeal", 200, deal)

    def test_window_must_be_open(self):
        self.register()
        deal = self.delivered_deal()
        patch_deal(self.c, deal, appeal_deadline=iso(-5))
        set_caller(CLIENT)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "appeal", 200, deal)

    def test_parties_are_excluded_from_the_pool(self):
        self.register(count=6)
        for addr in (CLIENT, DEV):
            set_caller(addr)
            call_payable(self.c, "register_juror", 100)
        age_jurors(self.c)
        deal = self.delivered_deal()
        self.assertEqual(self.c.count_eligible_jurors(deal), 6)

    def test_jurors_registered_after_acceptance_are_not_eligible(self):
        self.register(count=5)
        deal = self.delivered_deal()
        set_caller(JUROR_ADDRESSES[8])
        call_payable(self.c, "register_juror", 100)
        self.assertEqual(self.c.count_eligible_jurors(deal), 5)

    def test_stake_top_up_after_acceptance_removes_eligibility(self):
        self.register(count=5)
        deal = self.delivered_deal()
        set_caller(JUROR_ADDRESSES[0])
        call_payable(self.c, "register_juror", 100)
        self.assertEqual(self.c.count_eligible_jurors(deal), 4)

    def test_pool_members_cannot_unstake_until_the_draw(self):
        self.register()
        deal = self.delivered_deal()
        self.appeal(deal)
        set_caller(JUROR_ADDRESSES[0])
        with self.assertRaises(gl.vm.UserError):
            self.c.unstake_juror("10")
        draw_jury(self.c, deal)
        d = load(self.c, deal)
        outsider = [a for a in JUROR_ADDRESSES[:7] if a not in d["jury"]["selected_jurors"]][0]
        set_caller(outsider)
        self.c.unstake_juror("10")

    def test_selected_jurors_stay_locked_until_the_jury_finalises(self):
        deal = self.appealed_deal()
        selected = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(selected)
        with self.assertRaises(gl.vm.UserError):
            self.c.unstake_juror("1")

    def test_unstake_amount_validation(self):
        self.register(count=1)
        set_caller(JUROR_ADDRESSES[0])
        for bad in ("0", "-1", "101"):
            with self.assertRaises(gl.vm.UserError):
                self.c.unstake_juror(bad)
        self.c.unstake_juror("100")
        self.assertEqual(self.c.get_pending_withdrawal(JUROR_ADDRESSES[0]), "100")


class DrawAndReplay(JuryBase):
    def test_draw_picks_five_distinct_pool_members_and_can_be_replayed(self):
        deal = self.appealed_deal()
        d = load(self.c, deal)
        selected = d["jury"]["selected_jurors"]
        self.assertEqual(len(set(selected)), 5)
        self.assertTrue(set(selected) <= set(JUROR_ADDRESSES[:7]))
        self.assertEqual(d["status"], "appealed")
        self.assertTrue(json.loads(self.c.verify_jury_selection(deal))["match"])

    def test_selection_depends_on_the_beacon(self):
        self.register(count=12)
        picks = set()
        for beacon in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
            c = self.c = make_contract()
            self.register(count=12)
            deal = self.delivered_deal()
            self.appeal(deal)
            draw_jury(c, deal, beacon=beacon)
            picks.add(tuple(sorted(load(c, deal)["jury"]["selected_jurors"])))
        self.assertGreater(len(picks), 1)

    def test_draw_before_the_randomness_round_exists_fails(self):
        self.register()
        deal = self.delivered_deal()
        self.appeal(deal)
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.draw_jury(deal)

    def test_draw_fails_without_a_beacon(self):
        self.register()
        deal = self.delivered_deal()
        self.appeal(deal)
        d = load(self.c, deal)
        d["appeal"]["draw_round"] = 1
        store(self.c, deal, d)
        from unittest.mock import patch
        with patch.object(gl.nondet.web, "render", side_effect=RuntimeError("down")):
            with self.assertRaises(gl.vm.UserError):
                self.c.draw_jury(deal)

    def test_draw_rejects_a_beacon_for_the_wrong_round(self):
        self.register()
        deal = self.delivered_deal()
        self.appeal(deal)
        d = load(self.c, deal)
        d["appeal"]["draw_round"] = 1
        store(self.c, deal, d)
        from unittest.mock import patch
        with patch.object(gl.nondet.web, "render", return_value=json.dumps({"round": 2, "randomness": "x"})):
            with self.assertRaises(gl.vm.UserError):
                self.c.draw_jury(deal)

    def test_draw_only_in_appeal_filed_status(self):
        deal = self.appealed_deal()
        with self.assertRaises(gl.vm.UserError):
            draw_jury(self.c, deal)

    def test_tampering_with_the_recorded_jury_is_detected(self):
        deal = self.appealed_deal()
        d = load(self.c, deal)
        d["jury"]["selected_jurors"] = list(reversed(d["jury"]["selected_jurors"]))[:4] + [CLIENT]
        store(self.c, deal, d)
        self.assertFalse(json.loads(self.c.verify_jury_selection(deal))["match"])

    def test_verify_without_a_jury_fails(self):
        deal = self.delivered_deal()
        with self.assertRaises(gl.vm.UserError):
            self.c.verify_jury_selection(deal)


class CommitReveal(JuryBase):
    def test_only_selected_jurors_commit_once_in_the_window(self):
        deal = self.appealed_deal()
        d = load(self.c, deal)
        juror = d["jury"]["selected_jurors"][0]
        h = commit_hash("DELIVERED", "s", juror, deal)
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.commit_vote(deal, h)
        set_caller(juror)
        self.c.commit_vote(deal, h)
        with self.assertRaises(gl.vm.UserError):
            self.c.commit_vote(deal, h)

    def test_commit_must_be_sha256_hex(self):
        deal = self.appealed_deal()
        juror = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(juror)
        for bad in ("abc", "g" * 64, "a" * 63, ""):
            with self.assertRaises(gl.vm.UserError):
                self.c.commit_vote(deal, bad)

    def test_commit_window_closes(self):
        deal = self.appealed_deal()
        self.close_commit(deal)
        juror = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(juror)
        with self.assertRaises(gl.vm.UserError):
            self.c.commit_vote(deal, commit_hash("DELIVERED", "s", juror, deal))

    def test_reveal_needs_a_matching_commit_after_the_commit_window(self):
        deal = self.appealed_deal()
        juror = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(juror)
        self.c.commit_vote(deal, commit_hash("DELIVERED", "s", juror, deal))
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "s")
        self.close_commit(deal)
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "NOT_DELIVERED", "s")
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "other-salt")
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "MAYBE", "s")
        self.c.reveal_vote(deal, "DELIVERED", "s")
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "s")

    def test_a_copied_commit_cannot_be_revealed_by_the_copier(self):
        deal = self.appealed_deal()
        a, b = load(self.c, deal)["jury"]["selected_jurors"][:2]
        h = commit_hash("DELIVERED", "s", a, deal)
        set_caller(a)
        self.c.commit_vote(deal, h)
        set_caller(b)
        self.c.commit_vote(deal, h)
        self.close_commit(deal)
        set_caller(b)
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "s")

    def test_a_commit_is_bound_to_the_case(self):
        deal = self.appealed_deal()
        juror = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(juror)
        self.c.commit_vote(deal, commit_hash("DELIVERED", "s", juror, "deal_999"))
        self.close_commit(deal)
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "s")

    def test_salt_length_is_bounded(self):
        deal = self.appealed_deal()
        juror = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(juror)
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "x" * 257)

    def test_reveal_window_closes(self):
        deal = self.appealed_deal()
        juror = load(self.c, deal)["jury"]["selected_jurors"][0]
        set_caller(juror)
        self.c.commit_vote(deal, commit_hash("DELIVERED", "s", juror, deal))
        self.close_reveal(deal)
        with self.assertRaises(gl.vm.UserError):
            self.c.reveal_vote(deal, "DELIVERED", "s")


class FinalizeJury(JuryBase):
    def run_round(self, votes, answer="DELIVERED", skip=()):
        deal = self.appealed_deal()
        self.commit_all(deal, votes)
        self.close_commit(deal)
        self.reveal_all(deal, votes, skip=skip)
        self.close_reveal(deal)
        return deal, self.finalize(deal, answer)

    def test_cannot_finalize_before_the_reveal_window_closes(self):
        deal = self.appealed_deal()
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.finalize_jury(deal)

    def test_agreeing_verified_majority_forfeits_the_bond_to_the_majority(self):
        deal, d = self.run_round(["DELIVERED"] * 5)
        self.assertEqual((d["status"], d["final_outcome"]), ("finalized", "DELIVERED"))
        self.assertTrue(d["jury"]["verification_passed"])
        self.assertEqual(d["jury"]["provisional_outcome"], "DELIVERED")
        for addr in d["jury"]["selected_jurors"]:
            self.assertEqual(self.c.get_pending_withdrawal(addr), "40")
        self.assertEqual(self.c.get_pending_withdrawal(DEV), "2000")
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "0")

    def test_majority_that_the_evidence_does_not_support_is_not_enforced(self):
        deal, d = self.run_round(["NOT_DELIVERED"] * 5)
        self.assertFalse(d["jury"]["verification_passed"])
        self.assertEqual(d["final_outcome"], "DELIVERED")
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "200")
        for addr in d["jury"]["selected_jurors"]:
            rec = json.loads(self.c.get_juror(addr))
            self.assertEqual(rec["stake"], "100")
            self.assertEqual(rec["minority_votes"], 0)

    def test_a_tie_or_split_holds_the_tier1_outcome_and_refunds_the_bond(self):
        deal, d = self.run_round(["DELIVERED", "DELIVERED", "NOT_DELIVERED", "NOT_DELIVERED", "UNDETERMINED"])
        self.assertIsNone(d["jury"]["provisional_outcome"])
        self.assertEqual(d["final_outcome"], "DELIVERED")
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "200")

    def test_minority_voters_are_slashed_only_when_the_majority_is_verified(self):
        deal, d = self.run_round(["DELIVERED", "DELIVERED", "DELIVERED", "NOT_DELIVERED", "UNDETERMINED"])
        sel = d["jury"]["selected_jurors"]
        self.assertEqual(json.loads(self.c.get_juror(sel[3]))["stake"], "90")
        self.assertEqual(json.loads(self.c.get_juror(sel[4]))["stake"], "90")
        self.assertEqual(json.loads(self.c.get_juror(sel[0]))["reputation"], 2)
        self.assertEqual(json.loads(self.c.get_juror(sel[3]))["reputation"], -2)
        # bond 200 + 2 * 10 slashed, shared by the three majority jurors
        total = sum(int(self.c.get_pending_withdrawal(a)) for a in sel[:3])
        self.assertEqual(total, 220)

    def test_non_revealers_are_slashed_even_without_a_verified_majority(self):
        deal, d = self.run_round(["DELIVERED"] * 5, skip=(3, 4))
        sel = d["jury"]["selected_jurors"]
        # 3 of 5 reveal: still a majority, verified; both non-revealers lose 15
        self.assertEqual(json.loads(self.c.get_juror(sel[3]))["stake"], "85")
        self.assertEqual(json.loads(self.c.get_juror(sel[3]))["non_reveals"], 1)

    def test_nobody_revealing_pays_the_slash_to_the_appellant(self):
        deal, d = self.run_round(["DELIVERED"] * 5, skip=(0, 1, 2, 3, 4))
        self.assertEqual(d["final_outcome"], "DELIVERED")
        # bond refunded (200) plus 5 * 15 slashed stake
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), str(200 + 75))

    def test_jurors_are_unlocked_afterwards(self):
        deal, d = self.run_round(["DELIVERED"] * 5)
        for addr in d["jury"]["selected_jurors"]:
            self.assertEqual(json.loads(self.c.get_juror(addr))["locked_cases"], 0)

    def test_finalize_only_once(self):
        deal, d = self.run_round(["DELIVERED"] * 5)
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.finalize_jury(deal)

    def test_value_is_conserved(self):
        deal, d = self.run_round(["DELIVERED", "DELIVERED", "DELIVERED", "NOT_DELIVERED", "UNDETERMINED"])
        owed = sum(int(v) for v in self.c.pending_withdrawals.values())
        staked = sum(int(json.loads(rec)["stake"]) for rec in self.c.jurors.values())
        deposits = 1000 + 1000 + 200 + 7 * 100
        self.assertEqual(owed + staked, deposits)


class TierTwoVerification(JuryBase):
    """The jury can only be backed by what an independent re-derivation of the
    frozen evidence says."""

    def frozen_deal(self, payload, tier1_answer="DELIVERED", **kw):
        self.register()
        deal = open_deal(self.c, **kw)
        pass_deadline(self.c, deal)
        resolve(self.c, deal, FakePyPI().release(payload).index(), answer=tier1_answer)
        return deal

    def verify(self, deal, majority, answer="DELIVERED"):
        d = load(self.c, deal)
        with model(answer):
            result = self.c._verify_majority_against_evidence(d, majority)
        store(self.c, deal, d)
        return result

    def test_matching_majority_is_verified(self):
        deal = self.frozen_deal(DELIVERED_PAYLOAD_FOR_LATE())
        self.assertTrue(self.verify(deal, "DELIVERED"))

    def test_a_different_majority_is_not_verified(self):
        deal = self.frozen_deal(DELIVERED_PAYLOAD_FOR_LATE())
        for wrong in ("NOT_DELIVERED", "PARTIALLY_DELIVERED", "UNDETERMINED"):
            self.assertFalse(self.verify(deal, wrong), wrong)

    def test_the_model_cannot_conjure_a_delivery_from_a_failed_structure(self):
        deal = self.frozen_deal(DELIVERED_PAYLOAD_FOR_LATE() | {"info": {**DELIVERED_PAYLOAD_FOR_LATE()["info"], "yanked": True}})
        self.assertFalse(self.verify(deal, "DELIVERED", answer="DELIVERED"))
        self.assertTrue(self.verify(deal, "NOT_DELIVERED", answer="DELIVERED"))

    def test_tampered_frozen_evidence_fails_verification(self):
        deal = self.frozen_deal(DELIVERED_PAYLOAD_FOR_LATE())
        d = load(self.c, deal)
        d["evidence"][0]["content"] = d["evidence"][0]["content"].replace("widget", "gadget")
        store(self.c, deal, d)
        self.assertFalse(self.verify(deal, "DELIVERED"))

    def test_missing_evidence_fails_verification(self):
        self.register()
        deal = open_deal(self.c)
        self.assertFalse(self.verify(deal, "DELIVERED"))

    def test_a_verified_delivery_majority_needs_an_unclaimed_release(self):
        deal = self.frozen_deal(DELIVERED_PAYLOAD_FOR_LATE())
        self.c.release_claims["widget@1"] = "deal_77"
        self.assertFalse(self.verify(deal, "DELIVERED"))

    def test_a_partial_majority_is_verified_from_frozen_facts(self):
        payload = release_payload(files=[("bdist_wheel", -100, SHA_WHEEL)])
        deal = self.frozen_deal(payload)
        self.assertTrue(self.verify(deal, "PARTIALLY_DELIVERED"))
        self.assertFalse(self.verify(deal, "DELIVERED"))

    def test_absence_proof_is_verified_from_frozen_facts(self):
        self.register()
        deal = open_deal(self.c)
        from _bootstrap import pass_grace
        pass_grace(self.c, deal)
        resolve(self.c, deal, FakePyPI().missing().index())
        self.assertTrue(self.verify(deal, "NOT_DELIVERED"))
        self.assertFalse(self.verify(deal, "UNDETERMINED"))

    def test_non_delivery_cannot_be_voted_in_for_an_early_resolution(self):
        self.register()
        deal = open_deal(self.c)
        resolve(self.c, deal, FakePyPI().release().index())
        self.assertFalse(self.verify(deal, "NOT_DELIVERED"))
        self.assertTrue(self.verify(deal, "DELIVERED"))


class StuckAppealRecovery(JuryBase):
    def test_filed_appeal_that_is_never_drawn_is_recovered(self):
        self.register()
        deal = self.delivered_deal()
        self.appeal(deal)
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_stuck_appeal(deal)
        patch_deal(self.c, deal, appeal_deadline=iso(-(m.ShipSeal.RESOLUTION_TIMEOUT_SECONDS + 10)))
        self.c.expire_stuck_appeal(deal)
        d = load(self.c, deal)
        self.assertEqual((d["status"], d["final_outcome"]), ("finalized", "DELIVERED"))
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "200")
        self.assertEqual(self.c.get_pending_withdrawal(DEV), "2000")
        set_caller(JUROR_ADDRESSES[0])
        self.c.unstake_juror("100")

    def test_drawn_jury_that_never_finalises_is_recovered(self):
        deal = self.appealed_deal()
        d = load(self.c, deal)
        d["jury"]["reveal_deadline"] = iso(-(m.ShipSeal.RESOLUTION_TIMEOUT_SECONDS + 10))
        store(self.c, deal, d)
        set_caller(STRANGER)
        self.c.expire_stuck_appeal(deal)
        d = load(self.c, deal)
        self.assertEqual(d["status"], "finalized")
        for addr in d["jury"]["selected_jurors"]:
            self.assertEqual(json.loads(self.c.get_juror(addr))["locked_cases"], 0)
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), str(200 + 75))

    def test_recovery_is_not_available_in_other_states(self):
        deal = self.delivered_deal()
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_stuck_appeal(deal)


if __name__ == "__main__":
    unittest.main()
