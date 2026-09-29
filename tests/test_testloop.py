"""U1 of the browser test loop plan: the loop's rules as a pure module, tested as a table of facts
in and a decision out, with no Feeder, no clock, and no files."""
import ast
import copy
import os
import unittest
from datetime import datetime, timedelta

import _paths
from relay import testloop

START = datetime(2026, 9, 28, 9, 0)


def finding(severity="high", area="Search", card=None, **changes):
    """A finding with the shape KTD4 names, valid unless `changes` says otherwise."""
    shape = {
        "title": "Search drops the last result",
        "severity": severity,
        "kind": "defect",
        "area": area,
        "design": False,
        "cause": {"file": "app/search.py", "line": 42, "verdict": "defect"},
        "steps": ["Open Search", "Search for pump"],
        "expected": "Three results",
        "observed": "Two results",
        "done_when": ["Search for pump shows three results"],
    }
    if card is not None:
        shape["card"] = card
    shape.update(changes)
    return shape


def numbered(severities, area="Search"):
    return [finding(severity, area=area, title="Finding %d" % (index + 1))
            for index, severity in enumerate(severities)]


def titles(findings):
    return [item["title"] for item in findings]


class ModuleIsPure(unittest.TestCase):
    def test_imports_nothing_from_the_feeder_or_the_runner(self):
        path = os.path.join(_paths.SCRIPTS_DIR, "relay", "testloop.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append("." * node.level + (node.module or ""))
        self.assertTrue(imported)
        for name in imported:
            with self.subTest(name=name):
                self.assertFalse(name.startswith(".") or name.split(".")[0] == "relay", name)


class ValidateFinding(unittest.TestCase):
    def test_a_whole_finding_has_no_problems(self):
        self.assertEqual(testloop.validate_finding(finding()), [])
        self.assertEqual(testloop.validate_finding(finding(card="12")), [])
        self.assertEqual(testloop.validate_finding(finding(card=None)), [])

    def test_a_numeric_card_id_is_accepted(self):
        """Issue #118: a model on a check pass writes the id as a number."""
        self.assertEqual(testloop.validate_finding(finding(card=12)), [])

    def test_keys_beyond_the_contract_are_left_alone(self):
        self.assertEqual(testloop.validate_finding(finding(screenshot="shot.png")), [])

    def test_a_missing_severity_is_reported(self):
        shape = finding()
        del shape["severity"]
        self.assertEqual(testloop.validate_finding(shape), ["severity is missing"])

    def test_an_unknown_severity_is_reported(self):
        problems = testloop.validate_finding(finding(severity="critical"))
        self.assertEqual(len(problems), 1)
        self.assertIn("'critical'", problems[0])

    def test_each_broken_field_is_reported_by_name(self):
        table = [
            ({"title": ""}, "title"),
            ({"kind": "bug"}, "kind"),
            ({"area": "  "}, "area"),
            ({"design": "no"}, "design"),
            ({"cause": "app/search.py:42"}, "cause"),
            ({"cause": {"file": "", "line": 42, "verdict": "defect"}}, "cause.file"),
            ({"cause": {"file": "a.py", "line": 0, "verdict": "defect"}}, "cause.line"),
            ({"cause": {"file": "a.py", "line": True, "verdict": "defect"}}, "cause.line"),
            ({"cause": {"file": "a.py", "line": "42", "verdict": "defect"}}, "cause.line"),
            ({"cause": {"file": "a.py", "line": 42, "verdict": "maybe"}}, "cause.verdict"),
            ({"steps": []}, "steps"),
            ({"steps": "Open Search"}, "steps"),
            ({"expected": None}, "expected"),
            ({"observed": 3}, "observed"),
            ({"done_when": [""]}, "done_when"),
            ({"card": True}, "card"),
            ({"card": 1.5}, "card"),
            ({"card": ""}, "card"),
        ]
        for changes, name in table:
            with self.subTest(changes=changes):
                problems = testloop.validate_finding(finding(**changes))
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(name), problems)

    def test_a_finding_that_is_not_an_object_is_reported(self):
        self.assertEqual(testloop.validate_finding(["high"]),
                         ["a finding must be a JSON object"])


