use anyhow::Result;
use rusqlite::Connection;
use std::path::Path;

pub fn init_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let conn = Connection::open(path)?;
    init_schema(&conn)?;
    Ok(conn)
}

pub fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            file TEXT, chunk TEXT, language TEXT, chunk_index INTEGER,
            file_mtime REAL,
            start_line INTEGER,
            end_line INTEGER,
            start_byte INTEGER,
            end_byte INTEGER
        );
        CREATE TABLE IF NOT EXISTS vectors (id INTEGER, embedding BLOB);
        CREATE INDEX IF NOT EXISTS idx_file ON chunks(file);
        CREATE INDEX IF NOT EXISTS idx_file_mtime ON chunks(file, file_mtime);

        CREATE TABLE IF NOT EXISTS files (
            file TEXT PRIMARY KEY,
            chunk_count INTEGER,
            embedding BLOB
        );

        CREATE TABLE IF NOT EXISTS symbols (
            file       TEXT NOT NULL,
            name       TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            kind       TEXT NOT NULL,
            start_line INTEGER,
            end_line   INTEGER,
            file_mtime REAL
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name_lower ON symbols(name_lower);
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);

        CREATE TABLE IF NOT EXISTS file_graph (
            file       TEXT PRIMARY KEY,
            in_degree  INTEGER,
            out_degree INTEGER,
            pagerank   REAL,
            file_mtime REAL
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS graph_node (
            id     INTEGER PRIMARY KEY,
            kind   TEXT NOT NULL,
            key    TEXT NOT NULL,
            UNIQUE(kind, key)
        );

        CREATE TABLE IF NOT EXISTS graph_edge (
            src_id  INTEGER NOT NULL,
            dst_id  INTEGER NOT NULL,
            type    TEXT NOT NULL,
            weight  REAL NOT NULL,
            PRIMARY KEY (src_id, dst_id, type)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_graph_edge_src_type
          ON graph_edge(src_id, type, weight DESC);
        CREATE INDEX IF NOT EXISTS idx_graph_node_kind_key
          ON graph_node(kind, key);
        "#,
    )?;

    ensure_column(conn, "chunks", "enriched_at", "REAL")?;
    ensure_column(conn, "chunks", "description", "TEXT")?;
    Ok(())
}

fn ensure_column(conn: &Connection, table: &str, column: &str, kind: &str) -> Result<()> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let existing: String = row.get(1)?;
        if existing == column {
            return Ok(());
        }
    }
    conn.execute(
        &format!("ALTER TABLE {table} ADD COLUMN {column} {kind}"),
        [],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_python_compatible_tables() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('chunks','vectors','files','symbols','file_graph','metadata')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 6);
    }
}
