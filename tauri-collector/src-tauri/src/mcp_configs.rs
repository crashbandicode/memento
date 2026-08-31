//! Write MCP server entries into AI IDE config files so the user's
//! Claude Code / Cursor / Codex / Windsurf / Antigravity find the
//! bundled `memento-mcp-sidecar` and start talking to it over stdio
//! the next time they launch.
//!
//! The MCP entry name is `memento-memory` everywhere; environment
//! variables `MEMENTO_SERVER_URL` + `MEMENTO_SERVER_TOKEN` carry the
//! Memento API base + collector token so the sidecar can answer
//! `memory_search` / `memory_recall` / `daily_summary` queries.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Serialize;
use serde_json::{json, Value};

const CODEX_MCP_STARTUP_TIMEOUT_SECS: i64 = 90;

#[derive(Debug, Clone, Serialize)]
pub struct McpWriteReport {
    /// Tool ids whose config we wrote successfully (e.g. "claude_code").
    pub configured: Vec<String>,
    /// Tool ids we tried but couldn't write (missing parent dir, perm error, etc.).
    pub skipped: Vec<String>,
    /// Path of the bundled sidecar we pointed entries at.
    pub sidecar_path: String,
}

/// Locate the MCP sidecar binary in its resource onedir. Keep accepting the
/// old externalBin location so an app update can roll forward cleanly.
pub fn locate_mcp_sidecar() -> Result<PathBuf> {
    let exe = std::env::current_exe().context("current_exe()")?;
    let dir = exe.parent().context("exe has no parent dir")?;
    locate_mcp_sidecar_in(dir)
}

fn locate_mcp_sidecar_in(dir: &Path) -> Result<PathBuf> {
    let exe_suffix = if cfg!(windows) { ".exe" } else { "" };
    let executable = format!("memento-mcp-sidecar{exe_suffix}");
    let onedir = dir
        .join("binaries")
        .join("memento-mcp-sidecar")
        .join(&executable);
    let legacy = dir.join(&executable);
    for candidate in [&onedir, &legacy] {
        if candidate.is_file() {
            return Ok(candidate.to_path_buf());
        }
    }
    anyhow::bail!(
        "MCP sidecar not found at {} or {}; the app may have been built without it",
        onedir.display(),
        legacy.display()
    )
}

/// Write or update MCP entries in every AI tool config we know about.
/// Each tool is best-effort: a missing config file is skipped (the user
/// just doesn't use that tool), but we never destroy existing entries
/// for other MCP servers.
pub fn write_all(server_url: &str, server_token: &str) -> Result<McpWriteReport> {
    let sidecar = locate_mcp_sidecar()?;
    let sidecar_str = sidecar.to_string_lossy().into_owned();

    let mut configured = Vec::new();
    let mut skipped = Vec::new();

    let home = dirs::home_dir().context("no home dir")?;
    let env = json!({
        "MEMENTO_SERVER_URL": server_url,
        "MEMENTO_SERVER_TOKEN": server_token,
    });

    // ── JSON-based tools ──────────────────────────────────────
    let json_targets: &[(&str, PathBuf)] = &[
        ("claude_code", home.join(".claude.json")),
        ("cursor", home.join(".cursor").join("mcp.json")),
        ("windsurf", windsurf_path(&home)),
        ("antigravity", antigravity_path(&home)),
    ];
    for (tool, path) in json_targets {
        match write_json_mcp(path, &sidecar_str, &env) {
            Ok(()) => configured.push((*tool).to_string()),
            Err(_) => skipped.push((*tool).to_string()),
        }
    }

    // ── Codex (TOML) ──────────────────────────────────────────
    match write_codex_mcp(
        &home.join(".codex").join("config.toml"),
        &sidecar_str,
        server_url,
        server_token,
    ) {
        Ok(()) => configured.push("codex".into()),
        Err(_) => skipped.push("codex".into()),
    }

    Ok(McpWriteReport {
        configured,
        skipped,
        sidecar_path: sidecar_str,
    })
}

