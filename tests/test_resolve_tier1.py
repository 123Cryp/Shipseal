import copy
import json
import unittest
from unittest.mock import patch

from _bootstrap import (
    m, gl, make_contract, set_caller, reset_transfers, open_deal, load, patch_deal, pass_deadline, pass_grace,
    resolve, model, FakePyPI, release_payload, index_payload, stamp, iso, CLIENT, DEV, STRANGER, SHA_WHEEL, REPO,
)


def after_deadline_release(offset_in_window=-100, **kw):
    """A release payload whose files were uploaded inside the (already moved) window."""
    return release_payload(files=[("bdist_wheel", offset_in_window, SHA_WHEEL), ("sdist", offset_in_window, "b" * 64)], **kw)


class DeliveredPath(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()
        self.id = open_deal(self.c)

    def test_proven_delivery_before_the_deadline_is_recorded(self):
        d = resolve(self.c, self.id, FakePyPI().release().index())
        self.assertEqual(d["status"], "tier1_resolved")
        self.assertEqual(d["tier1_outcome"], "DELIVERED")
        self.assertEqual(d["tier1_verdict"], "DELIVERED_OK")
        self.assertEqual(d["tier1_reasons"], ["structured_checks_passed"])
        self.assertTrue(d["tier1_checks"]["identity"] and d["tier1_checks"]["repo"] and d["tier1_checks"]["kinds"])
        self.assertIn("Every structured check passed", d["tier1_summary"])

    def test_anyone_can_resolve(self):
        for who in (CLIENT, DEV, STRANGER):
            c = make_contract()
            deal = open_deal(c)
            d = resolve(c, deal, FakePyPI().release().index(), caller=who)
            self.assertEqual(d["tier1_outcome"], "DELIVERED")

    def test_frozen_evidence_and_root_are_stored(self):
        d = resolve(self.c, self.id, FakePyPI().release().index())
        self.assertEqual(len(d["evidence"]), 2)
        self.assertEqual(d["evidence"][0]["url"], "https://pypi.org/pypi/widget/1.0.0/json")
        self.assertEqual(d["evidence"][1]["url"], "https://pypi.org/pypi/widget/json")
        self.assertTrue(m._frozen_intact(d["evidence"], d["evidence_root"]))
        self.assertEqual(d["tier1_resolved_at"][:4], iso()[:4])

    def test_appeal_deadline_is_set(self):
        d = resolve(self.c, self.id, FakePyPI().release().index())
        self.assertIn("appeal_deadline", d)

    def test_release_is_claimed_for_this_deal(self):
        resolve(self.c, self.id, FakePyPI().release().index())
        self.assertEqual(self.c.get_release_claim("Widget", "1.0.0"), self.id)
        self.assertEqual(self.c.get_release_claim("widget", "1.0"), self.id)

    def test_only_an_open_deal_can_be_resolved(self):
        resolve(self.c, self.id, FakePyPI().release().index())
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, FakePyPI().release().index())
        created = make_contract()
        from _bootstrap import create_deal
        cid = create_deal(created)
        with self.assertRaises(gl.vm.UserError):
            resolve(created, cid, FakePyPI().release().index())

    def test_the_model_is_consulted_with_untrusted_data_framing(self):
        fake = FakePyPI().release().index()
        set_caller(STRANGER)
        with fake.patch(), model("DELIVERED") as ex:
            self.c.resolve_tier1(self.id)
        prompt = ex.call_args[0][0]
        self.assertIn("UNTRUSTED DATA", prompt)
        self.assertIn("DELIVERY CRITERIA", prompt)
        self.assertIn("command line interface", prompt)
        self.assertEqual(ex.call_args[1], {"response_format": "json"})