class SelectFindings(unittest.TestCase):
    def test_ae3_fourteen_serious_findings_file_ten_highest_first(self):
        severities = ["medium", "high"] * 7
        findings = numbered(severities)
        chosen = testloop.select_findings(findings, cap=10, budget_left=30)
        highs = [item["title"] for item in findings if item["severity"] == "high"]
        mediums = [item["title"] for item in findings if item["severity"] == "medium"]
        self.assertEqual(titles(chosen.to_file), highs + mediums[:3])
        self.assertEqual(titles(chosen.over_cap), mediums[3:])
        self.assertEqual(len(chosen.over_cap), 4)
        self.assertEqual(chosen.over_budget, ())

    def test_report_order_is_kept_within_a_severity(self):
        findings = numbered(["medium", "high", "medium", "high"])
        chosen = testloop.select_findings(findings, cap=10, budget_left=30)
        self.assertEqual(titles(chosen.to_file),
                         ["Finding 2", "Finding 4", "Finding 1", "Finding 3"])

    def test_lows_never_reach_the_to_file_list_whatever_the_room(self):
        for cap, budget in ((10, 30), (1, 30), (10, 0)):
            with self.subTest(cap=cap, budget=budget):
                findings = numbered(["low", "low", "high"])
                chosen = testloop.select_findings(findings, cap=cap, budget_left=budget)
                self.assertNotIn("low", [item["severity"] for item in chosen.to_file])
                self.assertEqual(titles(chosen.lows), ["Finding 1", "Finding 2"])

    def test_a_finding_in_a_stopped_area_is_dropped_and_named(self):
        findings = [finding("high", area="Search", title="kept"),
                    finding("high", area="Billing", title="gone"),
                    finding("low", area="Billing", title="gone low")]
        chosen = testloop.select_findings(findings, cap=10, budget_left=30,
                                          stopped_areas=["Billing"])
        self.assertEqual(titles(chosen.to_file), ["kept"])
        self.assertEqual(titles(chosen.dropped), ["gone", "gone low"])
        self.assertEqual(chosen.lows, ())

    def test_ae11_two_left_in_the_budget_files_two_and_names_three_past_it(self):
        findings = numbered(["high"] * 5)
        chosen = testloop.select_findings(findings, cap=10, budget_left=2)
        self.assertEqual(titles(chosen.to_file), ["Finding 1", "Finding 2"])
        self.assertEqual(titles(chosen.over_budget), ["Finding 3", "Finding 4", "Finding 5"])
        self.assertEqual(chosen.over_cap, ())

    def test_a_tie_between_the_cap_and_the_budget_names_the_budget(self):
        chosen = testloop.select_findings(numbered(["high"] * 12), cap=10, budget_left=10)
        self.assertEqual(len(chosen.to_file), 10)
        self.assertEqual(len(chosen.over_budget), 2)
        self.assertEqual(chosen.over_cap, ())

    def test_outcomes_follow_report_order(self):
        findings = [finding("medium", title="a"), finding("low", title="b"),
                    finding("high", area="Billing", title="c"), finding("high", title="d")]
        chosen = testloop.select_findings(findings, cap=1, budget_left=30,
                                          stopped_areas={"Billing"})
        self.assertEqual(chosen.outcomes, (testloop.OVER_CAP, testloop.OUTCOME_LOW,
                                           testloop.DROPPED, testloop.FILE))

    def test_a_spent_budget_files_nothing(self):
        for budget in (0, -3):
            with self.subTest(budget=budget):
                chosen = testloop.select_findings(numbered(["high"]), cap=10,
                                                  budget_left=budget)
                self.assertEqual(chosen.to_file, ())
                self.assertEqual(titles(chosen.over_budget), ["Finding 1"])

    def test_bad_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            testloop.select_findings(numbered(["high"]), cap=0, budget_left=5)
        with self.assertRaises(ValueError):
            testloop.select_findings(numbered(["high"]), cap=10, budget_left=None)
        with self.assertRaisesRegex(ValueError, "finding 1"):
            testloop.select_findings([finding("critical")], cap=10, budget_left=5)

    def test_the_findings_are_not_changed(self):
        findings = numbered(["medium", "high", "low"])
        before = copy.deepcopy(findings)
        testloop.select_findings(findings, cap=1, budget_left=1)
        self.assertEqual(findings, before)


