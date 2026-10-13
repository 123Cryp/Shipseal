"""Regression tests for issues found in the pre-submission audit."""
import json
import unittest

from _bootstrap import (
    m, gl, make_contract, set_caller, call_payable, open_deal, create_deal, load, resolve, FakePyPI,
    release_payload, index_payload, model, iso, CLIENT, DEV, STRANGER, REPO, JUROR_ADDRESSES,
)


def bloated_release():
    payload = release_payload()
    wheel = payload["urls"][0]
    payload["urls"] = [dict(wheel, filename=("w" * 190) + f"-{i}.whl") for i in range(59)] + [payload["urls"][1]]
    info = payload["info"]
    info["description"] = "\U0001F600" * 5000
    info["summary"] = "\U0001F600" * 400
    info["keywords"] = "\U0001F600" * 400
    info["classifiers"] = ["\U0001F600" * 150] * 60
    info["requires_dist"] = ["\U0001F600" * 250] * 60
    info["project_urls"] = {f"Link {i:02d}": "https://github.com/acme/widget/" + ("x" * 260) for i in range(29)}
    info["project_urls"]["Source"] = REPO
    return payload


class EvidenceSizeBudget(unittest.TestCase):
    def test_extracted_record_always_fits_the_frozen_page_limit(self):
        rec = m._extract_release(json.dumps(bloated_release()))
        page = m._canonical({"status": "ok", "data": rec})
        self.assertLessEqual(len(page), m.MAX_PAGE_CHARS)
        self.assertLessEqual(len(m._canonical(rec)), m.RELEASE_BUDGET_CHARS)
        self.assertEqual(rec["description"], "")
        self.assertEqual(len(rec["files"]), 60)

    def test_small_records_keep_their_text(self):
        rec = m._extract_release(json.dumps(release_payload()))
        self.assertEqual(rec["description"], "Widgets with a command line interface")
        self.assertEqual(rec["summary"], "A widget library")

    def test_a_large_honest_release_still_resolves_and_its_evidence_verifies(self):
        c = make_contract()
        deal = open_deal(c)
        d = resolve(c, deal, FakePyPI().release(bloated_release()).index())
        self.assertEqual(d["tier1_outcome"], "DELIVERED")
        self.assertTrue(m._frozen_intact(d["evidence"], d["evidence_root"]))

    def test_non_ascii_repository_links_are_ignored_not_truncated(self):
        payload = release_payload(project_urls={"Source": REPO + "é"})
        rec = m._extract_release(json.dumps(payload))
        self.assertEqual(rec["repo_urls"], [])

    def test_control_characters_in_filenames_are_neutralised(self):
        payload = release_payload()
        payload["urls"][0]["filename"] = "widget\x00☃.whl"
        rec = m._extract_release(json.dumps(payload))
        self.assertTrue(all(32 <= ord(ch) < 127 for f in rec["files"] for ch in f["filename"]))


class TimestampsWithZ(unittest.TestCase):
    def test_deadline_with_a_trailing_z_is_accepted(self):
        c = make_contract()
        set_caller(CLIENT)
        deadline = iso(7200).replace("+00:00", "Z")
        deal = call_payable(c, "create_deal", 1000, DEV, "scope", "criteria", "widget", "1.0.0", "",
                            "wheel", REPO, deadline)
        self.assertEqual(load(c, deal)["status"], "created")

    def test_parse_iso_handles_z(self):
        self.assertEqual(m.ShipSeal._parse_iso("2030-01-01T00:00:00Z").utcoffset().total_seconds(), 0)


class JurorRegistryBounds(unittest.TestCase):
    def test_unstake_rejects_non_numeric_amounts_with_a_user_error(self):
        c = make_contract()
        set_caller(STRANGER)
        call_payable(c, "register_juror", 10)
        for bad in ("abc", "1.5", ""):
            with self.assertRaises(gl.vm.UserError):
                c.unstake_juror(bad)

    def test_registry_is_capped_but_existing_jurors_can_top_up(self):
        original = m.ShipSeal.MAX_REGISTERED_JURORS
        m.ShipSeal.MAX_REGISTERED_JURORS = 2
        try:
            c = make_contract()
            for addr in JUROR_ADDRESSES[:2]:
                set_caller(addr)
                call_payable(c, "register_juror", 10)
            set_caller(JUROR_ADDRESSES[2])
            with self.assertRaises(gl.vm.UserError):
                call_payable(c, "register_juror", 10)
            set_caller(JUROR_ADDRESSES[0])
            call_payable(c, "register_juror", 5)
            self.assertEqual(json.loads(c.get_juror(JUROR_ADDRESSES[0]))["stake"], "15")
            self.assertEqual(int(c.juror_count), 2)
        finally:
            m.ShipSeal.MAX_REGISTERED_JURORS = original


