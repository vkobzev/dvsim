# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the vmanager runtime backend (.vsif generation)."""

import contextlib
from pathlib import Path

import pytest
from hamcrest import assert_that, contains_string, equal_to, instance_of
from pytest_mock import MockerFixture

from dvsim.job.data import JobSpec
from dvsim.job.status import JobStatus
from dvsim.runtime.data import JobCompletionEvent, JobHandle
from dvsim.runtime.registry import BackendType, backend_registry, register_backend
from dvsim.runtime.vmanager import VmanagerRuntimeBackend
from tests.test_scheduler import job_spec_factory


def test_vmanager_registered_in_registry() -> None:
    """The vmanager backend is registered and creatable (re-register if cleared)."""
    with contextlib.suppress(ValueError):
        # Already registered if the built-in registration is still present.
        register_backend(BackendType("vmanager"), VmanagerRuntimeBackend)
    backend = backend_registry.create(BackendType("vmanager"))
    assert_that(backend, instance_of(VmanagerRuntimeBackend))
    assert_that(backend.name, equal_to("vmanager"))


def test_invalid_build_mode_rejected() -> None:
    """An unknown build mode raises a ValueError."""
    with pytest.raises(ValueError, match="Invalid vmanager build_mode"):
        VmanagerRuntimeBackend(build_mode="bogus")


def test_default_build_mode_is_flist() -> None:
    """By default dvsim only generates the file list; vManager compiles + runs."""
    backend = VmanagerRuntimeBackend()
    assert_that(backend.build_mode, equal_to("flist"))


def test_jobspec_metadata_defaults_to_empty(tmp_path: Path) -> None:
    """JobSpec accepts an optional metadata field defaulting to an empty mapping."""
    job = job_spec_factory(tmp_path)
    assert_that(dict(job.metadata), equal_to({}))


def _run_test_spec(tmp_path: Path, **metadata: object) -> JobSpec:
    """Build a RunTest JobSpec carrying vmanager-relevant metadata."""
    md: dict[str, object] = {
        "run_cmd": "xrun -R -snapshot default.xms",
        "run_opts": ["+UVM_TESTNAME=my_test", "+en_scb=1"],
        "uvm_test": "my_test",
        "uvm_test_seq": "my_vseq",
        "svseed": 1701,
        "build_mode": "default",
        "build_dir": "/scratch/build",
        "build_cmd": "xrun -elaborate -f src.f -snapshot default.xms",
        "build_opts": ["-uvmhome", "CDNS"],
        "flist_file": "/scratch/build/src.f",
        "run_dir": "/scratch/run",
    }
    md.update(metadata)
    return job_spec_factory(
        tmp_path,
        job_type="RunTest",
        target="run",
        name="my_test",
        qual_name="0.my_test",
        seed=1701,
        metadata=md,
    )


@pytest.mark.asyncio
async def test_runtest_emitted_and_vsif_written(tmp_path: Path) -> None:
    """A RunTest job is captured and rendered into a .vsif with full metadata."""
    backend = VmanagerRuntimeBackend(build_mode="skip")
    events: list[JobCompletionEvent] = []

    async def on_complete(batch: list[JobCompletionEvent]) -> None:
        events.extend(batch)

    backend.attach_completion_callback(on_complete)
    job = _run_test_spec(tmp_path)

    await backend.submit(job)
    await backend.close()

    # The run job completes synthetically (emitted, not executed).
    assert_that(len(events), equal_to(1))
    assert_that(events[0].status, equal_to(JobStatus.PASSED))

    vsif = tmp_path / "scratch" / "test" / "vmanager" / f"{job.block.name}.vsif"
    assert_that(vsif.exists(), equal_to(True))
    text = vsif.read_text(encoding="utf-8")

    # Cadence vsif structure.
    assert_that(text, contains_string("session "))
    assert_that(text, contains_string("group "))
    assert_that(text, contains_string("test "))
    assert_that(text, contains_string("run_command :"))

    # The resolved simulator invocation and metadata are carried through.
    assert_that(text, contains_string("xrun -R -snapshot default.xms"))
    assert_that(text, contains_string("+UVM_TESTNAME=my_test"))
    assert_that(text, contains_string("+en_scb=1"))
    assert_that(text, contains_string("seed : 1701"))


