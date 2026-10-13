import json
import unittest

from _bootstrap import (
    m, gl, make_contract, set_caller, call_payable, reset_transfers, create_deal, open_deal, load, patch_deal,
    iso, CLIENT, DEV, STRANGER, REPO, SHA_WHEEL,
)


class CreationValidation(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()

    def bad(self, **kw):
        with self.assertRaises(gl.vm.UserError, msg=str(kw)):
            create_deal(self.c, **kw)
        self.assertEqual(self.c.total_deals(), 0)

    def test_happy_path_records_every_anchor_on_chain(self):
        deal_id = create_deal(self.c, sha=SHA_WHEEL.upper(), kinds="Sdist, wheel ", package="My_Widget", version="2.0.0rc1")
        d = load(self.c, deal_id)
        self.assertEqual(d["status"], "created")
        self.assertEqual(d["party_a"], CLIENT)
        self.assertEqual(d["party_b"], DEV)
        self.assertEqual(d["package"], "My_Widget")
        self.assertEqual(d["package_normalized"], "my-widget")
        self.assertEqual(d["version"], "2.0.0rc1")
        self.assertEqual(d["expected_sha256"], SHA_WHEEL)
        self.assertEqual(d["required_kinds"], ["sdist", "wheel"])
        self.assertEqual(d["expected_repo"], "github.com/acme/widget")
        self.assertEqual(d["allowed_evidence_host"], "pypi.org")
        self.assertEqual(d["evidence_urls"]["release"], "https://pypi.org/pypi/my-widget/2.0.0rc1/json")
        self.assertEqual(d["evidence_urls"]["index"], "https://pypi.org/pypi/my-widget/json")
        self.assertEqual(d["stake_amount"], "1000")
        self.assertEqual(self.c.total_deals(), 1)

    def test_ids_increment(self):
        self.assertEqual(create_deal(self.c), "deal_0")
        self.assertEqual(create_deal(self.c), "deal_1")

    def test_stake_floor(self):
        m.ShipSeal.MIN_STAKE = m.u256(500)
        try:
            self.bad(value=499)
            self.assertEqual(create_deal(self.c, value=500), "deal_0")
        finally:
            m.ShipSeal.MIN_STAKE = m.u256(1)

    def test_party_b_must_differ_and_be_an_address(self):
        self.bad(party_b=CLIENT)
        with self.assertRaises(ValueError):
            create_deal(self.c, party_b="not-an-address")

    def test_deadline_bounds(self):
        self.bad(deadline_offset=60)
        self.bad(deadline_offset=91 * 24 * 3600)
        create_deal(self.c, deadline_offset=3601 + 5)
        create_deal(self.c, deadline_offset=89 * 24 * 3600)

    def test_deadline_must_parse(self):
        set_caller(CLIENT)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "create_deal", 1000, DEV, "s", "c", "widget", "1.0.0", "", "wheel", REPO, "soon")

    def test_text_fields_required_and_bounded(self):
        self.bad(scope="   ")
        self.bad(criteria="")
        self.bad(scope="x" * 2001)
        self.bad(criteria="x" * 2001)
        create_deal(self.c, scope="x" * 2000, criteria="y" * 2000)

    def test_package_name_rules(self):
        for name in ("", "-bad", "bad-", "has space", "slash/name", "a" * 101, "q?x", "a@b"):
            self.bad(package=name)
        create_deal(self.c, package="a")
        create_deal(self.c, package="Zope.Interface")

    def test_version_must_be_canonical(self):
        for version in ("", "v1.0", "1.0.0-beta", "1.0.0+local", "01.0", "1..0", "1.0/../x", "latest", "1.0 0", "x" * 70):
            self.bad(version=version)
        for version in ("0", "1.0", "1.2.3", "2.0.0rc1", "1.0.post1", "1.0.dev3", "1.0a1.post2.dev1"):
            create_deal(self.c, version=version)

    def test_sha_must_be_empty_or_64_hex(self):
        self.bad(sha="abc")
        self.bad(sha="g" * 64)
        self.bad(sha="a" * 63)
        create_deal(self.c, sha="")
        create_deal(self.c, sha="A" * 64)
        self.assertEqual(load(self.c, "deal_1")["expected_sha256"], "a" * 64)

    def test_required_kinds_rules(self):
        self.bad(kinds="")
        self.bad(kinds="egg")
        self.bad(kinds="wheel,egg")
        create_deal(self.c, kinds="wheel")
        create_deal(self.c, kinds="sdist")
        create_deal(self.c, kinds="wheel,wheel,sdist")
        self.assertEqual(load(self.c, "deal_2")["required_kinds"], ["sdist", "wheel"])

    def test_repo_url_rules(self):
        for url in ("", "http://github.com/acme/widget", "https://github.com/acme", "https://github.com/",
                    "https://u:p@github.com/acme/widget", "https://github.com:8443/acme/widget",
                    "github.com/acme/widget", "ftp://github.com/acme/widget", "https://x/acme/widget",
                    "https://github.com/" + "a" * 300):
            self.bad(repo=url)
        create_deal(self.c, repo="https://gitlab.com/group/project")
        create_deal(self.c, repo="https://github.com/acme/widget.git")
        self.assertEqual(load(self.c, "deal_1")["expected_repo"], "github.com/acme/widget")

    def test_nothing_is_stored_for_a_rejected_deal(self):
        self.bad(version="nope")
        self.assertEqual(len(self.c.deals), 0)


