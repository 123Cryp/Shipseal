"""Table-driven tests of the pure structured verdict (_classify) and of the
outcome/backstop functions. No contract state is involved."""
import copy
import json
import unittest

from _bootstrap import m, release_payload, index_payload, stamp, REPO, SHA_WHEEL, SHA_SDIST, FakePyPI

CREATED = 1_000_000
DEADLINE = CREATED + 7200
AFTER = DEADLINE + m.ABSENCE_GRACE_SECONDS + 1


def spec(**over):
    s = {"package": "widget", "version": "1.0.0", "required_kinds": ["sdist", "wheel"], "expected_sha256": "",
         "expected_repo": "github.com/acme/widget", "created_ts": CREATED, "deadline_ts": DEADLINE}
    s.update(over)
    return s


def ts(offset):
    return CREATED + offset


def facts_from(payload, status="ok"):
    data = m._extract_release(json.dumps(payload))
    # Replace the wall-clock upload stamps with deterministic ones relative to CREATED.
    return {"package": "widget", "version": "1.0.0", "release": {"status": status, "data": data},
            "index": {"status": "skipped"}, "urls": ["u1", "u2"]}


def with_times(facts, times):
    """times: [wheel_offset, sdist_offset] in seconds after CREATED (None = no timestamp)."""
    facts = copy.deepcopy(facts)
    by_type = {"bdist_wheel": times[0], "sdist": times[1]}
    for f in facts["release"]["data"]["files"]:
        off = by_type.get(f["packagetype"])
        f["upload_ts"] = None if off is None else ts(off)
    return facts


def good(**over):
    base = facts_from(release_payload(**over))
    return with_times(base, [10, 20])


def verdict(facts, sp=None, at=DEADLINE - 1):
    return m._classify(facts, sp or spec(), at)


def absent(index_status="ok", name_ok=True, has_version=False):
    return {"package": "widget", "version": "1.0.0", "release": {"status": "not_found", "data": None},
            "index": {"status": index_status, "name_ok": name_ok, "has_version": has_version}, "urls": ["u1", "u2"]}