class ModelVetoAndBackstop(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()
        self.id = open_deal(self.c)

    def test_veto_before_the_deadline_reverts_and_leaves_the_deal_open(self):
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, FakePyPI().release().index(), answer="UNDETERMINED")
        self.assertEqual(load(self.c, self.id)["status"], "open")
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), "")

    def test_veto_after_the_deadline_is_recorded_as_undetermined(self):
        pass_deadline(self.c, self.id)
        d = resolve(self.c, self.id, FakePyPI().release(after_deadline_release()).index(), answer="UNDETERMINED")
        self.assertEqual(d["tier1_outcome"], "UNDETERMINED")
        self.assertEqual(d["tier1_reasons"], ["criteria_not_confirmed"])

    def test_model_saying_not_delivered_cannot_award_the_pot(self):
        pass_deadline(self.c, self.id)
        d = resolve(self.c, self.id, FakePyPI().release(after_deadline_release()).index(), answer="NOT_DELIVERED")
        self.assertEqual(d["tier1_outcome"], "UNDETERMINED")

    def test_model_garbage_is_a_veto_not_a_crash(self):
        pass_deadline(self.c, self.id)
        fake = FakePyPI().release(after_deadline_release()).index()
        set_caller(STRANGER)
        for garbage in ("not json", None, 42, {"outcome": 7}, {"nothing": 1}, {"outcome": "delivered"}):
            c = make_contract()
            deal = open_deal(c)
            pass_deadline(c, deal)
            with fake.patch(), patch.object(gl.nondet, "exec_prompt", return_value=garbage):
                d = json.loads(c.resolve_tier1(deal))
            self.assertEqual(d["tier1_outcome"], "UNDETERMINED", garbage)

    def test_model_failure_is_transient_and_reverts(self):
        pass_deadline(self.c, self.id)
        fake = FakePyPI().release(after_deadline_release()).index()
        set_caller(STRANGER)
        with fake.patch(), patch.object(gl.nondet, "exec_prompt", side_effect=RuntimeError("model down")):
            with self.assertRaises(gl.vm.UserError):
                self.c.resolve_tier1(self.id)
        self.assertEqual(load(self.c, self.id)["status"], "open")

    def test_model_claiming_delivered_cannot_override_a_failed_structure(self):
        pass_deadline(self.c, self.id)
        payload = after_deadline_release(repo="https://github.com/mallory/widget")
        fake = FakePyPI().release(payload).index()
        set_caller(STRANGER)
        with fake.patch(), model("DELIVERED") as ex:
            d = json.loads(self.c.resolve_tier1(self.id))
        self.assertEqual(d["tier1_outcome"], "NOT_DELIVERED")
        self.assertEqual(d["tier1_reasons"], ["repo_unverified"])
        ex.assert_not_called()

    def test_the_model_is_not_consulted_when_the_structure_already_decides(self):
        pass_deadline(self.c, self.id)
        payload = after_deadline_release(yanked=True)
        fake = FakePyPI().release(payload).index()
        set_caller(STRANGER)
        with fake.patch(), model("DELIVERED") as ex:
            d = json.loads(self.c.resolve_tier1(self.id))
        self.assertEqual(d["tier1_outcome"], "NOT_DELIVERED")
        ex.assert_not_called()


