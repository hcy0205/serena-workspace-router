import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from serena_workspace_router.config import sanitize_segment, sha1_text


def _normalize_list(items: list[str] | tuple[str, ...] | None) -> list[str]:
    values = []
    seen: set[str] = set()
    for item in items or []:
        normalized = str(item).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return values


def _format_time(timestamp: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(timestamp / 1000, tz=UTC).isoformat()


def _overlap(left: list[str], right: list[str]) -> list[str]:
    right_set = set(right)
    return [item for item in left if item in right_set]


def _to_posix(path: Path) -> str:
    return str(path).replace("\\", "/")


@dataclass(slots=True)
class ProjectState:
    project_key: str
    project_root: Path
    db: sqlite3.Connection


class ProjectSharedCoordinator:
    def __init__(self, coordination_base_dir: str | Path) -> None:
        self.coordination_base_dir = Path(coordination_base_dir).expanduser().resolve()
        self.coordination_base_dir.mkdir(parents=True, exist_ok=True)
        self._db_cache: dict[str, sqlite3.Connection] = {}

    def _build_project_key(self, project_path: str | Path) -> str:
        project_root = Path(project_path).expanduser().resolve()
        return f"{sanitize_segment(project_root.name)}--{sha1_text(str(project_root))[:10]}"

    def _get_project_state(self, project_path: str | Path) -> ProjectState:
        project_root = Path(project_path).expanduser().resolve()
        project_key = self._build_project_key(project_root)
        db_root = self.coordination_base_dir / project_key
        db_root.mkdir(parents=True, exist_ok=True)

        if project_key not in self._db_cache:
            db = sqlite3.connect(db_root / "coordination.db")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  workspace_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  module_name TEXT,
                  files_json TEXT NOT NULL,
                  symbols_json TEXT NOT NULL,
                  note TEXT,
                  status TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  released_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS changes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  workspace_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  module_name TEXT,
                  summary TEXT NOT NULL,
                  files_json TEXT NOT NULL,
                  symbols_json TEXT NOT NULL,
                  compatibility_notes TEXT,
                  base_commit TEXT,
                  created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_active
                  ON claims (released_at, expires_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_changes_created
                  ON changes (created_at DESC);
                """
            )
            self._db_cache[project_key] = db

        return ProjectState(project_key=project_key, project_root=db_root, db=self._db_cache[project_key])

    def _expire_claims(self, state: ProjectState, now: int) -> None:
        state.db.execute(
            "UPDATE claims SET released_at = ? WHERE released_at IS NULL AND expires_at <= ?",
            (now, now),
        )
        state.db.commit()

    def read_active_claims(self, project_path: str | Path) -> list[dict[str, object]]:
        state = self._get_project_state(project_path)
        self._expire_claims(state, _now_ms())
        rows = state.db.execute(
            """
            SELECT id, workspace_id, agent_id, module_name, files_json, symbols_json, note, status, created_at, expires_at
            FROM claims
            WHERE released_at IS NULL AND expires_at > ?
            ORDER BY created_at DESC
            """,
            (_now_ms(),),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "workspaceId": row["workspace_id"],
                "agentId": row["agent_id"],
                "moduleName": row["module_name"] or "",
                "files": json.loads(row["files_json"]),
                "symbols": json.loads(row["symbols_json"]),
                "note": row["note"] or "",
                "status": row["status"],
                "createdAt": row["created_at"],
                "expiresAt": row["expires_at"],
            }
            for row in rows
        ]

    def read_recent_changes(self, project_path: str | Path, limit: int = 20) -> list[dict[str, object]]:
        state = self._get_project_state(project_path)
        rows = state.db.execute(
            """
            SELECT id, workspace_id, agent_id, module_name, summary, files_json, symbols_json, compatibility_notes, base_commit, created_at
            FROM changes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "workspaceId": row["workspace_id"],
                "agentId": row["agent_id"],
                "moduleName": row["module_name"] or "",
                "summary": row["summary"],
                "files": json.loads(row["files_json"]),
                "symbols": json.loads(row["symbols_json"]),
                "compatibilityNotes": row["compatibility_notes"] or "",
                "baseCommit": row["base_commit"] or "",
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def claim_scope(
        self,
        *,
        project_path: str | Path,
        workspace_id: str,
        agent_id: str,
        module_name: str = "",
        files: list[str] | None = None,
        symbols: list[str] | None = None,
        note: str = "",
        ttl_ms: int | None = None,
    ) -> dict[str, object]:
        state = self._get_project_state(project_path)
        now = _now_ms()
        self._expire_claims(state, now)

        normalized_files = _normalize_list(files)
        normalized_symbols = _normalize_list(symbols)
        active_claims = [
            claim
            for claim in self.read_active_claims(project_path)
            if not (claim["workspaceId"] == workspace_id and claim["agentId"] == agent_id)
        ]
        conflicts: list[dict[str, object]] = []
        for claim in active_claims:
            overlapping_files = _overlap(normalized_files, claim["files"])  # type: ignore[arg-type]
            overlapping_symbols = _overlap(normalized_symbols, claim["symbols"])  # type: ignore[arg-type]
            same_module = bool(module_name and claim["moduleName"] and module_name == claim["moduleName"])
            if not overlapping_files and not overlapping_symbols and not same_module:
                continue
            conflicts.append(
                {
                    "level": "hard_conflict" if overlapping_files or overlapping_symbols else "soft_conflict",
                    "claimId": claim["id"],
                    "workspaceId": claim["workspaceId"],
                    "agentId": claim["agentId"],
                    "moduleName": claim["moduleName"],
                    "overlappingFiles": overlapping_files,
                    "overlappingSymbols": overlapping_symbols,
                    "note": claim["note"],
                }
            )

        status = "granted"
        if any(item["level"] == "hard_conflict" for item in conflicts):
            status = "hard_conflict"
        elif conflicts:
            status = "soft_conflict"

        ttl_value = max(30_000, int(ttl_ms or 30 * 60 * 1000))
        cursor = state.db.execute(
            """
            INSERT INTO claims (
              workspace_id, agent_id, module_name, files_json, symbols_json, note, status, created_at, expires_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                workspace_id,
                agent_id,
                module_name or None,
                json.dumps(normalized_files),
                json.dumps(normalized_symbols),
                note or None,
                status,
                now,
                now + ttl_value,
            ),
        )
        state.db.commit()
        return {
            "ok": True,
            "claimId": cursor.lastrowid,
            "status": status,
            "conflicts": conflicts,
            "projectKey": state.project_key,
        }

    def release_scope(self, *, project_path: str | Path, claim_id: int, workspace_id: str, agent_id: str) -> dict[str, object]:
        state = self._get_project_state(project_path)
        cursor = state.db.execute(
            """
            UPDATE claims
            SET released_at = ?
            WHERE id = ?
              AND released_at IS NULL
              AND workspace_id = ?
              AND agent_id = ?
            """,
            (_now_ms(), int(claim_id), workspace_id, agent_id),
        )
        state.db.commit()
        return {"ok": True, "claimId": int(claim_id), "released": cursor.rowcount > 0}

    def publish_change(
        self,
        *,
        project_path: str | Path,
        workspace_id: str,
        agent_id: str,
        module_name: str = "",
        summary: str,
        files: list[str] | None = None,
        symbols: list[str] | None = None,
        compatibility_notes: str = "",
        base_commit: str = "",
    ) -> dict[str, object]:
        state = self._get_project_state(project_path)
        cursor = state.db.execute(
            """
            INSERT INTO changes (
              workspace_id, agent_id, module_name, summary, files_json, symbols_json, compatibility_notes, base_commit, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                agent_id,
                module_name or None,
                summary,
                json.dumps(_normalize_list(files)),
                json.dumps(_normalize_list(symbols)),
                compatibility_notes or None,
                base_commit or None,
                _now_ms(),
            ),
        )
        state.db.commit()
        return {"ok": True, "changeId": cursor.lastrowid, "projectKey": state.project_key}

    def render_shared_memories(self, *, project_path: str | Path, project_serena_folder: str | Path) -> dict[str, object]:
        shared_dir = Path(project_serena_folder) / "memories" / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        recent_changes = self.read_recent_changes(project_path, limit=20)
        active_claims = self.read_active_claims(project_path)

        recent_changes_md = [
            "# 项目近期变更",
            "",
            *(
                [
                    "\n".join(
                        [
                            f"- 时间：{_format_time(int(item['createdAt']))}",
                            f"  - 作者：{item['agentId']}@{item['workspaceId']}",
                            f"  - 模块：{item['moduleName'] or '未指定'}",
                            f"  - 文件：{', '.join(item['files']) if item['files'] else '无'}",
                            f"  - 符号：{', '.join(item['symbols']) if item['symbols'] else '无'}",
                            f"  - 摘要：{item['summary']}",
                            f"  - 兼容建议：{item['compatibilityNotes'] or '无'}",
                        ]
                    )
                    for item in recent_changes
                ]
                if recent_changes
                else ["- 暂无变更"]
            ),
            "",
        ]
        active_claims_md = [
            "# 当前占用与协作状态",
            "",
            *(
                [
                    "\n".join(
                        [
                            f"- 负责人：{item['agentId']}@{item['workspaceId']}",
                            f"  - 模块：{item['moduleName'] or '未指定'}",
                            f"  - 状态：{item['status']}",
                            f"  - 文件：{', '.join(item['files']) if item['files'] else '无'}",
                            f"  - 符号：{', '.join(item['symbols']) if item['symbols'] else '无'}",
                            f"  - 说明：{item['note'] or '无'}",
                            f"  - 过期：{_format_time(int(item['expiresAt']))}",
                        ]
                    )
                    for item in active_claims
                ]
                if active_claims
                else ["- 暂无活跃占用"]
            ),
            "",
        ]
        compatibility_guide_md = [
            "# 协作兼容指南",
            "",
            "- 修改前先 `claim_scope`，声明模块、文件、符号范围。",
            "- 修改完成后立即 `publish_change`，写明变更摘要与兼容建议。",
            "- 若看到 `hard_conflict`，优先读取 `shared/project_recent_changes` 与 `shared/project_active_claims`。",
            "- 不要直接用 `write_memory` 改写 `shared/*`；共享区只允许协调工具写入。",
            "",
        ]

        (shared_dir / "project_recent_changes.md").write_text("\n".join(recent_changes_md), encoding="utf-8")
        (shared_dir / "project_active_claims.md").write_text("\n".join(active_claims_md), encoding="utf-8")
        (shared_dir / "compatibility_guide.md").write_text("\n".join(compatibility_guide_md), encoding="utf-8")
        return {
            "ok": True,
            "sharedDir": _to_posix(shared_dir),
            "recentChangesCount": len(recent_changes),
            "activeClaimsCount": len(active_claims),
        }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