class StructuredVerdict(unittest.TestCase):
    def test_all_checks_pass(self):
        got = verdict(good())
        self.assertEqual(got["verdict"], "DELIVERED_OK")
        self.assertEqual(got["reasons"], ["structured_checks_passed"])
        self.assertTrue(all(v for v in got["checks"].values() if v is not None))

    def test_wrong_version_in_record_is_identity_mismatch(self):
        f = good()
        f["release"]["data"]["version"] = "1.0"
        self.assertEqual(verdict(f)["reasons"], ["identity_mismatch"])
        self.assertEqual(verdict(f)["verdict"], "UNDETERMINED")

    def test_wrong_project_name_in_record_is_identity_mismatch(self):
        f = good()
        f["release"]["data"]["name"] = "gadget"
        self.assertEqual(verdict(f)["verdict"], "UNDETERMINED")

    def test_yanked_release_is_not_delivered(self):
        f = good()
        f["release"]["data"]["yanked"] = True
        got = verdict(f)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["yanked"]))

    def test_missing_wheel_is_partial(self):
        f = good()
        f["release"]["data"]["files"] = [x for x in f["release"]["data"]["files"] if x["packagetype"] != "bdist_wheel"]
        got = verdict(f)
        self.assertEqual((got["verdict"], got["reasons"]), ("PARTIALLY_DELIVERED", ["kind_missing:wheel"]))

    def test_missing_sdist_is_partial(self):
        f = good()
        f["release"]["data"]["files"] = [x for x in f["release"]["data"]["files"] if x["packagetype"] != "sdist"]
        self.assertEqual(verdict(f)["reasons"], ["kind_missing:sdist"])

    def test_a_late_second_kind_counts_as_missing(self):
        f = with_times(good(), [10, 7300])
        sd = [x for x in f["release"]["data"]["files"] if x["packagetype"] == "sdist"][0]
        self.assertGreater(sd["upload_ts"], DEADLINE)
        got = verdict(f)
        self.assertEqual(got["verdict"], "PARTIALLY_DELIVERED")

    def test_only_required_kinds_matter(self):
        f = good()
        got = verdict(f, spec(required_kinds=["wheel"]))
        self.assertEqual(got["verdict"], "DELIVERED_OK")

    def test_extra_unrequired_file_kinds_are_ignored(self):
        f = good()
        f["release"]["data"]["files"].append({"packagetype": "bdist_egg", "filename": "x.egg", "sha256": "",
                                              "upload_ts": ts(30), "yanked": False})
        self.assertEqual(verdict(f)["verdict"], "DELIVERED_OK")

    def test_release_without_any_required_kind_is_not_delivered(self):
        f = good()
        for x in f["release"]["data"]["files"]:
            x["packagetype"] = "bdist_egg"
        got = verdict(f)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["no_required_files"]))

    def test_files_uploaded_after_the_deadline_are_late(self):
        f = with_times(good(), [7300, 7400])
        got = verdict(f, at=AFTER)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["published_late"]))

    def test_upload_exactly_at_the_deadline_is_in_time(self):
        f = with_times(good(), [7200, 7200])
        self.assertEqual(verdict(f)["verdict"], "DELIVERED_OK")

    def test_upload_exactly_at_creation_is_in_time(self):
        f = with_times(good(), [0, 0])
        self.assertEqual(verdict(f)["verdict"], "DELIVERED_OK")

    def test_one_second_before_creation_predates_the_deal(self):
        f = with_times(good(), [-1, -1])
        got = verdict(f)
        self.assertEqual((got["verdict"], got["reasons"]), ("UNDETERMINED", ["release_predates_deal"]))

    def test_one_second_after_the_deadline_is_late(self):
        f = with_times(good(), [7201, 7201])
        self.assertEqual(verdict(f, at=AFTER)["reasons"], ["published_late"])

    def test_release_with_only_old_files_of_other_kinds_predates(self):
        f = with_times(good(), [-500, -400])
        self.assertEqual(verdict(f)["reasons"], ["release_predates_deal"])

    def test_old_required_files_with_a_recent_unrequired_file(self):
        f = with_times(good(), [-500, -400])
        f["release"]["data"]["files"].append({"packagetype": "bdist_egg", "filename": "x.egg", "sha256": "",
                                              "upload_ts": ts(30), "yanked": False})
        self.assertEqual(verdict(f)["reasons"], ["required_files_predate_deal"])

    def test_release_with_no_files_is_not_delivered_once_the_grace_has_passed(self):
        f = good()
        f["release"]["data"]["files"] = []
        got = verdict(f, at=DEADLINE + m.ABSENCE_GRACE_SECONDS + 1)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["release_has_no_files"]))

    def test_release_with_no_files_is_pending_until_the_grace_has_passed(self):
        f = good()
        f["release"]["data"]["files"] = []
        for at in (DEADLINE - 1, DEADLINE + m.ABSENCE_GRACE_SECONDS):
            self.assertEqual(verdict(f, at=at)["verdict"], "PENDING")

    def test_files_without_upload_time_cannot_prove_delivery(self):
        f = with_times(good(), [None, None])
        self.assertEqual(verdict(f)["verdict"], "UNDETERMINED")

    def test_repo_must_match(self):
        f = good(repo="https://github.com/mallory/widget")
        f = with_times(f, [10, 20])
        got = verdict(f)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["repo_unverified"]))

    def test_repo_with_no_urls_is_unverified(self):
        f = with_times(facts_from(release_payload(project_urls={})), [10, 20])
        self.assertEqual(verdict(f)["reasons"], ["repo_unverified"])

    def test_repo_matched_through_home_page(self):
        f = with_times(facts_from(release_payload(project_urls={}, home_page=REPO)), [10, 20])
        self.assertEqual(verdict(f)["verdict"], "DELIVERED_OK")

    def test_repo_subpath_issue_tracker_is_accepted(self):
        f = with_times(facts_from(release_payload(project_urls={"Tracker": REPO + "/issues"})), [10, 20])
        self.assertEqual(verdict(f)["verdict"], "DELIVERED_OK")

    def test_sha_must_match_a_required_file(self):
        sp = spec(expected_sha256="c" * 64)
        got = verdict(good(), sp)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["sha256_mismatch"]))

    def test_sha_matching_the_wheel_or_the_sdist_passes(self):
        self.assertEqual(verdict(good(), spec(expected_sha256=SHA_WHEEL))["verdict"], "DELIVERED_OK")
        self.assertEqual(verdict(good(), spec(expected_sha256=SHA_SDIST))["verdict"], "DELIVERED_OK")

    def test_sha_of_an_unrequired_file_does_not_count(self):
        f = good()
        f["release"]["data"]["files"].append({"packagetype": "bdist_egg", "filename": "x.egg", "sha256": "d" * 64,
                                              "upload_ts": ts(30), "yanked": False})
        self.assertEqual(verdict(f, spec(expected_sha256="d" * 64))["reasons"], ["sha256_mismatch"])

    def test_sha_of_a_late_file_does_not_count(self):
        f = with_times(good(), [10, 7300])
        sp = spec(expected_sha256=SHA_SDIST)
        got = verdict(f, sp)
        self.assertEqual(got["verdict"], "PARTIALLY_DELIVERED")

    def test_partial_wins_over_sha_mismatch_when_the_hashed_kind_is_missing(self):
        f = good()
        f["release"]["data"]["files"] = [x for x in f["release"]["data"]["files"] if x["packagetype"] == "bdist_wheel"]
        got = verdict(f, spec(expected_sha256=SHA_SDIST))
        self.assertEqual(got["verdict"], "PARTIALLY_DELIVERED")

    def test_yank_wins_over_everything_after_the_window_checks(self):
        f = good()
        f["release"]["data"]["yanked"] = True
        f["release"]["data"]["repo_urls"] = []
        self.assertEqual(verdict(f)["reasons"], ["yanked"])

    def test_unreadable_record_is_transient(self):
        f = good()
        f["release"] = {"status": "error", "data": None}
        got = verdict(f)
        self.assertEqual((got["verdict"], got["reasons"]), ("TRANSIENT", ["release_unavailable"]))

    def test_ok_status_with_malformed_data_is_transient(self):
        f = good()
        f["release"]["data"] = {"files": "nope"}
        self.assertEqual(verdict(f)["verdict"], "TRANSIENT")