class JurorRegistrationFee(unittest.TestCase):
    def setUp(self):
        self.original = m.ShipSeal.JUROR_REGISTRATION_FEE
        m.ShipSeal.JUROR_REGISTRATION_FEE = m.u256(100)

    def tearDown(self):
        m.ShipSeal.JUROR_REGISTRATION_FEE = self.original

    def test_first_registration_pays_a_non_refundable_fee_into_the_burn_address(self):
        c = make_contract()
        set_caller(STRANGER)
        call_payable(c, "register_juror", 150)
        rec = json.loads(c.get_juror(STRANGER))
        self.assertEqual(rec["stake"], "50")
        self.assertEqual(c.get_pending_withdrawal(m.BURN_ADDRESS), "100")

    def test_the_fee_is_charged_once_per_address(self):
        c = make_contract()
        set_caller(STRANGER)
        call_payable(c, "register_juror", 150)
        call_payable(c, "register_juror", 7)
        self.assertEqual(json.loads(c.get_juror(STRANGER))["stake"], "57")
        self.assertEqual(c.get_pending_withdrawal(m.BURN_ADDRESS), "100")

    def test_payment_below_fee_plus_minimum_stake_is_rejected(self):
        c = make_contract()
        set_caller(STRANGER)
        for value in (1, 100, 100):
            with self.assertRaises(gl.vm.UserError):
                call_payable(c, "register_juror", value)
        self.assertEqual(c.get_pending_withdrawal(m.BURN_ADDRESS), "0")
        self.assertEqual(int(c.juror_count), 0)

    def test_the_fee_cannot_be_unstaked_or_withdrawn(self):
        c = make_contract()
        c.balance = m.u256(150)
        set_caller(STRANGER)
        call_payable(c, "register_juror", 150)
        with self.assertRaises(gl.vm.UserError):
            c.unstake_juror("51")
        c.unstake_juror("50")
        set_caller(STRANGER)
        c.withdraw()
        self.assertEqual(c.get_pending_withdrawal(m.BURN_ADDRESS), "100")

    def test_config_exposes_the_fee(self):
        self.assertEqual(json.loads(make_contract().get_config())["juror_registration_fee"], "100")


class DocstringsMatchCode(unittest.TestCase):
    def test_no_stale_references(self):
        with open(m.__file__, encoding="utf-8") as fh:
            text = fh.read()
        for stale in ("EvidenceSnapshot", "WHY MAJORITY VOTE ALONE", "v1.1 scope", "Triggers\n        jury selection immediately"):
            self.assertNotIn(stale, text)


class DeveloperCannotDodgeDelivery(unittest.TestCase):
    def finalized_pot(self, payload, grace):
        from _bootstrap import pass_deadline, pass_grace, patch_deal
        c = make_contract()
        deal = open_deal(c)
        (pass_grace if grace else pass_deadline)(c, deal)
        d = resolve(c, deal, FakePyPI().release(payload).index())
        patch_deal(c, deal, appeal_deadline=iso(-5))
        set_caller(STRANGER)
        c.finalize_unappealed(deal)
        return c, d

    def test_publishing_without_the_agreed_repository_link_pays_the_client(self):
        payload = release_payload(files=[("bdist_wheel", -100, "a" * 64), ("sdist", -100, "b" * 64)],
                                  project_urls={"Source": "https://github.com/mallory/other"})
        c, d = self.finalized_pot(payload, grace=False)
        self.assertEqual(d["tier1_outcome"], "NOT_DELIVERED")
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "2000")
        self.assertEqual(c.get_pending_withdrawal(DEV), "0")
        self.assertEqual(c.get_release_claim("widget", "1.0.0"), "")

    def test_uploading_and_then_deleting_every_file_pays_the_client(self):
        payload = release_payload()
        payload["urls"] = []
        c, d = self.finalized_pot(payload, grace=True)
        self.assertEqual(d["tier1_outcome"], "NOT_DELIVERED")
        self.assertEqual(d["tier1_reasons"], ["release_has_no_files"])
        self.assertEqual(c.get_pending_withdrawal(CLIENT), "2000")

    def test_an_emptied_release_cannot_be_resolved_inside_the_grace_period(self):
        from _bootstrap import pass_deadline
        c = make_contract()
        deal = open_deal(c)
        pass_deadline(c, deal)
        payload = release_payload()
        payload["urls"] = []
        set_caller(STRANGER)
        with FakePyPI().release(payload).index().patch(), model("DELIVERED"):
            with self.assertRaises(gl.vm.UserError):
                c.resolve_tier1(deal)
        self.assertEqual(load(c, deal)["status"], "open")

    def test_an_emptied_release_before_the_deadline_cannot_void_the_developer(self):
        c = make_contract()
        deal = open_deal(c)
        payload = release_payload()
        payload["urls"] = []
        set_caller(STRANGER)
        with FakePyPI().release(payload).index().patch(), model("DELIVERED"):
            with self.assertRaises(gl.vm.UserError):
                c.resolve_tier1(deal)
        self.assertEqual(load(c, deal)["status"], "open")


if __name__ == "__main__":
    unittest.main()