fn windsurf_path(home: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    return home
        .join("Library")
        .join("Application Support")
        .join("Windsurf")
        .join("mcp.json");
    #[cfg(target_os = "windows")]
    return home
        .join("AppData")
        .join("Roaming")
        .join("Windsurf")
        .join("mcp.json");
    #[cfg(target_os = "linux")]
    return home.join(".config").join("windsurf").join("mcp.json");
    #[allow(unreachable_code)]
    home.join(".config").join("windsurf").join("mcp.json")
}

fn antigravity_path(home: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    return home
        .join("Library")
        .join("Application Support")
        .join("antigravity")
        .join("mcp.json");
    #[cfg(target_os = "windows")]
    return home
        .join("AppData")
        .join("Roaming")
        .join("antigravity")
        .join("mcp.json");
    #[allow(unreachable_code)]
    home.join(".config").join("antigravity").join("mcp.json")
}

/// Common shape for Claude Code / Cursor / Windsurf / Antigravity: a JSON
/// object with `mcpServers.<name> = { command, args, env }`.
///
/// We MERGE with whatever is already there — never overwrite the whole
/// file. Existing entries for other MCP servers are preserved. Our entry
/// is keyed `memento-memory` and gets overwritten on each call to reflect
/// the latest server_url / token.
fn write_json_mcp(path: &Path, sidecar: &str, env: &Value) -> Result<()> {
    let parent = path.parent().context("config path has no parent")?;
    // Don't create the config dir for tools that the user hasn't set up.
    // Without this, every Memento install would magic ~/.cursor/ into
    // existence even for users who don't use Cursor.
    if !parent.exists() {
        anyhow::bail!("tool dir doesn't exist: {}", parent.display());
    }

    let mut root: Value = if path.exists() {
        let bytes = fs::read(path)?;
        serde_json::from_slice(&bytes).unwrap_or(json!({}))
    } else {
        json!({})
    };

    if !root.is_object() {
        root = json!({});
    }
    let map = root.as_object_mut().expect("just ensured object");
    let mcp_servers = map.entry("mcpServers".to_string()).or_insert(json!({}));
    if !mcp_servers.is_object() {
        *mcp_servers = json!({});
    }
    mcp_servers
        .as_object_mut()
        .expect("just ensured object")
        .insert(
            "memento-memory".to_string(),
            json!({
                "command": sidecar,
                "args": [],
                "env": env,
            }),
        );

    atomic_write(path, serde_json::to_vec_pretty(&root)?.as_slice())
}

/// Codex uses TOML with [mcp_servers.<name>] sections. toml_edit
/// preserves the user's existing formatting + comments, which is what
/// you want for a config file someone might be hand-editing.
fn write_codex_mcp(path: &Path, sidecar: &str, server_url: &str, token: &str) -> Result<()> {
    let parent = path.parent().context("codex path has no parent")?;
    if !parent.exists() {
        anyhow::bail!("codex dir doesn't exist: {}", parent.display());
    }

    let text = if path.exists() {
        fs::read_to_string(path)?
    } else {
        String::new()
    };
    let rendered = update_codex_mcp_document(&text, sidecar, server_url, token)?;

    atomic_write(path, rendered.as_bytes())
}

fn update_codex_mcp_document(
    text: &str,
    sidecar: &str,
    server_url: &str,
    token: &str,
) -> Result<String> {
    let mut doc: toml_edit::DocumentMut = text
        .parse()
        .unwrap_or_else(|_| toml_edit::DocumentMut::new());

    // Get or create [mcp_servers] parent table.
    if doc.get("mcp_servers").is_none() {
        doc.insert(
            "mcp_servers",
            toml_edit::Item::Table(toml_edit::Table::new()),
        );
    }
    let parent_tbl = doc["mcp_servers"]
        .as_table_mut()
        .context("mcp_servers exists but isn't a table")?;
    if parent_tbl
        .get("memento-memory")
        .and_then(|item| item.as_table())
        .is_none()
    {
        parent_tbl.insert(
            "memento-memory",
            toml_edit::Item::Table(toml_edit::Table::new()),
        );
    }
    let entry = parent_tbl["memento-memory"]
        .as_table_mut()
        .context("memento-memory exists but isn't a table")?;
    entry.insert("command", toml_edit::value(sidecar));
    let mut args = toml_edit::Array::new();
    args.fmt();
    entry.insert("args", toml_edit::value(args));
    if entry.get("startup_timeout_sec").is_none() {
        entry.insert(
            "startup_timeout_sec",
            toml_edit::value(CODEX_MCP_STARTUP_TIMEOUT_SECS),
        );
    }
    if entry.get("env").and_then(|item| item.as_table()).is_none() {
        entry.insert("env", toml_edit::Item::Table(toml_edit::Table::new()));
    }
    let env_tbl = entry["env"]
        .as_table_mut()
        .context("memento-memory.env exists but isn't a table")?;
    env_tbl.insert("MEMENTO_SERVER_URL", toml_edit::value(server_url));
    env_tbl.insert("MEMENTO_SERVER_TOKEN", toml_edit::value(token));

    Ok(doc.to_string())
}