class NonDeliveredPaths(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()
        self.id = open_deal(self.c)

    def test_partial_after_the_deadline(self):
        pass_deadline(self.c, self.id)
        payload = release_payload(files=[("bdist_wheel", -100, SHA_WHEEL)])
        d = resolve(self.c, self.id, FakePyPI().release(payload).index())
        self.assertEqual(d["tier1_outcome"], "PARTIALLY_DELIVERED")
        self.assertEqual(d["tier1_reasons"], ["kind_missing:sdist"])
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), self.id)

    def test_partial_before_the_deadline_reverts(self):
        payload = release_payload(files=[("bdist_wheel", 0, SHA_WHEEL)])
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, FakePyPI().release(payload).index())
        self.assertEqual(load(self.c, self.id)["status"], "open")
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), "")

    def test_late_publication_after_the_deadline(self):
        pass_deadline(self.c, self.id, seconds_past=600)
        payload = release_payload(files=[("bdist_wheel", 0, SHA_WHEEL), ("sdist", 0, "b" * 64)])
        d = resolve(self.c, self.id, FakePyPI().release(payload).index())
        self.assertEqual((d["tier1_outcome"], d["tier1_reasons"]), ("NOT_DELIVERED", ["published_late"]))
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), "")

    def test_release_that_predates_the_deal_is_undetermined_not_a_win_for_the_client(self):
        pass_deadline(self.c, self.id)
        old = release_payload(files=[("bdist_wheel", -999999, SHA_WHEEL), ("sdist", -999999, "b" * 64)])
        d = resolve(self.c, self.id, FakePyPI().release(old).index())
        self.assertEqual((d["tier1_outcome"], d["tier1_reasons"]), ("UNDETERMINED", ["release_predates_deal"]))

    def test_yanked_after_the_deadline(self):
        pass_deadline(self.c, self.id)
        d = resolve(self.c, self.id, FakePyPI().release(after_deadline_release(yanked=True)).index())
        self.assertEqual(d["tier1_outcome"], "NOT_DELIVERED")

    def test_sha_mismatch_after_the_deadline(self):
        c = make_contract()
        deal = open_deal(c, sha="c" * 64)
        pass_deadline(c, deal)
        d = resolve(c, deal, FakePyPI().release(after_deadline_release()).index())
        self.assertEqual((d["tier1_outcome"], d["tier1_reasons"]), ("NOT_DELIVERED", ["sha256_mismatch"]))

    def test_sha_match_delivers(self):
        c = make_contract()
        deal = open_deal(c, sha=SHA_WHEEL)
        d = resolve(c, deal, FakePyPI().release().index())
        self.assertEqual(d["tier1_outcome"], "DELIVERED")

    def test_wrong_repo_is_not_delivered(self):
        pass_deadline(self.c, self.id)
        d = resolve(self.c, self.id, FakePyPI().release(after_deadline_release(repo="https://github.com/x/y")).index())
        self.assertEqual(d["tier1_outcome"], "NOT_DELIVERED")

    def test_wrong_version_returned_by_pypi_is_undetermined(self):
        pass_deadline(self.c, self.id)
        d = resolve(self.c, self.id, FakePyPI().release(after_deadline_release(ver="1.0")).index())
        self.assertEqual((d["tier1_outcome"], d["tier1_reasons"]), ("UNDETERMINED", ["identity_mismatch"]))

    def test_delivered_claim_requires_every_check_even_with_a_friendly_model(self):
        pass_deadline(self.c, self.id)
        for mutate in (lambda p: p["info"].update(yanked=True),
                       lambda p: p["info"].update(name="other"),
                       lambda p: p["info"].update(project_urls={}),
                       lambda p: p["urls"].pop()):
            c = make_contract()
            deal = open_deal(c)
            pass_deadline(c, deal)
            payload = after_deadline_release()
            mutate(payload)
            d = resolve(c, deal, FakePyPI().release(payload).index(), answer="DELIVERED")
            self.assertNotEqual(d["tier1_outcome"], "DELIVERED")


class AbsenceEvidencePaths(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()
        self.id = open_deal(self.c)

    def test_404_with_an_index_lacking_the_version_after_grace_is_not_delivered(self):
        pass_grace(self.c, self.id)
        fake = FakePyPI().missing().index(index_payload(versions=("0.1.0", "0.9.0")))
        d = resolve(self.c, self.id, fake)
        self.assertEqual((d["tier1_outcome"], d["tier1_reasons"]), ("NOT_DELIVERED", ["absent_proven"]))
        self.assertTrue(d["tier1_checks"]["absence"])
        # leader and validator each fetch the same two endpoints, in order
        self.assertEqual(fake.calls, [fake.release_url, fake.index_url] * 2)

    def test_absence_is_never_accepted_before_the_deadline(self):
        fake = FakePyPI().missing().index()
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, fake)
        self.assertEqual(load(self.c, self.id)["status"], "open")

    def test_absence_is_not_accepted_inside_the_grace_period(self):
        pass_deadline(self.c, self.id, seconds_past=600)
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, FakePyPI().missing().index())
        self.assertEqual(load(self.c, self.id)["status"], "open")

    def test_a_typo_cannot_produce_not_delivered(self):
        pass_grace(self.c, self.id)
        fake = FakePyPI().missing().index(status=404, payload="Not Found")
        d = resolve(self.c, self.id, fake)
        self.assertEqual((d["tier1_outcome"], d["tier1_reasons"]), ("UNDETERMINED", ["project_not_found"]))

    def test_an_outage_on_the_version_endpoint_reverts(self):
        pass_grace(self.c, self.id)
        fake = FakePyPI().release("x", status=503).index()
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, fake)
        self.assertEqual(load(self.c, self.id)["status"], "open")

    def test_a_network_failure_reverts(self):
        pass_grace(self.c, self.id)
        fake = FakePyPI().release("x", status=None)
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, fake)

    def test_404_with_a_broken_index_reverts(self):
        pass_grace(self.c, self.id)
        for index in (FakePyPI().missing().index("x", status=502), FakePyPI().missing().index("<html>", status=200)):
            with self.assertRaises(gl.vm.UserError):
                resolve(self.c, self.id, index)

    def test_404_while_the_index_lists_the_version_reverts_as_inconsistent(self):
        pass_grace(self.c, self.id)
        fake = FakePyPI().missing().index(index_payload(versions=("1.0.0",)))
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, fake)

    def test_404_while_the_index_lists_an_equivalent_version_reverts(self):
        pass_grace(self.c, self.id)
        fake = FakePyPI().missing().index(index_payload(versions=("1.0",)))
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.id, fake)

    def test_index_of_another_project_is_undetermined(self):
        pass_grace(self.c, self.id)
        d = resolve(self.c, self.id, FakePyPI().missing().index(index_payload(name="other")))
        self.assertEqual(d["tier1_outcome"], "UNDETERMINED")

    def test_an_outage_never_finalises_the_deal(self):
        pass_grace(self.c, self.id)
        for status in (500, 502, 503, 429, None):
            with self.assertRaises(gl.vm.UserError):
                resolve(self.c, self.id, FakePyPI().release("x", status=status))
        self.assertEqual(load(self.c, self.id)["status"], "open")

    def test_no_release_claim_is_made_for_absence(self):
        pass_grace(self.c, self.id)
        resolve(self.c, self.id, FakePyPI().missing().index())
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), "")


