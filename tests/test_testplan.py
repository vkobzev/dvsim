# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the testplan: mapping of simulation results to testpoints."""

import logging

import pytest

from dvsim.testplan import Covergroup, Result, Testplan, Testpoint


def make_result(name: str, passing: int = 1, total: int = 1) -> Result:
    return Result(name, passing=passing, total=total)


def make_testpoint(tests: list[str], name: str = "tp", stage: str = "V1") -> Testpoint:
    return Testpoint({"name": name, "desc": "desc", "stage": stage, "tests": tests})


def make_covergroup(name: str) -> Covergroup:
    return Covergroup({"name": name, "desc": "desc"})


@pytest.fixture
def dvsim_log(caplog):
    """Capture dvsim's own logger, which deliberately does not propagate."""
    logger = logging.getLogger("dvsim")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="dvsim")
    yield caplog
    logger.removeHandler(caplog.handler)


def mapped_names(testpoint: Testpoint) -> list[str]:
    return [tr.name for tr in testpoint.test_results if tr.total != 0]


def not_run_names(testpoint: Testpoint) -> list[str]:
    return [tr.name for tr in testpoint.test_results if tr.total == 0]


def write_testplan(tmp_path, body: str) -> Testplan:
    filename = tmp_path / "foo_testplan.hjson"
    filename.write_text(body)
    return Testplan(str(filename), tmp_path, "foo")


class TestExactMapping:
    def test_exact_names_unchanged(self):
        """Exact names map exactly as before wildcards were introduced."""
        tp = make_testpoint(["foo_smoke", "foo_full"])
        results = [make_result("foo_smoke"), make_result("bar_other")]

        tp.map_test_results(results)

        assert mapped_names(tp) == ["foo_smoke"]
        assert not_run_names(tp) == ["foo_full"]
        assert results[0].mapped is True
        assert results[1].mapped is False

    def test_no_tests_indicates_not_run(self):
        tp = make_testpoint([])
        tp.map_test_results([make_result("foo_smoke")])

        assert [tr.name for tr in tp.test_results] == ["tp"]

    def test_not_mapped_testpoint_is_skipped(self):
        tp = make_testpoint(["N/A"])
        tp.map_test_results([make_result("N/A")])

        assert tp.test_results == []


class TestPatternMapping:
    def test_star_maps_multiple_tests(self):
        tp = make_testpoint(["foo_*"])
        results = [make_result("foo_smoke"), make_result("foo_full"), make_result("bar_smoke")]

        tp.map_test_results(results)

        assert sorted(mapped_names(tp)) == ["foo_full", "foo_smoke"]
        assert not_run_names(tp) == []
        assert results[2].mapped is False

    def test_question_mark_matches_single_char(self):
        tp = make_testpoint(["foo_?"])
        results = [make_result("foo_1"), make_result("foo_12")]

        tp.map_test_results(results)

        assert mapped_names(tp) == ["foo_1"]
        assert results[1].mapped is False

    def test_bracket_is_literal(self):
        """A '[' alone must not turn the name into a character class."""
        tp = make_testpoint(["foo[0]"])
        results = [make_result("foo[0]"), make_result("foo0")]

        tp.map_test_results(results)

        assert mapped_names(tp) == ["foo[0]"]
        assert results[1].mapped is False

    def test_pattern_matching_nothing_is_not_run(self):
        tp = make_testpoint(["bar_*"])
        tp.map_test_results([make_result("foo_smoke")])

        assert mapped_names(tp) == []
        assert not_run_names(tp) == ["bar_*"]

    def test_test_can_match_multiple_testpoints(self):
        tp1 = make_testpoint(["foo_*"], name="tp1")
        tp2 = make_testpoint(["foo_smoke"], name="tp2")
        results = [make_result("foo_smoke")]

        tp1.map_test_results(results)
        tp2.map_test_results(results)

        assert mapped_names(tp1) == ["foo_smoke"]
        assert mapped_names(tp2) == ["foo_smoke"]
        assert results[0].mapped is True

    def test_exact_and_pattern_both_satisfied(self):
        tp = make_testpoint(["foo_smoke", "foo_*"])
        tp.map_test_results([make_result("foo_smoke")])

        assert mapped_names(tp) == ["foo_smoke"]
        assert not_run_names(tp) == []

    def test_substitutions_combine_with_patterns(self):
        """{brace} substitutions resolve first, globs are matched afterwards."""
        tp = make_testpoint(["{name}_*"])
        tp.do_substitutions({"name": "foo"}, reserved_names=())

        assert tp.tests == ["foo_*"]

        tp.map_test_results([make_result("foo_jtag")])
        assert mapped_names(tp) == ["foo_jtag"]


