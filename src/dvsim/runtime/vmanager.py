# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Runtime backend that emits a Cadence vManager ``.vsif`` session file.

Instead of executing ``RunTest`` jobs, this backend accumulates them and, on
``close()``, renders a Cadence vManager *Verification Session Input Format*
(``.vsif``) file describing the tests. The resulting file can be uploaded into
vManager (the Cadence Enterprise/Verisium Manager) to run the simulations there.

Build (``CompileSim``) jobs are, by default, still executed locally via an
embedded :class:`LocalRuntimeBackend`, so that the compiled simulation artifact
exists and is referenced by the generated session. This can be disabled with
``build_mode='skip'`` (pure generation; the build must then be provided by the
vManager host).

Run jobs are marked ``PASSED`` synthetically: they are emitted into the session
rather than executed locally, so dvsim's own pass/fail report does not reflect
their actual outcome (which is determined by vManager).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from dvsim.job.data import JobSpec, JobStatusInfo
from dvsim.job.status import JobStatus
from dvsim.job.time import JobTime
from dvsim.logging import log
from dvsim.runtime.backend import RuntimeBackend
from dvsim.runtime.data import CompletionCallback, JobCompletionEvent, JobHandle
from dvsim.runtime.local import LocalRuntimeBackend
from dvsim.templates.render import render_template

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable

__all__ = ("VmanagerRuntimeBackend",)

# Packaged default template, relative to the dvsim templates directory.
DEFAULT_TEMPLATE = "vmanager/session.vsif.j2"

# job_types representing simulation runs -> emitted into the vsif.
_RUN_JOB_TYPE = "RunTest"
# job_types representing builds -> run locally unless build_mode == 'skip'.
_BUILD_JOB_TYPES = {"CompileSim", "CompileOneShot"}
# Coverage jobs are always skipped here; coverage is collected within vManager.
_COV_JOB_TYPES = {"CovUnr", "CovMerge", "CovReport", "CovAnalyze", "CovVPlan"}


