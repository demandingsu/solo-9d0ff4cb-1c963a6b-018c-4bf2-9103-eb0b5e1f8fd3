"""SQLite 持久化层。

补报有“有效”与“已撤回”两种状态。替换操作撤回旧补报时只改状态、不删数据，
因此历史补报及其撤回状态始终可查；而计数校验、总片数更正和上下界查询只读取
有效补报。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ACTIVE = "active"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
    revoked_at     TEXT,
    revoked_by_replacement_id INTEGER
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    lo             INTEGER NOT NULL CHECK (lo >= 0),
    hi             INTEGER NOT NULL CHECK (hi > lo),
    count          INTEGER NOT NULL CHECK (count >= 0)
);

CREATE TABLE IF NOT EXISTS replacements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id),
    created_at     TEXT NOT NULL,
    total_pieces   INTEGER NOT NULL,
    basis_report_ids TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replacement_revocations (
    replacement_id INTEGER NOT NULL REFERENCES replacements(id) ON DELETE CASCADE,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id),
    PRIMARY KEY (replacement_id, report_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_reports_inspection
    ON count_reports(inspection_id);
CREATE INDEX IF NOT EXISTS idx_reports_inspection_status
    ON count_reports(inspection_id, status);
CREATE INDEX IF NOT EXISTS idx_segments_inspection
    ON segments(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_report
    ON segments(report_id);
CREATE INDEX IF NOT EXISTS idx_replacements_inspection
    ON replacements(inspection_id);
"""

# 旧库升级：``CREATE TABLE IF NOT EXISTS`` 不会补列，需显式 ALTER。
_REPORT_MIGRATIONS = (
    ("status", "status TEXT NOT NULL DEFAULT 'active'"),
    ("revoked_at", "revoked_at TEXT"),
    ("revoked_by_replacement_id", "revoked_by_replacement_id INTEGER"),
)

_REPLACEMENT_MIGRATIONS = (
    ("total_pieces", "total_pieces INTEGER NOT NULL DEFAULT 0"),
    ("basis_report_ids", "basis_report_ids TEXT NOT NULL DEFAULT '[]'"),
)


def connect(path: str) -> sqlite3.Connection:
    """打开一个请求级连接。

    ``isolation_level=None`` 让我们显式使用 ``BEGIN IMMEDIATE`` 控制写事务，
    从而把“读取现有计数 + 校验 + 写入”放在同一个串行化事务里。

    ``check_same_thread=False``：每条连接只服务一个请求，但依赖清理与
    端点可能落在不同的线程池线程上；连接不会跨请求共享。
    """
    connection = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        _migrate(connection)
        connection.executescript(INDEXES)
    finally:
        connection.close()


def _migrate(connection: sqlite3.Connection) -> None:
    for table, migrations in (
        ("count_reports", _REPORT_MIGRATIONS),
        ("replacements", _REPLACEMENT_MIGRATIONS),
    ):
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, ddl in migrations:
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_inspection(
    connection: sqlite3.Connection, total_pieces: int, note: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO inspections (total_pieces, note, created_at) VALUES (?, ?, ?)",
        (total_pieces, note, now()),
    )
    return int(cursor.lastrowid)


def get_inspection(connection: sqlite3.Connection, inspection_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM count_reports r
                WHERE r.inspection_id = i.id AND r.status = 'active')
                   AS active_report_count,
               (SELECT COUNT(*) FROM count_reports r
                WHERE r.inspection_id = i.id AND r.status = 'revoked')
                   AS revoked_report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count,
               (SELECT COUNT(*) FROM segments s
                JOIN count_reports r ON r.id = s.report_id
                WHERE s.inspection_id = i.id AND r.status = 'active')
                   AS active_segment_count
        FROM inspections i
        WHERE i.id = ?
        """,
        (inspection_id,),
    ).fetchone()


def list_inspections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM count_reports r
                WHERE r.inspection_id = i.id AND r.status = 'active')
                   AS active_report_count,
               (SELECT COUNT(*) FROM count_reports r
                WHERE r.inspection_id = i.id AND r.status = 'revoked')
                   AS revoked_report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count,
               (SELECT COUNT(*) FROM segments s
                JOIN count_reports r ON r.id = s.report_id
                WHERE s.inspection_id = i.id AND r.status = 'active')
                   AS active_segment_count
        FROM inspections i
        ORDER BY i.id
        """
    ).fetchall()


def update_inspection(
    connection: sqlite3.Connection,
    inspection_id: int,
    total_pieces: int,
    note: str | None,
) -> None:
    connection.execute(
        "UPDATE inspections SET total_pieces = ?, note = ? WHERE id = ?",
        (total_pieces, note, inspection_id),
    )


def load_constraints(
    connection: sqlite3.Connection, inspection_id: int
) -> list[tuple[int, int, int]]:
    """全部**有效**补报的区间约束，不含已撤回的旧补报。"""
    rows = connection.execute(
        """
        SELECT s.lo, s.hi, s.count
        FROM segments s
        JOIN count_reports r ON r.id = s.report_id
        WHERE s.inspection_id = ? AND r.status = 'active'
        ORDER BY s.id
        """,
        (inspection_id,),
    ).fetchall()
    return [(int(row["lo"]), int(row["hi"]), int(row["count"])) for row in rows]


def load_active_reports(
    connection: sqlite3.Connection, inspection_id: int
) -> list[dict]:
    """有效补报（含各自区间），按报告号升序。"""
    return _load_reports(connection, inspection_id, status=ACTIVE)