class TestStageRegressions:
    def test_patterns_excluded_from_stage_regressions(self, dvsim_log):
        plan = Testplan.__new__(Testplan)
        plan.testpoints = [
            make_testpoint(["foo_smoke", "foo_*"], name="tp1", stage="V1"),
            make_testpoint(["N/A"], name="tp2", stage="V2"),
        ]

        regressions = plan.get_stage_regressions()

        assert regressions == [{"name": "V1", "tests": ["foo_smoke"]}]
        assert "foo_*" in dvsim_log.text


class TestCovergroupMapping:
    def test_pattern_covergroup_matches(self, tmp_path):
        plan = write_testplan(
            tmp_path,
            """{
              name: foo
              covergroups: [
                {
                  name: "foo_*_cg"
                  desc: "desc"
                }
                {
                  name: bar_cg
                  desc: "desc"
                }
              ]
            }""",
        )

        plan.map_covergroups(["foo_jtag_cg"])

        assert plan.progress["Covergroups"] == {
            "total": 2,
            "written": 1,
            "passing": 1,
            "progress": "50.00 %",
        }

    def test_exact_covergroup_unchanged(self, tmp_path):
        plan = write_testplan(
            tmp_path,
            """{
              name: foo
              covergroups: [
                {
                  name: foo_cg
                  desc: "desc"
                }
              ]
            }""",
        )

        plan.map_covergroups(["foo_cg"])

        assert plan.progress["Covergroups"]["written"] == 1


class TestFullTestplanMapping:
    def test_unmapped_tests_bucket(self, tmp_path):
        plan = write_testplan(
            tmp_path,
            """{
              name: foo
              testpoints: [
                {
                  name: smoke
                  desc: "smoke"
                  stage: V1
                  tests: ["foo_*"]
                }
              ]
            }""",
        )
        plan.map_test_results([make_result("foo_smoke"), make_result("rogue_test")])

        unmapped = [tp for tp in plan.testpoints if tp.name == "Unmapped tests"]
        assert len(unmapped) == 1
        assert [tr.name for tr in unmapped[0].test_results] == ["rogue_test"]
        assert plan.progress["V1"]["total"] == 1
        assert plan.progress["V1"]["passing"] == 1

    def test_pattern_not_run_row(self, tmp_path):
        plan = write_testplan(
            tmp_path,
            """{
              name: foo
              testpoints: [
                {
                  name: smoke
                  desc: "smoke"
                  stage: V1
                  tests: ["foo_smoke", "foo_never_*"]
                }
              ]
            }""",
        )
        plan.map_test_results([make_result("foo_smoke")])

        # The written test counts towards progress; the pattern that matched
        # nothing shows up as a 0/0 "not run" row in the testpoint. The
        # not-run row raises the planned total, exactly like a written test
        # that has not been run yet.
        smoke = next(tp for tp in plan.testpoints if tp.name == "smoke")
        assert mapped_names(smoke) == ["foo_smoke"]
        assert not_run_names(smoke) == ["foo_never_*"]
        assert plan.progress["V1"]["total"] == 2
        assert plan.progress["V1"]["written"] == 1


def test_percentage_helper():
    assert Testplan._get_percentage(1, 4) == "25.00 %"
    assert Testplan._get_percentage(0, 0) == "-- %"
    with pytest.raises(ValueError):
        Testplan._get_percentage(2, 1)
