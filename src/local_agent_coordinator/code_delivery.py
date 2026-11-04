"""Read-only delivery progress for this task's explicitly linked workspaces."""
import hashlib
import time

from . import git_status, native_workspace


def _aggregate(values, empty_reason):
    if any(value["state"] == "pending" for value in values):
        return {"state": "pending", "reason": next(value["reason"] for value in values if value["state"] == "pending")}
    if not values or any(value["state"] == "unknown" for value in values):
        return {"state": "unknown", "reason": next((value["reason"] for value in values if value["state"] == "unknown"), empty_reason)}
    return {"state": "confirmed", "reason": None}


def _published_files(item, status, refs, changes, deadline):
    counts = {"total": len(changes), "matched": 0, "missing": 0, "different": 0, "mode_unknown": 0}
    content_matches = 0
    for name, wanted in changes.items():
        actual = git_status.committed_file(item["path"], refs["main_head"], status["project_path"], name, deadline=deadline)
        if wanted is not None and "executable" not in wanted:
            counts["mode_unknown"] += 1
        if (wanted is None and actual is None or wanted is not None and actual is not None
                and wanted["sha256"] == actual["sha256"]):
            content_matches += 1
        if wanted is None and actual is None:
            counts["matched"] += 1
        elif actual is None:
            counts["missing"] += 1
        elif (wanted is None or wanted["sha256"] != actual["sha256"]
              or "executable" in wanted and wanted["executable"] != actual["executable"]):
            counts["different"] += 1
        else:
            counts["matched"] += 1
    return counts, content_matches


def _published_status(canonical, changes, source, candidates, expected, deadline):
    """Map only explicit registrations or an exact accepted-delivery match."""
    unknown_counts = {key: None for key in ("total", "matched", "missing", "different", "mode_unknown")}
    if canonical is None:
        return {"state": "unknown" if expected else "confirmed",
                "reason": "published_work_unavailable" if expected else None, "files": unknown_counts}
    if changes is None:
        return {"state": "unknown", "reason": "published_work_baseline_unknown", "files": unknown_counts}
    counts = {"total": len(changes), "matched": 0, "missing": 0, "different": 0, "mode_unknown": 0}
    if not changes:
        return {"state": "confirmed", "reason": None, "files": counts}
    if source is not None:
        item, status, refs = source
        try:
            counts, _ = _published_files(item, status, refs, changes, deadline)
            return {"state": "confirmed" if counts["matched"] == counts["total"] else "pending",
                    "reason": None if counts["matched"] == counts["total"] else "published_work_not_on_mainline",
                    "files": counts, "workspace_id": item["id"]}
        except (git_status._GitError, KeyError, TypeError):
            return {"state": "unknown", "reason": "published_work_mainline_unconfirmed", "files": {**unknown_counts, "total": len(changes)}}
    checked = []
    for item, status, refs in candidates:
        evidence = item.get("source") or {}
        if not all(evidence.get(key) for key in ("asset_id", "run_id", "task_id", "result_sha256")):
            continue
        try:
            files, content_matches = _published_files(item, status, refs, changes, deadline)
            checked.append((item, files, content_matches))
        except (git_status._GitError, KeyError, TypeError):
            continue
    matches = [(item, files) for item, files, content_matches in checked
               if content_matches == len(changes) and any(wanted is not None for wanted in changes.values())]
    if len(matches) == 1:
        item, files = matches[0]
        complete = files["matched"] == files["total"]
        return {"state": "confirmed" if complete else "pending",
                "reason": None if complete else "published_work_not_on_mainline",
                "files": files, "workspace_id": item["id"]}
    # A sole partial match is useful evidence, not permission to infer where
    # missing files belong or silently exclude them from the task's delivery.
    if len(checked) == 1:
        item, files, _ = checked[0]
        return {"state": "unknown", "reason": "published_work_mapping_unknown", "files": files, "workspace_id": item["id"]}
    return {"state": "unknown", "reason": "published_work_mapping_unknown", "files": {**unknown_counts, "total": len(changes)}}