def _load_reports(
    connection: sqlite3.Connection, inspection_id: int, status: str | None
) -> list[dict]:
    where = "r.inspection_id = ?"
    params: list[object] = [inspection_id]
    if status is not None:
        where += " AND r.status = ?"
        params.append(status)

    reports = connection.execute(
        f"SELECT r.id, r.inspection_id, r.created_at, r.status, r.revoked_at, "
        f"r.revoked_by_replacement_id FROM count_reports r WHERE {where} ORDER BY r.id",
        params,
    ).fetchall()

    segment_rows = connection.execute(
        f"""
        SELECT s.id, s.report_id, s.lo, s.hi, s.count
        FROM segments s
        JOIN count_reports r ON r.id = s.report_id
        WHERE {where}
        ORDER BY s.id
        """,
        params,
    ).fetchall()

    segments_by_report: dict[int, list[dict]] = {int(r["id"]): [] for r in reports}
    for row in segment_rows:
        segments_by_report.setdefault(int(row["report_id"]), []).append(
            {
                "id": int(row["id"]),
                "lo": int(row["lo"]),
                "hi": int(row["hi"]),
                "count": int(row["count"]),
            }
        )

    return [
        {
            "id": int(report["id"]),
            "inspection_id": int(report["inspection_id"]),
            "created_at": report["created_at"],
            "status": report["status"],
            "revoked_at": report["revoked_at"],
            "revoked_by_replacement_id": report["revoked_by_replacement_id"],
            "segments": segments_by_report[int(report["id"])],
        }
        for report in reports
    ]


def insert_report(
    connection: sqlite3.Connection,
    inspection_id: int,
    segments: Sequence[tuple[int, int, int]],
    created_at: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO count_reports (inspection_id, created_at, status) "
        "VALUES (?, ?, 'active')",
        (inspection_id, created_at or now()),
    )
    report_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO segments (report_id, inspection_id, lo, hi, count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, inspection_id, lo, hi, count) for lo, hi, count in segments],
    )
    return report_id


def get_reports(
    connection: sqlite3.Connection,
    inspection_id: int,
    include_revoked: bool = True,
) -> list[dict]:
    status = None if include_revoked else ACTIVE
    return _load_reports(connection, inspection_id, status=status)


def get_report(
    connection: sqlite3.Connection, inspection_id: int, report_id: int
) -> dict | None:
    for report in _load_reports(connection, inspection_id, status=None):
        if report["id"] == report_id:
            return report
    return None


def revoke_reports(
    connection: sqlite3.Connection,
    report_ids: Sequence[int],
    replacement_id: int,
    revoked_at: str,
) -> None:
    """把给出的有效补报标记为已撤回；任何一份不是有效状态即视为竞态。"""
    for report_id in report_ids:
        cursor = connection.execute(
            """
            UPDATE count_reports
            SET status = 'revoked', revoked_at = ?, revoked_by_replacement_id = ?
            WHERE id = ? AND status = 'active'
            """,
            (revoked_at, replacement_id, report_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"补报 {report_id} 已不是有效状态，替换需回滚")


def insert_replacement(
    connection: sqlite3.Connection,
    inspection_id: int,
    report_id: int,
    revoked_report_ids: Sequence[int],
    created_at: str,
    total_pieces: int,
    basis_report_ids: Sequence[int],
) -> int:
    cursor = connection.execute(
        "INSERT INTO replacements "
        "(inspection_id, report_id, created_at, total_pieces, basis_report_ids) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            inspection_id,
            report_id,
            created_at,
            total_pieces,
            json.dumps(list(basis_report_ids)),
        ),
    )
    replacement_id = int(cursor.lastrowid)
    connection.executemany(
        "INSERT INTO replacement_revocations (replacement_id, report_id) "
        "VALUES (?, ?)",
        [(replacement_id, report_id) for report_id in revoked_report_ids],
    )
    return replacement_id


def get_replacements(
    connection: sqlite3.Connection, inspection_id: int
) -> list[dict]:
    rows = connection.execute(
        "SELECT id, inspection_id, report_id, created_at, total_pieces, "
        "basis_report_ids FROM replacements WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    links = connection.execute(
        """
        SELECT replacement_id, report_id
        FROM replacement_revocations
        WHERE replacement_id IN (
            SELECT id FROM replacements WHERE inspection_id = ?
        )
        ORDER BY report_id
        """,
        (inspection_id,),
    ).fetchall()

    revoked_by_replacement: dict[int, list[int]] = {int(r["id"]): [] for r in rows}
    for link in links:
        revoked_by_replacement[int(link["replacement_id"])].append(
            int(link["report_id"])
        )

    reports = {r["id"]: r for r in _load_reports(connection, inspection_id, None)}
    result = []
    for row in rows:
        report = reports.get(int(row["report_id"]))
        if report is None:
            raise RuntimeError(f"替换 {row['id']} 指向的补报不存在")
        result.append(
            {
                "id": int(row["id"]),
                "inspection_id": int(row["inspection_id"]),
                "created_at": row["created_at"],
                "report": report,
                "revoked_report_ids": revoked_by_replacement[int(row["id"])],
                "total_pieces": int(row["total_pieces"]),
                "basis_report_ids": [
                    int(value) for value in json.loads(row["basis_report_ids"])
                ],
            }
        )
    return result
