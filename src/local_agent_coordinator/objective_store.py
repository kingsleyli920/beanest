"""Persistent objective limits and one bounded, version-bound checkpoint.

All methods run in RoomStore's SQLite transaction. This is a ledger and state
machine, not a second worker scheduler or a source of file/tool permissions.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from .execution_policy import normalized_policy, effective_policy, ExecutionPolicyError, calls_available, stage_available, public_deadline, deadline_reached
from .room_contract import RoomError, checked_text
from .stop_policy import continues_automatically, OWN_RECOVERY


ACTIVE = frozenset({"running", "checkpoint", "awaiting_continue", "needs_user", "paused"})


def objective_scope_reference(objective):
    digest = hashlib.sha256(objective["original_goal"].encode("utf-8")).hexdigest()
    return "beanest-objective:" + objective["id"] + ":sha256:" + digest


class ObjectiveStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def initialize(db):
        db.execute("""CREATE TABLE IF NOT EXISTS objectives (
            id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id),
            original_goal TEXT NOT NULL, policy_json TEXT NOT NULL,
            created_at REAL NOT NULL, deadline REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, stop_reason TEXT, checkpoint_json TEXT,
            work_mode TEXT NOT NULL, runtime_settings_json TEXT,
            retired_json TEXT NOT NULL DEFAULT '{}', pause_requested INTEGER NOT NULL DEFAULT 0
        )""")
        if "objective_id" not in {row[1] for row in db.execute("PRAGMA table_info(team_runs)")}:
            db.execute("ALTER TABLE team_runs ADD COLUMN objective_id TEXT REFERENCES objectives(id)")
        if "baseline_json" not in {row[1] for row in db.execute("PRAGMA table_info(objectives)")}:
            db.execute("ALTER TABLE objectives ADD COLUMN baseline_json TEXT")
        if "user_decision_json" not in {row[1] for row in db.execute("PRAGMA table_info(objectives)")}:
            db.execute("ALTER TABLE objectives ADD COLUMN user_decision_json TEXT")
        if "continuation_json" not in {row[1] for row in db.execute("PRAGMA table_info(objectives)")}:
            db.execute("ALTER TABLE objectives ADD COLUMN continuation_json TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS objective_progress (
            objective_id TEXT NOT NULL REFERENCES objectives(id),
            scope TEXT NOT NULL, signature TEXT NOT NULL, repeats INTEGER NOT NULL,
            PRIMARY KEY(objective_id,scope,signature)
        )""")

    @staticmethod
    def current(db, room_id):
        row = db.execute("SELECT o.* FROM objectives o JOIN team_runs r ON r.objective_id=o.id "
                         "JOIN teams t ON t.current_run_id=r.id WHERE t.room_id=?", (room_id,)).fetchone()
        if row is None:
            return None
        # Continue under the current policy without rewriting historical caps,
        # counters, failures or the user's original goal in the stored ledger.
        policy = effective_policy(json.loads(row["policy_json"]))
        return {**dict(row), "deadline": row["deadline"] if policy["max_seconds"] is not None else 0,
                "policy_json": json.dumps(policy)}

    def create(self, db, room_id, goal, policy):
        try:
            policy = normalized_policy(policy)
        except ExecutionPolicyError:
            raise RoomError("invalid_execution_policy") from None
        if policy is None:
            return None
        room = self.store._room(db, room_id)
        now, identifier = self.store.clock(), str(uuid4())
        db.execute("INSERT INTO objectives(id,room_id,original_goal,policy_json,created_at,deadline,status,"
                   "work_mode,runtime_settings_json) VALUES (?,?,?,?,?,?,'running',?,?)",
                   (identifier, room_id, goal, json.dumps(policy), now, now + policy["max_seconds"] if policy["max_seconds"] is not None else 0,
                    room["work_mode"], room["runtime_settings_json"]))
        db.execute("UPDATE team_runs SET objective_id=? WHERE id=(SELECT current_run_id FROM teams WHERE room_id=?)",
                   (identifier, room_id))
        self.apply_stage_limits(db, room_id)
        self.store._event(db, room_id, "objective_started", sender="user", objective_id=identifier)
        return identifier

    def budget(self, db, objective):
        policy = json.loads(objective["policy_json"])
        queued = db.execute("SELECT count(*) FROM jobs j JOIN team_jobs t ON t.job_id=j.id "
                            "JOIN team_runs r ON r.id=t.run_id WHERE r.objective_id=? AND j.status='queued'",
                            (objective["id"],)).fetchone()[0]
        limit = policy["max_calls"]
        return {"used": objective["used"], "limit": limit, "queued": queued,
                "remaining": max(0, limit - objective["used"] - queued) if limit is not None else None}

    def apply_stage_limits(self, db, room_id):
        objective = self.current(db, room_id)
        if objective is None:
            return
        db.execute("UPDATE rooms SET deadline=0,call_limit=0 WHERE id=?", (room_id,))
        db.execute("UPDATE team_runs SET deadline=0,call_limit=0 WHERE id=(SELECT current_run_id FROM teams WHERE room_id=?)",
                   (room_id,))

    def can_dispatch(self, db, room_id):
        objective = self.current(db, room_id)
        if objective is None:
            return True
        if objective["status"] not in {"running", "checkpoint"}:
            return False
        if deadline_reached(objective["deadline"], self.store.clock()):
            self.store._team.stop(db, room_id, "objective_deadline_exceeded")
            return False
        limit = json.loads(objective["policy_json"])["max_calls"]
        if limit is not None and objective["used"] >= limit:
            # Started calls have already spent their lease. Reaching the cap
            # blocks another claim, but must not invalidate those results while
            # another worker's completion wakes the scheduler.
            inflight = db.execute("SELECT 1 FROM jobs WHERE room_id=? AND status='working' UNION "
                                  "SELECT 1 FROM executions WHERE room_id=? AND status='working'",
                                  (room_id, room_id)).fetchone()
            if not inflight:
                self.store._team.stop(db, room_id, "objective_calls_exhausted")
            return False
        return True

    def record_claim(self, db, room_id):
        objective = self.current(db, room_id)
        if objective is not None and objective["status"] not in {"complete", "stopped", "interrupted"}:
            db.execute("UPDATE objectives SET used=used+1 WHERE id=?", (objective["id"],))

    def repeated_without_progress(self, db, room_id, scope, evidence, *, include_tasks=True, stop_on_repeat=True):
        """Bound repeated identical evidence, never a count of useful calls.

        New read ranges, hashes, completed results, ownership epochs or plan
        decisions have different signatures. Chat wording, timestamps and
        working/waiting transitions cannot reset this persisted protection.
        """
        objective = self.current(db, room_id)
        if objective is None or json.loads(objective["policy_json"])["mode"] != "autonomous":
            return False
        tasks = [{key: task[key] for key in ("id", "owner", "assignment_epoch", "result_digest", "result_version")}
                 for task in db.execute("SELECT * FROM shared_tasks WHERE room_id=? AND run_id=(SELECT current_run_id FROM teams WHERE room_id=?) ORDER BY id",
                                        (room_id, room_id))] if include_tasks else []
        signature = hashlib.sha256(json.dumps([tasks, evidence], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        seen = db.execute("SELECT 1 FROM objective_progress WHERE objective_id=? AND scope=? AND signature=?",
                          (objective["id"], scope, signature)).fetchone()
        if seen is None:
            # A genuinely new piece of evidence starts a new progress episode.
            # Previously seen ranges still stay recorded, so A/B/A/B cannot
            # masquerade as a fresh observation on every turn.
            db.execute("UPDATE objective_progress SET repeats=0 WHERE objective_id=? AND scope=?",
                       (objective["id"], scope))
        db.execute("INSERT INTO objective_progress VALUES (?,?,?,1) ON CONFLICT(objective_id,scope,signature) "
                   "DO UPDATE SET repeats=repeats+1", (objective["id"], scope, signature))
        repeats = db.execute("SELECT repeats FROM objective_progress WHERE objective_id=? AND scope=? AND signature=?",
                             (objective["id"], scope, signature)).fetchone()[0]
        if repeats == 2:
            self.store._event(db, room_id, "objective_progress_warning", sender="system",
                              objective_id=objective["id"], code="objective_repeated_evidence")
        if repeats >= 3:
            if stop_on_repeat:
                self.store._team.stop(db, room_id, "objective_no_progress")
            return True
        return False

    @staticmethod
    def inspection_evidence(observation):
        request, result = observation.get("request", {}), observation.get("result", {})
        empty = (request.get("kind") == "read" and not result.get("content")
                 or request.get("kind") == "search" and not result.get("matches")
                 or request.get("kind") == "list" and not result.get("entries"))
        if result.get("error") or empty:
            return {"kind": request.get("kind"), "path": request.get("path"), "error": result.get("error"),
                    "revision": result.get("revision"), "sha256": result.get("sha256"), "empty": empty}
        return observation

    def stop(self, db, room_id, reason, *, status="stopped"):
        objective = self.current(db, room_id)
        if (objective is not None and objective["status"] == "paused"
                and (continues_automatically(reason) or reason in OWN_RECOVERY)):
            # The user's pause holds through a stop the host would resume by
            # itself; resuming returns to that wait instead of skipping it.
            checkpoint = json.loads(objective["checkpoint_json"] or "{}")
            if checkpoint.get("resume_status") not in {"stopped", "interrupted"}:
                checkpoint["resume_status"] = status
                db.execute("UPDATE objectives SET checkpoint_json=?,stop_reason=? WHERE id=?",
                           (json.dumps(checkpoint), reason, objective["id"]))
            return
        if objective is not None and objective["status"] not in {"complete", "stopped", "interrupted"}:
            db.execute("UPDATE objectives SET status=?,stop_reason=?,pause_requested=0 WHERE id=?",
                       (status, reason, objective["id"]))
            self.store._event(db, room_id, "objective_stopped", sender="system", objective_id=objective["id"], code=reason)

    def begin_checkpoint(self, db, room_id, binding, provider):
        objective = self.current(db, room_id)
        if objective is None or objective["status"] != "running":
            return False
        checkpoint = json.loads(objective["checkpoint_json"]) if objective["checkpoint_json"] else None
        if checkpoint and checkpoint.get("binding", {}).get("run_id") == binding["run_id"]:
            return False
        value = {"binding": binding, "provider": provider, "proposal": None, "proposal_digest": None,
                 "reviewer": None, "review_kind": None, "reviewed": False}
        db.execute("UPDATE objectives SET status='checkpoint',checkpoint_json=? WHERE id=?",
                   (json.dumps(value), objective["id"]))
        db.execute("UPDATE teams SET phase='checkpoint' WHERE room_id=?", (room_id,))
        return True

    def propose(self, db, room_id, provider, command):
        objective = self.current(db, room_id)
        checkpoint = json.loads(objective["checkpoint_json"]) if objective and objective["checkpoint_json"] else None
        if (objective is None or checkpoint is None or checkpoint["proposal"] is not None
                or checkpoint["provider"] != provider or objective["status"] not in {"checkpoint", "paused"}):
            raise RoomError("objective_checkpoint_conflict")
        decision = command["decision"]
        if not isinstance(decision, str) or decision not in {"continue", "complete", "needs_user"}:
            raise RoomError("invalid_objective_checkpoint")
        proposal = {key: command[key] for key in ("decision", "summary", "next_goal", "scope_reference", "question")}
        for key, limit in (("summary", 2000), ("next_goal", 3000), ("scope_reference", 3000), ("question", 2000)):
            value = proposal[key]
            if value is not None:
                checked_text(value, limit)
        if (not isinstance(proposal["summary"], str)
                or decision == "continue" and not isinstance(proposal["next_goal"], str)
                or decision == "needs_user" and not isinstance(proposal["question"], str)):
            raise RoomError("invalid_objective_checkpoint")
        reference, original = proposal["scope_reference"], objective["original_goal"]
        same_scope = (reference == objective_scope_reference(objective) if isinstance(reference, str)
                      and reference.startswith("beanest-objective:") else
                      isinstance(reference, str) and len(reference.strip()) >= min(8, len(original.strip()))
                      and reference in original)
        proposal["scope"] = ("new_goal" if decision == "complete" and proposal["next_goal"] else
                             "current_goal" if same_scope else "new_goal" if proposal["next_goal"] else None)
        checkpoint["proposal"] = proposal
        checkpoint["proposal_digest"] = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
        db.execute("UPDATE objectives SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), objective["id"]))

    def finish_checkpoint(self, db, room_id, *, reviewer=None, verdict="accept", reason=""):
        objective = self.current(db, room_id)
        checkpoint = json.loads(objective["checkpoint_json"])
        proposal = checkpoint["proposal"]
        if checkpoint["reviewed"] or proposal is None:
            raise RoomError("objective_checkpoint_conflict")
        checkpoint.update(reviewer=reviewer, reviewed=True,
                          review_kind="independent_review" if reviewer and reviewer != checkpoint["provider"] else "self_check")
        decision = proposal["decision"]
        # A rejected assessment is data to revise, not a user decision. Even
        # an incorrectly classified scope must return to its author first.
        if verdict == "changes_requested" or decision in {"continue", "complete"} and proposal["scope"] == "current_goal":
            results = sorted(json.dumps(sorted((file["path"], file["sha256"]) for file in
                                              json.loads(task["result_files_json"] or "[]")))
                             if task["result_files_json"] else hashlib.sha256((task["result"] or "").encode()).hexdigest()
                             for task in db.execute("SELECT result_files_json,result FROM shared_tasks WHERE room_id=? "
                                                    "AND run_id=(SELECT current_run_id FROM teams WHERE room_id=?) "
                                                    "AND status IN ('accepted','self_checked')", (room_id, room_id)))
            if verdict == "changes_requested":
                feedback = {"proposal": proposal, "proposal_digest": checkpoint["proposal_digest"],
                            "reviewer": reviewer, "reason": reason, "binding": checkpoint["binding"],
                            "input_user_seq": checkpoint.get("input_user_seq")}
                self.store._event(db, room_id, "objective_checkpoint_revision_requested", sender="system",
                                  objective_id=objective["id"], provider=checkpoint["provider"], feedback=feedback)
                # The structured assessment is itself an artifact. Rewritten
                # review feedback alone is not a new proposal or new evidence.
                scope = json.dumps(["checkpoint_revision", checkpoint.get("input_user_seq")])
                paused = objective["status"] == "paused"
                if self.repeated_without_progress(db, room_id, scope, [checkpoint["proposal_digest"], results],
                        include_tasks=False, stop_on_repeat=not paused):
                    if paused:
                        checkpoint.update(resume_status="stopped", resume_stop_reason="objective_no_progress",
                                          revision_feedback=feedback)
                    db.execute("UPDATE objectives SET checkpoint_json=?,stop_reason='objective_no_progress' WHERE id=?",
                               (json.dumps(checkpoint), objective["id"]))
                    return
                checkpoint.update(proposal=None, proposal_digest=None, reviewer=None, review_kind=None,
                                  reviewed=False, revision_feedback=feedback)
                if paused:
                    checkpoint["resume_status"] = "checkpoint"
                db.execute("UPDATE objectives SET status=?,stop_reason=NULL,checkpoint_json=? WHERE id=?",
                           ("paused" if paused else "checkpoint", json.dumps(checkpoint), objective["id"]))
                db.execute("UPDATE teams SET phase='checkpoint' WHERE room_id=?", (room_id,))
                return
            if decision == "continue" and self.repeated_without_progress(db, room_id, "checkpoint_continuation",
                    [proposal["next_goal"], proposal["scope_reference"], results], include_tasks=False):
                return
        if verdict != "accept":
            status, stop = "needs_user", "objective_checkpoint_disagreed"
            proposal["question"] = reason
        elif decision == "continue" and proposal["scope"] != "current_goal":
            status, stop = "needs_user", "objective_scope_confirmation"
        elif decision == "continue":
            status, stop = "awaiting_continue", None
        elif decision == "needs_user":
            status, stop = "needs_user", "objective_decision_required"
        else:
            status, stop = "complete", None
        checkpoint.pop("revision_feedback", None)
        # Pause never discards a returned assessment or starts its next stage.
        if objective["status"] == "paused":
            checkpoint["resume_status"] = status
            status = "paused"
        db.execute("UPDATE objectives SET status=?,stop_reason=?,checkpoint_json=? WHERE id=?",
                   (status, stop, json.dumps(checkpoint), objective["id"]))
        db.execute("UPDATE teams SET phase='ready_for_user' WHERE room_id=?", (room_id,))
        question = ({"question": proposal["question"], "question_binding": checkpoint["binding"],
                     "proposal_digest": checkpoint["proposal_digest"]}
                    if decision == "needs_user" and verdict == "accept" else {})
        self.store._event(db, room_id, "objective_checkpoint_finished", sender="system",
                          objective_id=objective["id"], decision=decision, review_kind=checkpoint["review_kind"], code=stop, **question)

    def require_task_decision(self, db, room_id, binding, tasks, *, native_question=None):
        """Surface an existing task block without inventing another model turn.

        Tasks retain their blocked state; this host checkpoint is a question,
        never a review or acceptance of the unfinished stage.
        """
        objective = self.current(db, room_id)
        if objective is None or objective["status"] != "running" or not tasks and native_question is None:
            return False
        question = native_question if native_question is not None else "；".join(task["title"] + "：" + (task["blocked_reason"] or "需要补充决定") for task in tasks)
        question = question[:2000]
        proposal = {"decision": "needs_user", "summary": "有任务明确提出阻塞，未继续派发。",
                    "next_goal": None, "scope_reference": objective["original_goal"], "scope": "current_goal",
                    "question": question}
        checkpoint = {"binding": binding, "provider": None, "proposal": proposal,
                      "proposal_digest": hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest(),
                      "reviewer": None, "review_kind": None, "reviewed": True, "origin": "task_block"}
        db.execute("UPDATE objectives SET status='needs_user',stop_reason='objective_decision_required',checkpoint_json=? WHERE id=?",
                   (json.dumps(checkpoint), objective["id"]))
        self.store._event(db, room_id, "objective_decision_required", sender="system",
                          objective_id=objective["id"], task_ids=[task["id"] for task in tasks])
        return True

    def snapshot(self, db, room_id):
        row = self.current(db, room_id)
        if row is None:
            return None
        policy, budget = json.loads(row["policy_json"]), self.budget(db, row)
        checkpoint = json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None
        step = None
        stage = db.execute("SELECT count(*) FROM team_runs WHERE objective_id=?", (row["id"],)).fetchone()[0]
        if checkpoint and checkpoint.get("proposal"):
            proposal = checkpoint["proposal"]
            step = {"origin": checkpoint.get("origin"), "decision": proposal["decision"], "summary": proposal["summary"],
                    "goal": proposal["next_goal"], "scope_reference": proposal["scope_reference"],
                    "scope": proposal["scope"], "question": proposal["question"],
                    "binding": checkpoint["binding"], "review_kind": checkpoint["review_kind"],
                    "can_start": (row["status"] == "awaiting_continue" and checkpoint["reviewed"]
                                  and proposal["scope"] == "current_goal" and calls_available(budget["remaining"])
                                  and not deadline_reached(row["deadline"], self.store.clock()) and stage_available(policy, stage))}
        return {"id": row["id"], "mode": policy["mode"], "execution_policy": policy,
                "original_goal": row["original_goal"], "scope_reference": objective_scope_reference(row),
                "created_at": row["created_at"], "deadline": public_deadline(row["deadline"]),
                "status": row["status"], "stop_reason": row["stop_reason"], "budget": budget, "stage": stage,
                "pause_requested": bool(row["pause_requested"]), "next_step": step,
                "baseline": json.loads(row["baseline_json"]) if row["baseline_json"] else None,
                "user_decision": json.loads(row["user_decision_json"]) if row["user_decision_json"] else None,
                "continuation": json.loads(row["continuation_json"]) if row["continuation_json"] else None,
                "can_answer": (row["status"] == "needs_user" and row["stop_reason"] in
                               {"objective_decision_required", "objective_checkpoint_disagreed"}
                               and calls_available(budget["remaining"]) and stage_available(policy, stage) and not deadline_reached(row["deadline"], self.store.clock())),
                "checkpoint_proposal": ({**checkpoint["proposal"], "proposal_digest": checkpoint["proposal_digest"]}
                                        if checkpoint and checkpoint.get("proposal") else None),
                "checkpoint_revision": (checkpoint.get("revision_feedback")
                                        if checkpoint and row["status"] in {"checkpoint", "paused"} else None),
                "retired_partners": json.loads(row["retired_json"])}

    def available_members(self, db, room_id):
        row = self.current(db, room_id)
        retired = json.loads(row["retired_json"]) if row else {}
        return [member for member in self.store.member_ids_in_transaction(db, room_id)
                if member not in retired and self.store._partner_dispatch_reason(db, member) is None
                and not db.execute("SELECT identity_uncertain FROM members WHERE room_id=? AND provider=?", (room_id, member)).fetchone()[0]]

    def set_pause(self, db, room_id, objective_id, paused, *, defer_resume=False, waiting=False):
        """waiting: the objective is stopped on something the host resumes by
        itself (a quota wait, an automatic continue); pausing holds that too."""
        row = self.current(db, room_id)
        if row is None or row["id"] != objective_id:
            raise RoomError("objective_conflict")
        checkpoint = json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else {}
        if paused:
            pausable = {"running", "checkpoint", "awaiting_continue"} | ({"stopped", "interrupted", "needs_user"} if waiting else set())
            if row["status"] not in pausable:
                raise RoomError("objective_not_running")
            checkpoint["resume_status"] = row["status"]
            db.execute("UPDATE objectives SET status='paused',checkpoint_json=? WHERE id=?",
                       (json.dumps(checkpoint), objective_id))
        else:
            if row["status"] != "paused" or row["pause_requested"] and not defer_resume:
                raise RoomError("objective_not_paused")
            if row["pause_requested"]:
                checkpoint["resume_requested"] = True
                db.execute("UPDATE objectives SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), objective_id))
                self.store._event(db, room_id, "objective_resume_pending", sender="user", objective_id=objective_id)
                return
            self._resume_after_pause(db, room_id, objective_id, checkpoint)
        self.settle_pause(db, room_id)
        self.store._event(db, room_id, "objective_paused" if paused else "objective_resumed",
                          sender="user", objective_id=objective_id)

    def _resume_after_pause(self, db, room_id, objective_id, checkpoint):
        state = checkpoint.pop("resume_status", "running")
        deferred_stop = checkpoint.pop("resume_stop_reason", None)
        if state == "stopped" and deferred_stop:
            db.execute("UPDATE objectives SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), objective_id))
            self.store._team.stop(db, room_id, deferred_stop)
        else:
            db.execute("UPDATE objectives SET status=?,checkpoint_json=? WHERE id=?",
                       (state, json.dumps(checkpoint) if checkpoint else None, objective_id))

    def resume_status(self, db, room_id):
        """The state a paused objective returns to."""
        row = self.current(db, room_id)
        if row is None or row["status"] != "paused":
            return None
        return json.loads(row["checkpoint_json"] or "{}").get("resume_status", "running")

    def settle_pause(self, db, room_id):
        row = self.current(db, room_id)
        if row is None or row["status"] != "paused":
            return
        inflight = db.execute("SELECT 1 FROM jobs WHERE room_id=? AND status='working' UNION "
                              "SELECT 1 FROM executions WHERE room_id=? AND status='working'",
                              (room_id, room_id)).fetchone() is not None
        # A returned call must have durable process cleanup before resume can
        # claim another lease. A completed job alone is not that proof.
        if not inflight:
            for job in db.execute("SELECT j.id FROM jobs j JOIN team_jobs t ON t.job_id=j.id "
                                  "JOIN team_runs r ON r.id=t.run_id WHERE r.objective_id=? "
                                  "AND j.status!='queued' AND EXISTS(SELECT 1 FROM events e WHERE e.room_id=j.room_id "
                                  "AND json_extract(e.value_json,'$.kind')='job_claimed' "
                                  "AND json_extract(e.value_json,'$.job_id')=j.id)", (row["id"],)):
                event = db.execute("SELECT value_json FROM events WHERE room_id=? "
                                   "AND json_extract(value_json,'$.kind')='job_cleanup' "
                                   "AND json_extract(value_json,'$.job_id')=? ORDER BY seq DESC LIMIT 1",
                                   (room_id, job["id"])).fetchone()
                if event is None or json.loads(event[0]).get("cleanup_confirmed") is not True:
                    inflight = True
                    break
        db.execute("UPDATE objectives SET pause_requested=? WHERE id=?", (int(inflight), row["id"]))
        checkpoint = json.loads(row["checkpoint_json"] or "{}")
        if not inflight and checkpoint.pop("resume_requested", False):
            self._resume_after_pause(db, room_id, row["id"], checkpoint)
            self.store._event(db, room_id, "objective_resumed", sender="user", objective_id=row["id"], after_cleanup=True)