def summarize(workspaces, *, statuses, native_states, canonical, canonical_changes=None, retained_states=(), refresh_remote=False, now, deadline=None, delivery_repository=None, remote_cache=None):
    main, remote, private, repositories, roots = [], [], [], {}, []
    # Service calls always supply the host's binding. Keep the pure helper's
    # registered-project default for callers without room persistence.
    if delivery_repository is None:
        project = next((item for item in workspaces if item["kind"] == "project"), None)
        delivery_repository = {"state": "bound" if project else "unbound",
            "workspace_id": project["id"] if project else None, "reason": "delivery_repository_unbound" if project is None else None}
    target_id = delivery_repository["workspace_id"] if delivery_repository["state"] == "bound" else None
    workspace_checks = []
    candidates = []
    source = None
    private_ids = {item["path"]: item["id"] for item in workspaces if item["kind"] == "native"}
    tracked = untracked = 0
    counts_known = True
    counted_directories = set()
    for item in workspaces:
        if item["kind"] == "native":
            continue
        if target_id is not None and item["id"] != target_id:
            continue
        status = statuses.get(item["id"], {"state": "unknown", "reason": "not_checked"})
        target = item["id"] == target_id
        # A non-Git captured registration is not a second missing repository.
        if status["state"] == "not_git":
            continue
        if target and status["state"] != "ready":
            if status.get("repository_root") and status["repository_root"] not in roots:
                roots.append(status["repository_root"])
            workspace_checks.append({"state": "unknown", "reason": status.get("reason") or "workspace_unavailable", "workspace_id": item["id"]})
            counts_known = False
        elif target:
            root = status["repository_root"]
            if root not in roots:
                roots.append(root)
            directory_key = (root, status["project_path"])
            counts = status["uncommitted"]
            if directory_key not in counted_directories:
                counted_directories.add(directory_key)
                tracked += counts["tracked"]
                untracked += counts["untracked"]
                workspace_checks.append({"state": "pending" if counts["tracked"] else "confirmed",
                    "reason": "uncommitted_tracked_changes" if counts["tracked"] else None,
                    "workspace_id": item["id"], "tracked": counts["tracked"], "untracked": counts["untracked"]})
        try:
            refs = git_status.local_delivery_refs(item["path"], status.get("mainline", {}).get("name"), deadline=deadline)
        except git_status._GitError as error:
            if target:
                main.append({"state": "unknown", "reason": error.code, "workspace_id": item["id"]})
                remote.append({"state": "unknown", "reason": error.code, "workspace_id": item["id"]})
            continue
        root = refs["repository_root"]
        if target and root not in roots:
            roots.append(root)
        status = {**status, "repository_root": root, "project_path": refs["project_path"]}
        identity = (refs["common"], root, status["project_path"])
        if identity in repositories:
            continue
        if target:
            repositories[identity] = {**refs, "workspace_id": item["id"]}
        candidate = (item, status, refs)
        candidates.append(candidate)
        if target:
            # A user/registration binding is stronger than report references.
            # Keep exact missing/different counts instead of letting unrelated
            # evidence repositories dilute this mapping.
            source = candidate
        if target:
            main.append({"state": "confirmed" if refs["included"] else "pending",
                "reason": None if refs["included"] else "branch_not_on_mainline",
                "workspace_id": item["id"]})
    published = _published_status(canonical, canonical_changes, source, candidates,
        expected=any(item["kind"] == "project" for item in workspaces) or bool(native_states or retained_states), deadline=deadline)
    # Read private D against its receipt and the currently published S. The
    # coordinator's private baseline commits are unrelated to user Git history.
    for state in native_states:
        workspace_id = private_ids.get(state.get("project"))
        try:
            if deadline is not None and time.monotonic() >= deadline:
                private.append({"state": "unknown", "reason": "code_status_timeout", "workspace_id": workspace_id})
                continue
            if canonical is None or state.get("conflicts"):
                private.append({"state": "unknown", "reason": "private_workspace_unconfirmed", "workspace_id": workspace_id})
                continue
            allowed = sorted({entry["path"] for entry in state["files"]} | set(canonical))
            changes = native_workspace.collect_changes(state["project"], state, allowed, resolved_conflicts=[])
            unpublished = any(canonical.get(entry["path"]) != entry["sha256"] for entry in changes["files"])
            blocked = False
            for entry in changes["blocked"]:
                path = native_workspace._file(native_workspace._directory(state["project"]), entry["path"])
                if not path.is_file():
                    blocked = True
                    continue
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if canonical.get(entry["path"]) != actual or entry["reason"] == "native_mode_change_not_imported":
                    blocked = True
            if unpublished or blocked:
                private.append({"state": "pending", "reason": "private_work_not_published", "workspace_id": workspace_id})
        except (native_workspace.NativeWorkspaceError, OSError, ValueError, KeyError, TypeError):
            private.append({"state": "unknown", "reason": "private_workspace_unavailable", "workspace_id": workspace_id})
    for retained in retained_states:
        workspace_id = private_ids.get(retained.get("source_project")) or ("retained:" + str(retained.get("provider")))
        try:
            if deadline is not None and time.monotonic() >= deadline:
                private.append({"state": "unknown", "reason": "code_status_timeout", "workspace_id": workspace_id})
                continue
            root = native_workspace._directory(retained["source_project"])
            for entry in retained["observation"]["files"]:
                if entry.get("sha256") == entry.get("previous_sha256"):
                    continue
                path = native_workspace._file(root, entry["path"])
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != entry["sha256"]:
                    private.append({"state": "unknown", "reason": "retained_work_changed", "workspace_id": workspace_id})
                elif canonical is None:
                    private.append({"state": "unknown", "reason": "retained_work_unconfirmed", "workspace_id": workspace_id})
                elif canonical.get(entry["path"]) != actual:
                    private.append({"state": "pending", "reason": "retained_work_not_published", "workspace_id": workspace_id})
        except (native_workspace.NativeWorkspaceError, OSError, ValueError, KeyError, TypeError):
            private.append({"state": "unknown", "reason": "retained_work_unavailable", "workspace_id": workspace_id})
    checked, remote_checks = set(), {}
    for identity, refs in repositories.items():
        common = refs["common"]
        checked.add(common)
        key = (common, refs["mainline_name"], refs["main_head"])
        if key not in remote_checks:
            remote_checks[key] = (git_status.read_github_main(identity[1], refs["mainline_name"], refs["main_head"],
                now=now, deadline=deadline, cache=remote_cache, refresh=refresh_remote)
                if refresh_remote or remote_cache is not None else
                {"state": "unknown", "reason": "not_checked", "checked_at": None, "cached": False})
        remote.append({**remote_checks[key], "workspace_id": refs["workspace_id"]})
    if delivery_repository["state"] != "bound":
        reason = delivery_repository.get("reason") or "delivery_repository_unbound"
        main.append({"state": "unknown", "reason": reason})
        remote.append({"state": "unknown", "reason": reason})
    main_status = _aggregate(main, "no_linked_repository")
    main_status["checks"] = main
    remote_status = _aggregate(remote, "no_linked_repository")
    remote_status["checks"] = remote
    checked_times = [entry["checked_at"] for entry in remote if entry.get("checked_at") is not None]
    remote_status["checked_at"] = min(checked_times) if len(checked_times) == len(remote) and checked_times else None
    remote_status["cached"] = any(entry.get("cached") is True for entry in remote)
    private_status = _aggregate(private, "private_work_unconfirmed") if private else {"state": "confirmed", "reason": None}
    workspace_status = _aggregate(workspace_checks, "workspace_unavailable") if workspace_checks else {"state": "confirmed", "reason": None}
    overall = _aggregate([main_status, remote_status, published, private_status, workspace_status], "delivery_unconfirmed")
    issues = [{"scope": scope, **value} for scope, values in (("main", main), ("remote", remote),
              ("published", [published]), ("private", private), ("workspace", workspace_checks)) for value in values if value["state"] != "confirmed"]
    # Untracked entries may be assets or source. Surface them without guessing
    # whether they belong in Git, and never hide them behind a complete verdict.
    if overall["state"] == "confirmed" and untracked:
        overall = {"state": "needs_attention", "reason": "untracked_items_need_decision"}
    return {"scope": "task_linked_workspaces", "state": overall["state"], "reason": overall["reason"],
        "main": main_status, "remote": remote_status, "published": published, "private": private_status,
        "issues": issues,
        "workspace": {**workspace_status, "checks": workspace_checks, "tracked": tracked if counts_known else None, "untracked": untracked if counts_known else None},
        "coverage": {"repositories": len(checked), "private_workspaces": len(native_states), "retained_workspaces": len(retained_states)},
        "request": {"available": bool(roots), "repository_roots": roots,
            "targets": [{"repository_root": root, "target_branch": refs["mainline_name"]}
                for (common, root, scope), refs in repositories.items()],
            "reason": None if roots else delivery_repository.get("reason") or "no_linked_repository"}}