class ReleaseClaimRegistry(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()

    def test_one_release_settles_only_one_deal(self):
        first = open_deal(self.c)
        second = open_deal(self.c)
        a = resolve(self.c, first, FakePyPI().release().index())
        self.assertEqual(a["tier1_outcome"], "DELIVERED")
        pass_deadline(self.c, second)
        b = resolve(self.c, second, FakePyPI().release(after_deadline_release()).index())
        self.assertEqual((b["tier1_outcome"], b["tier1_reasons"]), ("UNDETERMINED", ["release_already_claimed"]))
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), first)

    def test_a_second_claim_before_the_deadline_reverts(self):
        first = open_deal(self.c)
        second = open_deal(self.c)
        resolve(self.c, first, FakePyPI().release().index())
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, second, FakePyPI().release().index())
        self.assertEqual(load(self.c, second)["status"], "open")

    def test_partial_also_consumes_the_release(self):
        first = open_deal(self.c)
        pass_deadline(self.c, first)
        payload = release_payload(files=[("bdist_wheel", -100, SHA_WHEEL)])
        resolve(self.c, first, FakePyPI().release(payload).index())
        second = open_deal(self.c)
        pass_deadline(self.c, second)
        d = resolve(self.c, second, FakePyPI().release(payload).index())
        self.assertEqual(d["tier1_outcome"], "UNDETERMINED")

    def test_different_versions_and_packages_do_not_collide(self):
        a = open_deal(self.c)
        b = open_deal(self.c, version="1.0.1")
        c3 = open_deal(self.c, package="gadget", repo=REPO)
        self.assertEqual(resolve(self.c, a, FakePyPI().release().index())["tier1_outcome"], "DELIVERED")
        self.assertEqual(resolve(self.c, b, FakePyPI("widget", "1.0.1").release(release_payload("widget", "1.0.1")).index())["tier1_outcome"], "DELIVERED")
        self.assertEqual(resolve(self.c, c3, FakePyPI("gadget", "1.0.0").release(release_payload("gadget", "1.0.0")).index())["tier1_outcome"], "DELIVERED")

    def test_equivalent_spellings_of_the_name_share_one_claim(self):
        a = open_deal(self.c, package="My_Widget")
        b = open_deal(self.c, package="my-widget")
        fake = FakePyPI("my-widget", "1.0.0").release(release_payload("my-widget", "1.0.0")).index()
        self.assertEqual(resolve(self.c, a, fake)["tier1_outcome"], "DELIVERED")
        pass_deadline(self.c, b)
        fake2 = FakePyPI("my-widget", "1.0.0").release(release_payload("my-widget", "1.0.0", files=[("bdist_wheel", -100, SHA_WHEEL), ("sdist", -100, "b" * 64)])).index()
        self.assertEqual(resolve(self.c, b, fake2)["tier1_outcome"], "UNDETERMINED")

    def test_the_claim_is_released_when_the_final_outcome_is_not_a_delivery(self):
        deal = open_deal(self.c)
        resolve(self.c, deal, FakePyPI().release().index())
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), deal)
        patch_deal(self.c, deal, final_outcome="NOT_DELIVERED", payout_settled=False)
        d = load(self.c, deal)
        self.c._settle(deal, d)
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), "")

    def test_the_claim_stays_when_the_final_outcome_is_delivered(self):
        deal = open_deal(self.c)
        resolve(self.c, deal, FakePyPI().release().index())
        set_caller(STRANGER)
        patch_deal(self.c, deal, appeal_deadline=iso(-5))
        self.c.finalize_unappealed(deal)
        self.assertEqual(self.c.get_release_claim("widget", "1.0.0"), deal)


