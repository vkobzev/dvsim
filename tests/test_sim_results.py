# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Tests for dvsim.sim_results."""

from pathlib import Path

import pytest
from hamcrest import assert_that, equal_to, has_key, none

from dvsim.job.data import CompletedJobStatus, JobStatusInfo, WorkspaceConfig
from dvsim.job.status import JobStatus
from dvsim.report.data import IPMeta, ToolMeta
from dvsim.sim_results import BucketedFailures


def _ip_meta() -> IPMeta:
    return IPMeta(
        name="test_ip",
        commit="test_commit",
        commit_short="test",
        branch="test_branch",
        url="test_url",
        revision_info=None,
    )


def _workspace(tmp_path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        timestamp="test_timestamp",
        project_root=tmp_path / "root",
        scratch_root=tmp_path / "scratch",
        scratch_path=tmp_path / "scratch" / "test",
    )


def _failed_job(
    tmp_path: Path,
    *,
    name: str = "test_job",
    seed: int | None,
    fail_msg: str = "something went wrong",
) -> CompletedJobStatus:
    return CompletedJobStatus(
        name=name,
        job_type="mock_type",
        seed=seed,
        block=_ip_meta(),
        tool=ToolMeta(name="test_tool", version="test_version"),
        workspace_cfg=_workspace(tmp_path),
        full_name=name,
        qual_name=name,
        target="run",
        log_path=tmp_path / f"{name}.log",
        job_runtime=1.0,
        simulated_time=1.0,
        status=JobStatus.FAILED,
        fail_msg=JobStatusInfo(message=fail_msg),
    )


class TestBucketedFailures:
    """Tests for BucketedFailures.from_job_status."""

    @staticmethod
    def test_none_seed_does_not_crash(tmp_path: Path) -> None:
        """A failed job with a None seed is bucketed without raising.

        Regression test: previously from_job_status called int() on the seed
        unconditionally, raising TypeError when the seed was None.
        """
        job = _failed_job(tmp_path, seed=None)

        result = BucketedFailures.from_job_status([job])

        assert_that(len(result.buckets), equal_to(1))
        assert_that(result.buckets, has_key("something went wrong"))
        overview = result.buckets["something went wrong"][0]
        assert_that(overview.seed, none())

    @staticmethod
    @pytest.mark.parametrize("seed", [0, 1, 0x1FFFFFFFF, -1])
    def test_seed_is_masked_to_32_bits(tmp_path: Path, seed: int) -> None:
        """A failed job with an integer seed is masked to 32 bits."""
        job = _failed_job(tmp_path, seed=seed)

        result = BucketedFailures.from_job_status([job])

        overview = result.buckets["something went wrong"][0]
        assert_that(overview.seed, equal_to(seed & 0xFFFFFFFF))
