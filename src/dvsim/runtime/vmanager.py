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


# Matches an inline seed assignment, capturing the prefix, e.g. ``+SVSEED=``.
_SEED_ASSIGN_RE = re.compile(r"^([+-]s?v?seed=)", re.IGNORECASE)
# Matches a seed flag that takes the value as the next token, e.g. ``-svseed``.
_SEED_FLAG_RE = re.compile(r"^[+-](sv_seed|svseed)$", re.IGNORECASE)

# vManager substitutes this with the per-run SV seed when ``sv_seed : random``.
VM_SV_SEED = "$BRUN_SV_SEED"


def _rewrite_seed_args(opts: Iterable[str]) -> tuple[list[str], bool]:
    """Rewrite hardcoded simulator seed values to vManager's ``$BRUN_SV_SEED``.

    ``+SVSEED=<n>`` becomes ``+SVSEED=$BRUN_SV_SEED`` and ``-svseed <n>`` becomes
    ``-svseed $BRUN_SV_SEED`` so that vManager's randomized seed (``sv_seed :
    random``) actually reaches the simulator instead of being pinned by dvsim.

    Returns ``(rewritten_opts, found)`` where ``found`` indicates whether a seed
    argument was present.
    """
    tokens = [str(o).strip() for o in opts if str(o).strip()]
    out: list[str] = []
    found = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        assign = _SEED_ASSIGN_RE.match(tok)
        if assign:
            out.append(f"{assign.group(1)}{VM_SV_SEED}")
            found = True
            i += 1
            continue
        if _SEED_FLAG_RE.match(tok) and i + 1 < len(tokens):
            out.append(tok)
            out.append(VM_SV_SEED)
            found = True
            i += 2
            continue
        out.append(tok)
        i += 1
    return out, found


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
        build_cmd = str(md.get("build_cmd", "") or "").strip()
        build_opts = list(md.get("build_opts", []) or [])
        run_dir = str(md.get("run_dir", "") or "").strip()

        # Point any hardcoded seed at vManager's randomized seed ($BRUN_SV_SEED,
        # set via `sv_seed : random` on the group) so vManager drives the seed.
        run_opts, seed_found = _rewrite_seed_args(md.get("run_opts", []) or [])
        run_command = self._build_run_script(run_cmd, run_opts, job, seed_found=seed_found)

        block = job.block.name
        entry: dict[str, Any] = {
            # Base test name (e.g. "chip_destroy_ext_sens1"). Reseed iterations of
            # the same test share this name and are collapsed into a single vsif
            # `test` block with a `count` attribute (see _write_vsif_files).
            "test_name": _sanitize_test_name(job.name),
            "qual_name": job.qual_name,
            "base_name": job.name,
            "full_name": job.full_name,
            "seed": md.get("svseed", job.seed),
            "tool": job.tool.name,
            "run_script": run_command,
            "make_cmd": job.cmd,
            "run_cmd": run_cmd,
            "run_opts": run_opts,
            "build_cmd": build_cmd,
            "build_opts": build_opts,
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
        self._entries.setdefault(block, []).append(entry)
        self._block_ws.setdefault(block, job.workspace_cfg)

    def _build_run_script(
        self,
        run_cmd: str,
        run_opts: list[str],
        job: JobSpec,
        *,
        seed_found: bool,
    ) -> str:
        """Compute the per-test ``run_script`` (run only).

        The compile step is emitted separately (at the group level, via
        ``pre_group_script`` in flist mode) so the snapshot is built once per
        group rather than recompiled for every test. The seed is wired to
        vManager's ``$BRUN_SV_SEED`` (set by ``sv_seed : random``); if the
        simulator command did not carry an explicit seed option, one is appended.
        """
        parts = [run_cmd, *run_opts] if run_cmd else [job.cmd]
        if not seed_found:
            parts.extend(["-svseed", VM_SV_SEED])
        return " ".join(p for p in parts if p)

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

            # Within each build_mode, collapse reseed iterations of the same test
            # into a single vsif `test` block carrying a `count` attribute (the
            # number of runs); vManager runs it that many times, each with a fresh
            # random seed thanks to `sv_seed : random`.
            collapsed_groups = {
                mode: _collapse_reseed(mode_entries) for mode, mode_entries in groups.items()
            }

            context = {
                "block": block,
                "session_name": f"{block}_dvsim_{ws.timestamp}",
                "top_dir": str(out_dir),
                "groups": [
                    {
                        "name": f"{block}_{mode}",
                        "build_mode": mode,
                        # In flist mode dvsim did not compile, so vManager builds
                        # the snapshot once per group (pre_group_script) using the
                        # file lists / build options. local/skip assume a snapshot
                        # already exists, so no group compile step is emitted.
                        "compile_script": (
                            _compile_script(tests_mode) if self.build_mode == "flist" else ""
                        ),
                        "timeout_secs": _max_timeout_secs(tests_mode),
                        "tests": tests_mode,
                    }
                    for mode, tests_mode in collapsed_groups.items()
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


def _compile_script(tests: list[dict[str, Any]]) -> str:
    """Build the group-level compile command (``pre_group_script``) for flist mode.

    Combines the (shared) ``build_cmd`` with the build options, which carry the
    file-list paths. Taken from the first test in the group since all tests in a
    group share the same build_mode.
    """
    if not tests:
        return ""
    first = tests[0]
    parts = [str(first.get("build_cmd", "") or "").strip(), *(first.get("build_opts", []) or [])]
    return " ".join(p.strip() for p in parts if p.strip())


def _collapse_reseed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse reseed iterations of the same test into one vsif test block.

    dvsim expands ``reseed: N`` into N jobs that differ only by seed. Since the
    seed is delegated to vManager (``sv_seed : random`` → ``$BRUN_SV_SEED``),
    those iterations are identical and are merged into a single entry whose
    ``count`` is the number of runs. Entries are returned in first-seen order.
    """
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in entries:
        name = entry["test_name"]
        if name not in by_name:
            merged = dict(entry)
            merged["count"] = 0
            by_name[name] = merged
            order.append(name)
        by_name[name]["count"] += 1
    return [by_name[name] for name in order]