class ConsensusAgreement(unittest.TestCase):
    """The validator accepts a leader result only when it re-derives the same
    decisive fields and the same evidence manifest."""

    def setUp(self):
        self.spec = {"package": "widget", "version": "1.0.0", "required_kinds": ["sdist", "wheel"],
                     "expected_sha256": "", "expected_repo": "github.com/acme/widget",
                     "created_ts": m._to_epoch(iso(-60)), "deadline_ts": m._to_epoch(iso(7200))}
        fake = FakePyPI().release().index()
        with fake.patch(), model("DELIVERED"):
            leader, validator = m._tier1_fns(self.spec, "scope", "criteria", m._to_epoch(iso()))
            self.result = leader()
        self.fake = fake

    def derive(self, mutate_payload=None, answer="DELIVERED"):
        payload = release_payload()
        if mutate_payload:
            mutate_payload(payload)
        fake = FakePyPI().release(payload).index()
        with fake.patch(), model(answer):
            leader, _ = m._tier1_fns(self.spec, "scope", "criteria", m._to_epoch(iso()))
            return leader()

    def test_identical_results_agree_and_reasoning_may_differ(self):
        mine = self.derive()
        self.assertTrue(m._results_agree(self.result, mine))
        other = copy.deepcopy(self.result)
        other["reasoning"] = "worded differently"
        self.assertTrue(m._results_agree(other, mine))

    def test_each_decisive_field_must_match(self):
        mine = self.derive()
        for key, value in (("verdict", "UNDETERMINED"), ("outcome", "UNDETERMINED"), ("reasons", ["x"]),
                           ("checks", {"identity": False})):
            forged = copy.deepcopy(self.result)
            forged[key] = value
            self.assertFalse(m._results_agree(forged, mine), key)

    def test_a_different_model_answer_disagrees(self):
        self.assertFalse(m._results_agree(self.result, self.derive(answer="UNDETERMINED")))

    def test_different_evidence_disagrees(self):
        mine = self.derive(lambda p: p["info"].update(summary="something else"))
        self.assertFalse(m._results_agree(self.result, mine))

    def test_leader_cannot_store_content_its_manifest_does_not_describe(self):
        mine = self.derive()
        forged = copy.deepcopy(self.result)
        forged["pages"][0]["content"] = forged["pages"][0]["content"].replace("widget", "gadget")
        self.assertFalse(m._results_agree(forged, mine))

    def test_oversized_reasoning_and_non_dicts_are_rejected(self):
        mine = self.derive()
        forged = copy.deepcopy(self.result)
        forged["reasoning"] = "x" * 2001
        self.assertFalse(m._results_agree(forged, mine))
        self.assertFalse(m._results_agree(None, mine))
        self.assertFalse(m._results_agree(self.result, "nope"))

    def test_volatile_pypi_fields_do_not_break_consensus(self):
        def churn(p):
            p["last_serial"] = 99999999
            for entry in p["urls"]:
                entry["downloads"] = 4242
        self.assertTrue(m._results_agree(self.result, self.derive(churn)))

    def test_validator_wraps_the_leader_value_in_gl_vm_return(self):
        with self.fake.patch(), model("DELIVERED"):
            leader, validator = m._tier1_fns(self.spec, "scope", "criteria", m._to_epoch(iso()))
            self.assertTrue(validator(gl.vm.Return(leader())))
            self.assertFalse(validator(leader()))

    def test_validator_rejects_a_forged_leader_outcome(self):
        with self.fake.patch(), model("DELIVERED"):
            leader, validator = m._tier1_fns(self.spec, "scope", "criteria", m._to_epoch(iso()))
            forged = leader()
            forged["outcome"] = "NOT_DELIVERED"
            self.assertFalse(validator(gl.vm.Return(forged)))


if __name__ == "__main__":
    unittest.main()
