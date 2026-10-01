#!/usr/bin/env python3
"""Fail when a dbt package declares a dependency that can resolve to different source later.

Why this exists: a MaxCompute dbt project that installs these packages must be reproducible.
`revision: main` and hub version ranges both silently change under a fixed commit of
`packages.yml`, so a build that worked yesterday can compile different macros today.

Rules checked in the project directory (default: repo root, where `packages.yml` lives):
  R1  git dependency   -> `revision` is a full 40-char commit sha, or a tag that exists as a
                          tag on the remote. A name that exists as a *branch* is a violation.
  R2  hub dependency   -> `version` is one exact version. Ranges (lists) and Jinja templates
                          are violations.
  R3  `package-lock.yml` exists next to `packages.yml`, lists the same top-level packages,
      records a full sha for every git entry, and every tag-pinned git entry still resolves
      on the remote to exactly the sha recorded in the lock.
  R4  the lock has no entry that `packages.yml` no longer declares (stale lock). A lock entry
      that is not declared is treated as transitive when it is declared by an installed package's
      own `packages.yml` under `dbt_packages/`; without `dbt_packages/` on disk the check cannot
      tell a transitive entry from a stale one, so R4 is skipped and says so. (A fresh CI runner
      is exactly that case, and there the lock-reproducibility step in the workflow is the check
      that catches staleness.)

`integration_tests/` projects are deliberately out of scope: they are the upstream
multi-adapter CI matrix, not what a MaxCompute user installs, and their getdbt version maps
are selected by dbt minor version.

Exit codes: 0 = clean, 1 = violations, 2 = the check could not run (bad input, no PyYAML).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

try:
    import yaml
except ImportError:                                    # pragma: no cover
    sys.stderr.write("PyYAML is required (it ships with dbt-core): pip install pyyaml\n")
    raise SystemExit(2)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PEELED = {}                                            # url -> {ref: sha}


def remote_refs(url: str) -> dict:
    """All refs of a git remote, peeled tags folded in. Cached per url."""
    if url not in PEELED:
        try:
            out = subprocess.run(["git", "ls-remote", url], capture_output=True, text=True,
                                 timeout=120, check=True).stdout
        except Exception as exc:                        # noqa: BLE001
            sys.stderr.write(f"WARN cannot list remote refs for {url}: {exc}\n")
            out = ""
        refs = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                base = ref[:-3]
                refs[base] = sha                        # annotated tag -> commit
            elif ref not in refs or not ref.startswith("refs/tags/"):
                refs[ref] = sha
        PEELED[url] = refs
    return PEELED[url]


def classify_git(revision: str, url: str):
    """-> (kind, detail). kind in pinned-sha / pinned-tag / floating-branch / unknown-ref."""
    if SHA_RE.match(revision or ""):
        return "pinned-sha", revision
    refs = remote_refs(url) if url else {}
    is_branch = f"refs/heads/{revision}" in refs
    is_tag = f"refs/tags/{revision}" in refs
    if is_tag and not is_branch:
        return "pinned-tag", refs[f"refs/tags/{revision}"]
    if is_branch:
        return "floating-branch", refs[f"refs/heads/{revision}"]
    if is_tag:                                          # a tag AND a branch with the same name
        return "ambiguous-ref", refs[f"refs/tags/{revision}"]
    return "unknown-ref", ""


def transitive_identities(project_dir: str):
    """Lock entries that an installed package declares for itself, or None if we cannot look."""
    pkgs = pathlib.Path(project_dir, "dbt_packages")
    if not pkgs.is_dir():
        return None, False
    found = set()
    for sub in sorted(pkgs.iterdir()):
        child = sub / "packages.yml"
        if not child.is_file():
            continue
        try:
            body = yaml.safe_load(child.read_text()) or {}
        except Exception:                                    # noqa: BLE001
            continue
        for entry in body.get("packages") or []:
            found.add(dep_identity(entry))
    return found, True


def read_project(project_dir: str):
    pkg_path = os.path.join(project_dir, "packages.yml")
    lock_path = os.path.join(project_dir, "package-lock.yml")
    if not os.path.exists(pkg_path):
        raise SystemExit(f"no packages.yml in {project_dir}")
    with open(pkg_path) as fh:
        pkg = yaml.safe_load(fh) or {}
    lock = None
    if os.path.exists(lock_path):
        with open(lock_path) as fh:
            lock = yaml.safe_load(fh) or {}
    return pkg.get("packages") or [], lock, pkg_path, lock_path


def norm_url(url: str) -> str:
    return str(url).rstrip("/").removesuffix(".git")


def dep_identity(entry: dict):
    """Comparable identity between packages.yml and package-lock.yml.

    `name` is deliberately not part of the identity: packages.yml may omit it, while
    package-lock.yml always writes the project name it installed under. An explicitly declared
    name is still compared, separately, in R3.
    """
    if "git" in entry:
        return ("git", norm_url(entry["git"]))
    if "package" in entry:
        return ("hub", str(entry["package"]))
    if "local" in entry:
        return ("local", os.path.normpath(entry["local"]))
    return ("other", str(entry))


lock_identity = dep_identity


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-dir", default=".", help="directory holding packages.yml")
    args = ap.parse_args()

    deps, lock, pkg_path, lock_path = read_project(args.project_dir)
    problems, checked, skipped = [], 0, []

    for entry in deps:
        checked += 1
        where = f"{os.path.relpath(pkg_path, os.getcwd()) or '.'}: {dep_identity(entry)}"
        if "git" in entry:
            kind, sha = classify_git(str(entry.get("revision", "")), entry["git"])
            if kind == "floating-branch":
                problems.append(f"R1 {where} uses branch '{entry['revision']}' "
                                f"(currently {sha[:9]}) -> pin a tag or a full commit sha")
            elif kind in ("unknown-ref", "ambiguous-ref"):
                problems.append(f"R1 {where} revision '{entry.get('revision')}' is neither a "
                                f"40-char sha nor a unique remote tag ({kind})")
        elif "package" in entry:
            ver = entry.get("version")
            if not isinstance(ver, str) or not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.\-]*$",
                                                        ver.strip()):
                problems.append(f"R2 {where} version {ver!r} is a range or template "
                                f"-> pin one exact version")
        # local deps are reproducible by definition.

    declared = [dep_identity(e) for e in deps]
    if lock is None:
        if any(d[0] != "local" for d in declared):
            problems.append(f"R3 no package-lock.yml next to {os.path.relpath(pkg_path)} "
                            f"-> run `dbt deps` and commit the lock")
    else:
        lock_entries = lock.get("packages") or []
        locked = [lock_identity(e) for e in lock_entries]
        for entry, want in zip(deps, declared):
            if want[0] == "local":
                continue
            if want not in locked:
                problems.append(f"R3 {want} is declared in packages.yml but missing from "
                                f"package-lock.yml -> regenerate the lock")
                continue
            if entry.get("name"):        # an explicitly declared install name must be honoured
                lock_match = [e for e in lock_entries if lock_identity(e) == want]
                if lock_match and lock_match[0].get("name") != entry["name"]:
                    problems.append(f"R3 {want} declares name '{entry['name']}' but "
                                    f"package-lock.yml records '{lock_match[0].get('name')}'")
        transitive, unknown_source = transitive_identities(args.project_dir)
        if transitive is None:
            skipped.append("R4 (no dbt_packages/ on disk: a non-declared lock entry cannot be "
                           "told apart from a stale one; the workflow's `dbt deps` diff covers it)")
        else:
            for have in locked:
                if have not in declared and have not in transitive:
                    problems.append(f"R4 {have} is in package-lock.yml, is not declared, and no "
                                    f"installed package declares it -> regenerate the lock")
        for entry in lock_entries:
            if "git" in entry:
                rev = str(entry.get("revision", ""))
                if not SHA_RE.match(rev):
                    problems.append(f"R3 lock entry {lock_identity(entry)} records a "
                                    f"non-sha revision '{rev}' -> regenerate the lock")
        # tag drift: the tag must still point at the sha the lock was built from
        for entry in deps:
            if "git" not in entry:
                continue
            kind, sha = classify_git(str(entry.get("revision", "")), entry["git"])
            if kind != "pinned-tag":
                continue
            match = [e for e in lock_entries
                     if lock_identity(e) == dep_identity(entry) and SHA_RE.match(str(e.get("revision", "")))]
            if match and match[0]["revision"] != sha:
                problems.append(f"R3 tag '{entry['revision']}' now resolves to {sha[:9]}, "
                                f"package-lock.yml says {match[0]['revision'][:9]} "
                                f"-> the tag moved; pin the commit sha instead")

    print(f"checked {checked} declared dependencies in {os.path.relpath(pkg_path)} "
          f"({'with' if lock is not None else 'without'} package-lock.yml), "
          f"violations {len(problems)}")
    for note in skipped:
        print("  SKIP " + note)
    for p in problems:
        print("  FAIL " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