class AcceptCancelExpire(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()
        self.id = create_deal(self.c)

    def test_only_party_b_can_accept_with_the_exact_stake(self):
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 1000, self.id)
        set_caller(CLIENT)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 1000, self.id)
        set_caller(DEV)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 999, self.id)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 1001, self.id)
        call_payable(self.c, "accept_deal", 1000, self.id)
        d = load(self.c, self.id)
        self.assertEqual(d["status"], "open")
        self.assertIn("accepted_at", d)

    def test_cannot_accept_twice(self):
        set_caller(DEV)
        call_payable(self.c, "accept_deal", 1000, self.id)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 1000, self.id)

    def test_cannot_accept_after_the_deadline(self):
        patch_deal(self.c, self.id, deadline=iso(-5))
        set_caller(DEV)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 1000, self.id)

    def test_accept_unknown_deal(self):
        set_caller(DEV)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "accept_deal", 1000, "deal_99")

    def test_party_a_can_cancel_before_acceptance_and_is_refunded(self):
        set_caller(CLIENT)
        self.c.cancel_deal(self.id)
        self.assertEqual(load(self.c, self.id)["status"], "cancelled")
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "1000")

    def test_only_party_a_can_cancel(self):
        for who in (DEV, STRANGER):
            set_caller(who)
            with self.assertRaises(gl.vm.UserError):
                self.c.cancel_deal(self.id)

    def test_cannot_cancel_after_acceptance(self):
        set_caller(DEV)
        call_payable(self.c, "accept_deal", 1000, self.id)
        set_caller(CLIENT)
        with self.assertRaises(gl.vm.UserError):
            self.c.cancel_deal(self.id)

    def test_cancel_twice_does_not_double_refund(self):
        set_caller(CLIENT)
        self.c.cancel_deal(self.id)
        with self.assertRaises(gl.vm.UserError):
            self.c.cancel_deal(self.id)
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "1000")

    def test_expire_unaccepted_refunds_party_a_after_the_deadline(self):
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_unaccepted(self.id)
        patch_deal(self.c, self.id, deadline=iso(-5))
        self.c.expire_unaccepted(self.id)
        self.assertEqual(load(self.c, self.id)["status"], "cancelled")
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "1000")
        self.assertEqual(self.c.get_pending_withdrawal(STRANGER), "0")

    def test_expire_unaccepted_rejects_an_accepted_deal(self):
        set_caller(DEV)
        call_payable(self.c, "accept_deal", 1000, self.id)
        patch_deal(self.c, self.id, deadline=iso(-5))
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_unaccepted(self.id)

    def test_expire_unaccepted_cannot_be_repeated(self):
        patch_deal(self.c, self.id, deadline=iso(-5))
        self.c.expire_unaccepted(self.id)
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_unaccepted(self.id)
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "1000")


class ExpireUnresolved(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        reset_transfers()
        self.id = open_deal(self.c)

    def test_not_before_the_timeout(self):
        patch_deal(self.c, self.id, deadline=iso(-3600))
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_unresolved(self.id)

    def test_not_before_the_deadline_either(self):
        set_caller(STRANGER)
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_unresolved(self.id)

    def test_after_the_timeout_anyone_refunds_both_stakes(self):
        patch_deal(self.c, self.id, deadline=iso(-(m.ShipSeal.RESOLUTION_TIMEOUT_SECONDS + 10)))
        set_caller(STRANGER)
        self.c.expire_unresolved(self.id)
        d = load(self.c, self.id)
        self.assertEqual((d["status"], d["final_outcome"]), ("finalized", "UNDETERMINED"))
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "1000")
        self.assertEqual(self.c.get_pending_withdrawal(DEV), "1000")

    def test_only_an_open_deal_can_expire(self):
        patch_deal(self.c, self.id, deadline=iso(-(m.ShipSeal.RESOLUTION_TIMEOUT_SECONDS + 10)))
        self.c.expire_unresolved(self.id)
        with self.assertRaises(gl.vm.UserError):
            self.c.expire_unresolved(self.id)
        self.assertEqual(self.c.get_pending_withdrawal(CLIENT), "1000")


if __name__ == "__main__":
    unittest.main()