class AbsenceAsEvidence(unittest.TestCase):
    def test_404_plus_index_without_version_after_grace_is_not_delivered(self):
        got = verdict(absent(), at=AFTER)
        self.assertEqual((got["verdict"], got["reasons"]), ("NOT_DELIVERED", ["absent_proven"]))
        self.assertTrue(got["checks"]["absence"])

    def test_absence_is_not_ripe_before_the_deadline(self):
        got = verdict(absent(), at=DEADLINE - 5)
        self.assertEqual((got["verdict"], got["reasons"]), ("PENDING", ["absence_not_ripe"]))

    def test_absence_is_not_ripe_at_the_deadline_itself(self):
        self.assertEqual(verdict(absent(), at=DEADLINE)["verdict"], "PENDING")

    def test_absence_is_not_ripe_inside_the_grace_period(self):
        self.assertEqual(verdict(absent(), at=DEADLINE + m.ABSENCE_GRACE_SECONDS)["verdict"], "PENDING")

    def test_absence_is_ripe_one_second_after_the_grace_period(self):
        self.assertEqual(verdict(absent(), at=DEADLINE + m.ABSENCE_GRACE_SECONDS + 1)["verdict"], "NOT_DELIVERED")

    def test_404_with_404_index_is_a_possible_typo_not_proof(self):
        got = verdict(absent("not_found"), at=AFTER)
        self.assertEqual((got["verdict"], got["reasons"]), ("UNDETERMINED", ["project_not_found"]))

    def test_404_with_unreadable_index_is_transient(self):
        got = verdict(absent("error"), at=AFTER)
        self.assertEqual((got["verdict"], got["reasons"]), ("TRANSIENT", ["index_unavailable"]))

    def test_404_with_skipped_index_is_transient(self):
        self.assertEqual(verdict(absent("skipped"), at=AFTER)["verdict"], "TRANSIENT")

    def test_404_but_index_lists_the_version_is_inconsistent(self):
        got = verdict(absent(has_version=True), at=AFTER)
        self.assertEqual((got["verdict"], got["reasons"]), ("TRANSIENT", ["index_lists_version"]))

    def test_404_but_index_names_another_project(self):
        got = verdict(absent(name_ok=False), at=AFTER)
        self.assertEqual((got["verdict"], got["reasons"]), ("UNDETERMINED", ["index_name_mismatch"]))

    def test_an_outage_alone_can_never_produce_not_delivered(self):
        for release_status in ("error",):
            f = {"package": "widget", "version": "1.0.0", "release": {"status": release_status, "data": None},
                 "index": {"status": "skipped"}, "urls": ["a", "b"]}
            self.assertNotEqual(verdict(f, at=AFTER)["verdict"], "NOT_DELIVERED")


class OutcomeAndBackstop(unittest.TestCase):
    def test_delivered_needs_both_structure_and_model(self):
        self.assertEqual(m._final_outcome("DELIVERED_OK", "DELIVERED"), "DELIVERED")
        self.assertEqual(m._final_outcome("DELIVERED_OK", "UNDETERMINED"), "UNDETERMINED")
        self.assertEqual(m._final_outcome("DELIVERED_OK", None), "UNDETERMINED")
        self.assertEqual(m._final_outcome("DELIVERED_OK", "NOT_DELIVERED"), "UNDETERMINED")

    def test_a_delivered_claim_never_survives_a_failed_structure(self):
        for verdict_name in ("PARTIALLY_DELIVERED", "NOT_DELIVERED", "UNDETERMINED", "TRANSIENT", "PENDING"):
            self.assertNotEqual(m._final_outcome(verdict_name, "DELIVERED"), "DELIVERED", verdict_name)

    def test_structured_negatives_cannot_be_overridden_by_the_model(self):
        self.assertEqual(m._final_outcome("NOT_DELIVERED", "DELIVERED"), "NOT_DELIVERED")
        self.assertEqual(m._final_outcome("PARTIALLY_DELIVERED", "DELIVERED"), "PARTIALLY_DELIVERED")
        self.assertEqual(m._final_outcome("UNDETERMINED", "NOT_DELIVERED"), "UNDETERMINED")

    def test_backstop_forces_delivered_down_to_undetermined(self):
        self.assertEqual(m._backstop("DELIVERED", False), "UNDETERMINED")
        self.assertEqual(m._backstop("DELIVERED", True), "DELIVERED")
        self.assertEqual(m._backstop("NOT_DELIVERED", False), "NOT_DELIVERED")

    def test_describe_renders_reason_codes_in_english(self):
        self.assertIn("wheel", m._describe(["kind_missing:wheel"]))
        self.assertIn("yanked", m._describe(["yanked"]).lower())
        self.assertEqual(m._describe(["unknown_code"]), "unknown_code")


if __name__ == "__main__":
    unittest.main()