fn atomic_write(path: &Path, contents: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, contents)?;
    fs::rename(&tmp, path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_update_preserves_timeout_comments_other_servers_and_custom_fields() {
        let input = r#"# user comment
[mcp_servers.other]
command = "other-server"

[mcp_servers.memento-memory]
command = "old-sidecar"
startup_timeout_sec = 123
custom_field = "keep-me"

[mcp_servers.memento-memory.env]
MEMENTO_SERVER_URL = "old-url"
MEMENTO_SERVER_TOKEN = "old-token"
CUSTOM_ENV = "keep-me-too"
"#;

        let rendered = update_codex_mcp_document(
            input,
            r"C:\Memento\memento-mcp-sidecar.exe",
            "https://memento.invalid",
            "new-token",
        )
        .expect("update succeeds");
        let doc = rendered
            .parse::<toml_edit::DocumentMut>()
            .expect("valid TOML");

        assert!(rendered.contains("# user comment"));
        assert_eq!(
            doc["mcp_servers"]["other"]["command"].as_str(),
            Some("other-server")
        );
        assert_eq!(
            doc["mcp_servers"]["memento-memory"]["startup_timeout_sec"].as_integer(),
            Some(123)
        );
        assert_eq!(
            doc["mcp_servers"]["memento-memory"]["custom_field"].as_str(),
            Some("keep-me")
        );
        assert_eq!(
            doc["mcp_servers"]["memento-memory"]["env"]["CUSTOM_ENV"].as_str(),
            Some("keep-me-too")
        );
    }

    #[test]
    fn codex_update_adds_default_startup_timeout() {
        let rendered = update_codex_mcp_document(
            "",
            "memento-mcp-sidecar",
            "https://memento.invalid",
            "token",
        )
        .expect("update succeeds");
        let doc = rendered
            .parse::<toml_edit::DocumentMut>()
            .expect("valid TOML");

        assert_eq!(
            doc["mcp_servers"]["memento-memory"]["startup_timeout_sec"].as_integer(),
            Some(CODEX_MCP_STARTUP_TIMEOUT_SECS)
        );
    }

    #[test]
    fn onedir_mcp_sidecar_is_preferred_with_legacy_fallback() {
        let root = std::env::temp_dir().join(format!(
            "memento-mcp-locate-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock after epoch")
                .as_nanos()
        ));
        let suffix = if cfg!(windows) { ".exe" } else { "" };
        let executable = format!("memento-mcp-sidecar{suffix}");
        let legacy = root.join(&executable);
        let onedir = root
            .join("binaries")
            .join("memento-mcp-sidecar")
            .join(&executable);
        fs::create_dir_all(onedir.parent().expect("onedir parent")).expect("create tree");
        fs::write(&legacy, b"legacy").expect("write legacy");
        fs::write(&onedir, b"onedir").expect("write onedir");

        assert_eq!(locate_mcp_sidecar_in(&root).expect("locate onedir"), onedir);
        fs::remove_file(&onedir).expect("remove onedir");
        assert_eq!(locate_mcp_sidecar_in(&root).expect("locate legacy"), legacy);

        fs::remove_dir_all(root).expect("clean temp tree");
    }
}