FILED = {
    "1": {"generation": 1, "area": "Search"},
    "2": {"generation": 2, "area": "Search"},
}


class CardsToCheck(unittest.TestCase):
    def test_ae1_a_landed_last_generation_card_is_not_checked(self):
        self.assertEqual(testloop.cards_to_check(["1", "2", "9"], FILED), ("1", "9"))

    def test_only_last_generation_cards_landed_checks_nothing(self):
        self.assertEqual(testloop.cards_to_check(["2"], FILED), ())


class GenerationFor(unittest.TestCase):
    def test_the_generation_table(self):
        table = [
            (testloop.TOUR, None, (), 1),
            (testloop.TOUR, "1", ("1",), 1),
            (testloop.CHECK, "1", ("1", "9"), 2),
            (testloop.CHECK, "9", ("1", "9"), 1),
            (testloop.CHECK, None, ("1",), 2),
            (testloop.CHECK, "9", ("1",), 2),
            (testloop.CHECK, "2", ("2",), 2),
        ]
        for kind, card, sent, expected in table:
            with self.subTest(kind=kind, card=card, sent=sent):
                self.assertEqual(testloop.generation_for(kind, card, sent, FILED), expected)

    def test_ae1_a_filing_from_checking_a_tour_card_is_the_last_generation(self):
        generation = testloop.generation_for(testloop.CHECK, "1", ["1"], FILED)
        self.assertEqual(generation, testloop.LAST_GENERATION)
        filed = dict(FILED, B={"generation": generation, "area": "Search"})
        self.assertEqual(testloop.cards_to_check(["B"], filed), ())

    def test_a_filed_record_with_no_readable_generation_reads_as_the_last(self):
        for record in ({"area": "Search"}, {"generation": None}, {"generation": "1"},
                       {"generation": True}, {"generation": 7}, "1"):
            with self.subTest(record=record):
                filed = {"7": record}
                self.assertEqual(testloop.cards_to_check(["7"], filed), ())
                self.assertEqual(testloop.generation_for(testloop.CHECK, "7", ["7"], filed),
                                 testloop.LAST_GENERATION)

    def test_an_unknown_pass_kind_is_refused(self):
        with self.assertRaises(ValueError):
            testloop.generation_for("sweep", None, (), FILED)


class AreaPatches(unittest.TestCase):
    FILED = {
        "1": {"generation": 1, "area": "Search"},
        "3": {"generation": 1, "area": "Search"},
        "5": {"generation": 1, "area": "Search"},
        "7": {"generation": 1, "area": "Billing"},
    }

    def test_ae7_three_landings_that_each_failed_again_reach_the_cap(self):
        checks = {"1": ["Search"], "3": ["Search", "Search"], "5": ["Search"]}
        patches = testloop.area_patches(self.FILED, checks, cap=3)
        self.assertEqual(patches.counts, {"Search": 3})
        self.assertEqual(patches.reached, ("Search",))

    def test_two_landings_do_not(self):
        patches = testloop.area_patches(self.FILED, {"1": ["Search"], "3": ["Search"]}, cap=3)
        self.assertEqual(patches.counts, {"Search": 2})
        self.assertEqual(patches.reached, ())

    def test_a_check_that_filed_elsewhere_or_a_card_the_loop_did_not_file_counts_nothing(self):
        checks = {"1": ["Billing"], "3": [], "9": ["Search"], "7": ["Billing"]}
        patches = testloop.area_patches(self.FILED, checks, cap=3)
        self.assertEqual(patches.counts, {"Billing": 1})

    def test_a_cap_that_is_not_a_positive_integer_is_refused(self):
        for cap in (0, -1, True, "3", None):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    testloop.area_patches(self.FILED, {"1": ["Search"]}, cap=cap)