@pytest.mark.asyncio
async def test_build_mode_skip_does_not_run_builds(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """With build_mode=skip, CompileSim jobs are not delegated to the local backend."""
    backend = VmanagerRuntimeBackend(build_mode="skip")
    local_submit = mocker.patch.object(
        backend._local,  # noqa: SLF001
        "submit_many",
        new=mocker.AsyncMock(),
    )
    events: list[JobCompletionEvent] = []

    async def on_complete(batch: list[JobCompletionEvent]) -> None:
        events.extend(batch)

    backend.attach_completion_callback(on_complete)

    job = job_spec_factory(tmp_path, job_type="CompileSim", target="build", cmd="echo build")
    handle = await backend.submit(job)

    local_submit.assert_not_called()
    # The build is marked passed synthetically rather than executed.
    assert_that(len(events), equal_to(1))
    assert_that(events[0].status, equal_to(JobStatus.PASSED))
    assert_that(handle, instance_of(JobHandle))
    assert_that(handle.backend, equal_to("vmanager"))


@pytest.mark.asyncio
async def test_build_mode_local_delegates_builds(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """With build_mode=local, CompileSim jobs are delegated to the local backend."""
    backend = VmanagerRuntimeBackend(build_mode="local")
    local_submit = mocker.patch.object(
        backend._local,  # noqa: SLF001
        "submit_many",
        new=mocker.AsyncMock(return_value={}),
    )

    async def on_complete(batch: list[JobCompletionEvent]) -> None:
        del batch

    backend.attach_completion_callback(on_complete)

    job = job_spec_factory(tmp_path, job_type="CompileSim", target="build", cmd="echo build")
    await backend.submit(job)

    local_submit.assert_called_once()


@pytest.mark.asyncio
async def test_flist_neutralises_compile_step(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """build_mode=flist runs the build with the compile step neutralised."""
    backend = VmanagerRuntimeBackend(build_mode="flist")
    local_submit = mocker.patch.object(
        backend._local,  # noqa: SLF001
        "submit_many",
        new=mocker.AsyncMock(return_value={}),
    )

    async def on_complete(batch: list[JobCompletionEvent]) -> None:
        del batch

    backend.attach_completion_callback(on_complete)

    job = job_spec_factory(
        tmp_path,
        job_type="CompileSim",
        target="build",
        cmd="make -f flow.mk build build_cmd='xrun -compile'",
    )
    await backend.submit(job)

    local_submit.assert_called_once()
    submitted = local_submit.call_args[0][0]
    # The compile (build_cmd) is neutralised so only the file list is generated.
    assert_that(submitted[0].cmd, contains_string(" build_cmd=true"))


@pytest.mark.asyncio
async def test_flist_run_command_compiles_and_runs(tmp_path: Path) -> None:
    """build_mode=flist emits a run_command that compiles (build_cmd) then runs."""
    backend = VmanagerRuntimeBackend(build_mode="flist")
    events: list[JobCompletionEvent] = []

    async def on_complete(batch: list[JobCompletionEvent]) -> None:
        events.extend(batch)

    backend.attach_completion_callback(on_complete)
    job = _run_test_spec(tmp_path)

    await backend.submit(job)
    await backend.close()

    vsif = tmp_path / "scratch" / "test" / "vmanager" / f"{job.block.name}.vsif"
    text = vsif.read_text(encoding="utf-8")
    # flist mode: vManager must compile (build_cmd) and then run (run_cmd + opts).
    assert_that(text, contains_string("xrun -elaborate -f src.f"))
    assert_that(text, contains_string("xrun -R -snapshot default.xms"))
    assert_that(text, contains_string(" && "))


@pytest.mark.asyncio
async def test_coverage_jobs_skipped(tmp_path: Path) -> None:
    """Coverage jobs are emitted as synthetic passes (coverage is collected in vManager)."""
    backend = VmanagerRuntimeBackend(build_mode="skip")
    events: list[JobCompletionEvent] = []

    async def on_complete(batch: list[JobCompletionEvent]) -> None:
        events.extend(batch)

    backend.attach_completion_callback(on_complete)

    job = job_spec_factory(tmp_path, job_type="CovReport", target="cov_report")
    await backend.submit(job)

    assert_that(len(events), equal_to(1))
    assert_that(events[0].status, equal_to(JobStatus.PASSED))