def _sanitize_test_name(name: str) -> str:
    """Make a string safe to use as a vsif ``test`` block name."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "test"


class VmanagerRuntimeBackend(RuntimeBackend):
    """Backend that generates vManager ``.vsif`` session files."""

    name = "vmanager"

    # Class-level defaults, set from the CLI before the backend is instantiated
    # (mirrors how RunTest.seeds is configured). May be overridden per-instance.
    # 'flist' is the default: dvsim runs only the fusesoc file-list generation
    # (the step vManager cannot do itself) and does NOT compile the snapshot;
    # vManager then compiles + runs each test from the generated file list.
    build_mode_default: str = "flist"
    vsif_template_default: str | None = None

    def __init__(
        self,
        *,
        build_mode: str | None = None,
        vsif_template: str | None = None,
        max_parallelism: int | None = None,
    ) -> None:
        """Construct a vmanager runtime backend.

        Args:
            build_mode: How to handle build (CompileSim) jobs.
              ``'flist'`` (default) runs only the file-list generation
              (fusesoc) locally, neutralising the compile step; vManager then
              compiles + runs from the generated file list.
              ``'local'`` runs the full build locally (file list + snapshot),
              so vManager only runs the prebuilt snapshot.
              ``'skip'`` runs nothing - both build and runs are deferred to
              vManager (the build must be provided by the vManager host).
            vsif_template: Optional path to a custom Jinja2 vsif template. If
              ``None`` (or the path does not exist), the packaged default is used.
              Falls back to the ``DVSIM_VMANAGER_TEMPLATE`` environment variable.
            max_parallelism: Forwarded to the embedded local backend used for builds.

        """
        super().__init__(max_parallelism=max_parallelism)
        build_mode = build_mode or self.build_mode_default
        if build_mode not in ("local", "skip", "flist"):
            msg = (
                f"Invalid vmanager build_mode {build_mode!r}; "
                "expected 'local', 'skip' or 'flist'."
            )
            raise ValueError(msg)
        self.build_mode = build_mode
        self.vsif_template = vsif_template or self.vsif_template_default
        if self.vsif_template is None:
            self.vsif_template = os.environ.get("DVSIM_VMANAGER_TEMPLATE")

        # Captured run-test entries, grouped by block name.
        self._entries: dict[str, list[dict[str, Any]]] = {}
        # Workspace config per block, used to resolve the vsif output location.
        self._block_ws: dict[str, Any] = {}

        # Builds run for real through the local backend.
        self._local = LocalRuntimeBackend(max_parallelism=max_parallelism)
        self._written: list[Path] = []

    def attach_completion_callback(self, callback: CompletionCallback) -> None:  # type: ignore[name-defined]
        """Forward the scheduler's completion callback to the embedded local backend."""
        super().attach_completion_callback(callback)
        self._local.attach_completion_callback(callback)

    def _record_run(self, job: JobSpec) -> None:
        """Capture a RunTest job as a vsif test entry."""
        md: dict[str, Any] = dict(job.metadata)
        run_cmd = str(md.get("run_cmd", "") or "").strip()
        run_opts = md.get("run_opts", []) or []
        opts_str = " ".join(str(o).strip() for o in run_opts if str(o).strip())
        run_dir = str(md.get("run_dir", "") or "").strip()
        build_cmd = str(md.get("build_cmd", "") or "").strip()

        run_command = self._build_run_command(job, run_cmd, opts_str, build_cmd)

        entry: dict[str, Any] = {
            "test_name": _sanitize_test_name(job.qual_name),
            "qual_name": job.qual_name,
            "base_name": job.name,
            "full_name": job.full_name,
            "seed": md.get("svseed", job.seed),
            "seed_explicit": job.seed is not None,
            "tool": job.tool.name,
            "run_command": run_command,
            "make_cmd": job.cmd,
            "run_cmd": run_cmd,
            "run_opts": list(run_opts),
            "build_cmd": build_cmd,
            "build_opts": list(md.get("build_opts", []) or []),
            "uvm_test": str(md.get("uvm_test", "") or ""),
            "uvm_test_seq": str(md.get("uvm_test_seq", "") or ""),
            "build_mode": str(md.get("build_mode", "") or ""),
            "build_dir": str(md.get("build_dir", "") or ""),
            "flist_file": str(md.get("flist_file", "") or ""),
            "run_dir": run_dir,
            "log_path": str(job.log_path),
            "timeout_mins": job.timeout_mins,
            "pre_run_cmds": list(md.get("pre_run_cmds", []) or []),
            "post_run_cmds": list(md.get("post_run_cmds", []) or []),
            "sw_images": list(md.get("sw_images", []) or []),
        }
        block = job.block.name
        self._entries.setdefault(block, []).append(entry)
        self._block_ws.setdefault(block, job.workspace_cfg)

    def _build_run_command(
        self, job: JobSpec, run_cmd: str, opts_str: str, build_cmd: str
    ) -> str:
        """Compute the vsif ``run_command`` for a test, based on the build mode.

        - ``flist``: dvsim only generated the file list, so vManager must both
          compile (``build_cmd``) and run (``run_cmd`` + run_opts).
        - ``local``/``skip``: the snapshot is assumed to exist (built by dvsim or
          provided externally), so vManager only runs it (``run_cmd`` + run_opts).
        """
        run_part = f"{run_cmd} {opts_str}".strip() if run_cmd else job.cmd
        if self.build_mode == "flist" and build_cmd:
            run_part = f"{build_cmd} && {run_part}"
        return run_part

    async def submit_many(self, jobs: Iterable[JobSpec]) -> dict[Hashable, JobHandle]:
        """Submit jobs: capture runs, delegate builds locally, skip the rest."""
        jobs = list(jobs)
        completions: list[JobCompletionEvent] = []
        handles: dict[Hashable, JobHandle] = {}
        build_jobs: list[JobSpec] = []

        for job in jobs:
            if job.job_type == _RUN_JOB_TYPE:
                self._record_run(job)
                handles[job.id] = self._synth_handle(job)
                completions.append(
                    JobCompletionEvent(
                        job,
                        JobStatus.PASSED,
                        JobStatusInfo(message="Emitted to vmanager session; not executed locally."),
                    )
                )
            elif job.job_type in _BUILD_JOB_TYPES:
                if self.build_mode == "skip":
                    handles[job.id] = self._synth_handle(job)
                    completions.append(
                        JobCompletionEvent(
                            job,
                            JobStatus.PASSED,
                            JobStatusInfo(message="Skipped: vmanager build_mode=skip."),
                        )
                    )
                else:
                    # 'local' runs the full build; 'flist' runs only the
                    # file-list generation by neutralising the compile step.
                    build_jobs.append(self._flist_job(job) if self.build_mode == "flist" else job)
            elif job.job_type in _COV_JOB_TYPES:
                handles[job.id] = self._synth_handle(job)
                completions.append(
                    JobCompletionEvent(
                        job,
                        JobStatus.PASSED,
                        JobStatusInfo(message=f"Skipped by vmanager backend ({job.job_type})."),
                    )
                )
            else:
                handles[job.id] = self._synth_handle(job)
                completions.append(
                    JobCompletionEvent(
                        job,
                        JobStatus.PASSED,
                        JobStatusInfo(message=f"Skipped by vmanager backend ({job.job_type})."),
                    )
                )

        # Delegate builds to the local backend for real execution.
        if build_jobs:
            handles.update(await self._local.submit_many(build_jobs))

        if completions:
            await self._emit_completion(completions)
        return handles

    def _synth_handle(self, job: JobSpec) -> JobHandle:
        return JobHandle(
            spec=job, backend=self.name, job_runtime=JobTime(), simulated_time=JobTime()
        )

    @staticmethod
    def _flist_job(job: JobSpec) -> JobSpec:
        """Return a copy of a build job whose compile step is neutralised.

        Appending ``build_cmd=true`` to the make command makes GNU make use the
        last assignment, so the (slow) compile becomes a no-op while the
        file-list generation (``sv_flist_gen_cmd``) still runs. This yields the
        file list that vManager needs, without building the snapshot.
        """
        return job.model_copy(update={"cmd": f"{job.cmd} build_cmd=true"})

    async def kill_many(self, handles: Iterable[JobHandle]) -> None:
        """Kill any locally-running (build) jobs; recorded runs need no action."""
        await self._local.kill_many(handles)

    async def close(self) -> None:
        """Finalize local jobs, then render the vsif session files."""
        await self._local.close()
        self._write_vsif_files()

    def _render(self, context: dict[str, Any]) -> str:
        tpl = self.vsif_template
        if tpl and Path(tpl).is_file():
            env = Environment(
                loader=FileSystemLoader(str(Path(tpl).parent)),
                autoescape=select_autoescape(),
            )
            return env.get_template(Path(tpl).name).render(**context)
        return render_template(DEFAULT_TEMPLATE, context)

    def _write_vsif_files(self) -> None:
        """Write one vsif per block, grouping tests by build_mode."""
        if not self._entries:
            log.info("[vmanager] No RunTest jobs captured; no vsif file to write.")
            return

        total = 0
        for block, entries in self._entries.items():
            ws = self._block_ws.get(block)
            if ws is None:
                continue
            out_dir = Path(ws.scratch_path) / "vmanager"
            out_dir.mkdir(parents=True, exist_ok=True)
            vsif_path = out_dir / f"{block}.vsif"

            # Group tests by build_mode so each group can share a build snapshot.
            groups: dict[str, list[dict[str, Any]]] = {}
            for e in entries:
                groups.setdefault(e["build_mode"] or "default", []).append(e)

            context = {
                "block": block,
                "session_name": f"{block}_dvsim_{ws.timestamp}",
                "top_dir": str(out_dir),
                "groups": [
                    {
                        "name": f"{block}_{mode}",
                        "build_mode": mode,
                        "timeout_secs": _max_timeout_secs(entries_mode),
                        "tests": entries_mode,
                    }
                    for mode, entries_mode in groups.items()
                ],
            }
            content = self._render(context)
            vsif_path.write_text(content, encoding="utf-8")
            self._written.append(vsif_path)
            total += len(entries)
            log.info("[vmanager] Wrote %d test(s) to %s", len(entries), vsif_path)

        log.info(
            "[vmanager] Done. %d test(s) across %d session file(s). "
            "Tests were not executed locally - run them in vManager.",
            total,
            len(self._written),
        )
        for p in self._written:
            log.info("[vmanager] session file: %s", p)


def _max_timeout_secs(tests: list[dict[str, Any]]) -> int:
    """Largest per-test timeout (in seconds) across a group, with a sane default."""
    secs = [
        int(float(t["timeout_mins"]) * 60)
        for t in tests
        if t.get("timeout_mins") is not None
    ]
    return max(secs) if secs else 3600