def tour(findings=(), new_cards=0, status=testloop.RAN):
    return testloop.PassResult(kind=testloop.TOUR, status=status,
                               findings=tuple(findings), new_cards=new_cards)


def check(findings=(), new_cards=0, status=testloop.RAN):
    return testloop.PassResult(kind=testloop.CHECK, status=status,
                               findings=tuple(findings), new_cards=new_cards)


class ShouldStop(unittest.TestCase):
    def stop(self, result, rounds=1, hours=1, cards=0, report_only=False,
             settings=testloop.Settings()):
        return testloop.should_stop(result, rounds=rounds, started_at=START,
                                    now=START + timedelta(hours=hours), cards_filed=cards,
                                    report_only=report_only, settings=settings)

    def test_ae2_a_tour_with_only_lows_stops_clean(self):
        self.assertEqual(self.stop(tour(numbered(["low", "low"])), rounds=3),
                         testloop.STOP_CLEAN)
        self.assertEqual(self.stop(tour()), testloop.STOP_CLEAN)

    def test_ae2_a_check_with_only_lows_goes_on(self):
        self.assertIsNone(self.stop(check(numbered(["low"]))))
        self.assertIsNone(self.stop(check()))

    def test_a_tour_that_filed_a_card_goes_on(self):
        self.assertIsNone(self.stop(tour(numbered(["high", "low"]), new_cards=1), cards=1))

    def test_ae10_findings_commented_onto_open_cards_stop_on_open_findings(self):
        self.assertEqual(self.stop(tour(numbered(["high", "high"]), new_cards=0)),
                         testloop.STOP_OPEN_FINDINGS)

    def test_ae10_findings_dropped_for_stopped_areas_stop_on_open_findings(self):
        findings = numbered(["high", "high"], area="Billing")
        chosen = testloop.select_findings(findings, cap=10, budget_left=30,
                                          stopped_areas=["Billing"])
        self.assertEqual(chosen.to_file, ())
        self.assertEqual(self.stop(tour(findings, new_cards=0)), testloop.STOP_OPEN_FINDINGS)

    def test_ae11_filing_the_last_of_the_budget_stops_on_the_budget(self):
        chosen = testloop.select_findings(numbered(["high"] * 5), cap=10, budget_left=30 - 28)
        filed = 28 + len(chosen.to_file)
        self.assertEqual(self.stop(check(numbered(["high"] * 5), new_cards=2), cards=filed),
                         testloop.STOP_BUDGET)
        self.assertEqual(self.stop(tour(numbered(["high"] * 5), new_cards=2), cards=filed),
                         testloop.STOP_BUDGET)

    def test_a_spent_budget_names_the_budget_not_open_findings(self):
        findings = numbered(["high", "high"])
        chosen = testloop.select_findings(findings, cap=10, budget_left=0)
        self.assertEqual(chosen.to_file, ())
        self.assertEqual(self.stop(tour(findings, new_cards=0), cards=30),
                         testloop.STOP_BUDGET)

    def test_a_clean_tour_names_clean_even_with_the_budget_spent(self):
        self.assertEqual(self.stop(tour(numbered(["low"])), cards=30), testloop.STOP_CLEAN)

    def test_an_entry_that_is_not_a_finding_is_not_serious(self):
        self.assertEqual(self.stop(tour(["stray", ["high"], numbered(["low"])[0]])),
                         testloop.STOP_CLEAN)
        self.assertEqual(self.stop(tour(["stray"] + numbered(["high"]), new_cards=1)), None)

    def test_a_planning_finding_beside_only_lows_stops_clean(self):
        """Issue #115: the attended planning finding of R19 is filed high so a person sees it,
        and it is the loop's own request, not a defect the tour found."""
        planning = finding("high", area="Billing", title="Plan the Billing area",
                           **{testloop.ATTENDED_KEY: True})
        self.assertTrue(testloop.is_attended(planning))
        self.assertFalse(testloop.is_attended(finding()))
        self.assertFalse(testloop.is_attended(finding(**{testloop.ATTENDED_KEY: "yes"})))
        self.assertEqual(self.stop(tour(numbered(["low"]) + [planning], new_cards=0)),
                         testloop.STOP_CLEAN)
        self.assertEqual(self.stop(tour([planning], new_cards=0)), testloop.STOP_CLEAN)

    def test_a_planning_card_filed_beside_dropped_or_commented_findings_is_no_new_card(self):
        """The caller counts the planning card out of `new_cards`, and the dropped or commented
        highs then stop the loop on open findings, never read as productive."""
        planning = finding("high", area="Billing", **{testloop.ATTENDED_KEY: True})
        dropped = numbered(["high", "medium"], area="Billing")
        self.assertEqual(self.stop(tour(dropped + [planning], new_cards=0)),
                         testloop.STOP_OPEN_FINDINGS)
        self.assertIsNone(self.stop(tour(dropped + [planning], new_cards=1), cards=1))

    def test_an_unknown_kind_or_status_is_refused(self):
        for result in (testloop.PassResult(kind="Tour", status=testloop.RAN),
                       testloop.PassResult(kind=testloop.TOUR, status="Ran")):
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    self.stop(result, rounds=9)

    def test_the_round_cap_stops_at_round_six_not_five(self):
        busy = tour(numbered(["high"]), new_cards=1)
        self.assertIsNone(self.stop(busy, rounds=5, cards=5))
        self.assertEqual(self.stop(busy, rounds=6, cards=6), testloop.STOP_ROUNDS)

    def test_the_round_cap_does_not_stop_a_check(self):
        self.assertIsNone(self.stop(check(numbered(["high"]), new_cards=1), rounds=6, cards=6))

    def test_the_clock_cap_stops_at_twenty_four_hours_from_the_start(self):
        busy = check(numbered(["high"]), new_cards=1)
        self.assertIsNone(self.stop(busy, hours=23.99, cards=1))
        self.assertEqual(self.stop(busy, hours=24, cards=1), testloop.STOP_CLOCK)

    def test_a_pass_that_did_not_run_stops_only_on_the_clock_or_the_budget(self):
        for status in (testloop.NOT_RUN, testloop.FAILED):
            with self.subTest(status=status):
                self.assertIsNone(self.stop(tour(status=status), rounds=6))
                self.assertIsNone(self.stop(tour(status=status), report_only=True))
                self.assertEqual(self.stop(tour(status=status), hours=25),
                                 testloop.STOP_CLOCK)
                self.assertEqual(self.stop(check(status=status), cards=30),
                                 testloop.STOP_BUDGET)

    def test_a_report_only_tour_stops(self):
        self.assertEqual(self.stop(tour(numbered(["high", "high"])), report_only=True),
                         testloop.STOP_REPORT_ONLY)

    def test_the_caps_are_settings(self):
        settings = testloop.Settings(max_rounds=2, max_hours=1, max_cards_total=5)
        busy = tour(numbered(["high"]), new_cards=1)
        self.assertEqual(self.stop(busy, rounds=2, hours=0, cards=1, settings=settings),
                         testloop.STOP_ROUNDS)
        self.assertEqual(self.stop(busy, rounds=1, hours=1, cards=1, settings=settings),
                         testloop.STOP_CLOCK)
        self.assertEqual(self.stop(busy, rounds=1, hours=0, cards=5, settings=settings),
                         testloop.STOP_BUDGET)

    def test_the_defaults_are_the_plans(self):
        settings = testloop.Settings()
        self.assertEqual((settings.max_rounds, settings.max_hours, settings.max_cards_per_pass,
                          settings.max_patches_per_area, settings.max_cards_total),
                         (6, 24, 10, 3, 30))


def partial(findings=(), new_cards=0, untoured=("Invoices", "Settings"), status=testloop.RAN,
            kind=testloop.TOUR):
    return testloop.PassResult(kind=kind, status=status, findings=tuple(findings),
                               new_cards=new_cards, untoured=tuple(untoured))


class PartialTour(ShouldStop):
    """Issue #121: a tour that could not reach every area is never read as clean. The live
    tour that found this reached 3 of 10 areas behind a sign in, reported `ran` with two lows,
    and would have stopped the loop on the clean reason."""

    def test_a_partial_tour_with_only_lows_is_not_clean_and_goes_on(self):
        self.assertIsNone(self.stop(partial(numbered(["low", "low"])), rounds=3))
        self.assertIsNone(self.stop(partial()))
        self.assertTrue(testloop.is_partial(partial()))
        self.assertFalse(testloop.is_partial(tour()))

    def test_a_partial_tour_with_nothing_serious_does_not_stop_on_open_findings_either(self):
        # With no high or medium finding there is no open finding to stop on: the tour found
        # nothing in the areas it saw, and the rest were never looked at.
        self.assertIsNone(self.stop(partial(numbered(["low"]), new_cards=0)))
        planning = finding("high", area="Billing", **{testloop.ATTENDED_KEY: True})
        self.assertIsNone(self.stop(partial([planning], new_cards=0)))

    def test_a_partial_tour_whose_serious_findings_made_no_card_still_stops_on_open_findings(self):
        self.assertEqual(self.stop(partial(numbered(["high"]), new_cards=0)),
                         testloop.STOP_OPEN_FINDINGS)

    def test_a_partial_tour_that_filed_a_card_goes_on(self):
        self.assertIsNone(self.stop(partial(numbered(["high"]), new_cards=1), cards=1))

    def test_the_caps_and_report_only_still_stop_a_partial_tour(self):
        self.assertEqual(self.stop(partial(numbered(["low"])), report_only=True),
                         testloop.STOP_REPORT_ONLY)
        self.assertEqual(self.stop(partial(), cards=30), testloop.STOP_BUDGET)
        self.assertEqual(self.stop(partial(numbered(["high"]), new_cards=1), rounds=6, cards=6),
                         testloop.STOP_ROUNDS)
        self.assertEqual(self.stop(partial(), hours=24), testloop.STOP_CLOCK)

    def test_a_partial_check_goes_on_like_any_check(self):
        self.assertIsNone(self.stop(partial(kind=testloop.CHECK)))
        self.assertIsNone(self.stop(partial(numbered(["high"]), kind=testloop.CHECK)))

    def test_untoured_on_a_pass_that_did_not_run_changes_nothing(self):
        for status in (testloop.NOT_RUN, testloop.FAILED):
            with self.subTest(status=status):
                self.assertFalse(testloop.is_partial(partial(status=status)))
                self.assertIsNone(self.stop(partial(status=status), rounds=6))
                self.assertEqual(self.stop(partial(status=status), hours=25),
                                 testloop.STOP_CLOCK)

    def test_a_pass_result_names_no_untoured_area_by_default(self):
        self.assertEqual(tour().untoured, ())
        self.assertEqual(self.stop(tour(numbered(["low"]))), testloop.STOP_CLEAN)


if __name__ == "__main__":
    unittest.main()
