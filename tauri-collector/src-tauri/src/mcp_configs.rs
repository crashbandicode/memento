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

#[derive(Debug, Clone, Serialize)]
pub struct McpWriteReport {
    /// Tool ids whose config we wrote successfully (e.g. "claude_code").
    pub configured: Vec<String>,
    /// Tool ids we tried but couldn't write (missing parent dir, perm error, etc.).
    pub skipped: Vec<String>,
    /// Path of the bundled sidecar we pointed entries at.
    pub sidecar_path: String,
}

/// Locate the MCP sidecar binary that ships next to the main app exe.
/// Tauri's NSIS / MSI bundlers drop externalBin binaries in the same
/// directory as the main executable, with the triple suffix stripped.
pub fn locate_mcp_sidecar() -> Result<PathBuf> {
    let exe = std::env::current_exe().context("current_exe()")?;
    let dir = exe.parent().context("exe has no parent dir")?;
    let exe_suffix = if cfg!(windows) { ".exe" } else { "" };
    let bin = dir.join(format!("memento-mcp-sidecar{exe_suffix}"));
    if !bin.exists() {
        anyhow::bail!(
            "MCP sidecar not found at {}; the .msi may have been built without it",
            bin.display()
        );
    }
    Ok(bin)
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
        ("cursor",      home.join(".cursor").join("mcp.json")),
        ("windsurf",    windsurf_path(&home)),
        ("antigravity", antigravity_path(&home)),
    ];
    for (tool, path) in json_targets {
        match write_json_mcp(path, &sidecar_str, &env) {
            Ok(()) => configured.push((*tool).to_string()),
            Err(_) => skipped.push((*tool).to_string()),
        }
    }

    // ── Codex (TOML) ──────────────────────────────────────────
    match write_codex_mcp(&home.join(".codex").join("config.toml"), &sidecar_str,
                         server_url, server_token) {
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
    return home.join("Library").join("Application Support").join("Windsurf").join("mcp.json");
    #[cfg(target_os = "windows")]
    return home.join("AppData").join("Roaming").join("Windsurf").join("mcp.json");
    #[cfg(target_os = "linux")]
    return home.join(".config").join("windsurf").join("mcp.json");
    #[allow(unreachable_code)]
    home.join(".config").join("windsurf").join("mcp.json")
}

fn antigravity_path(home: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    return home.join("Library").join("Application Support").join("antigravity").join("mcp.json");
    #[cfg(target_os = "windows")]
    return home.join("AppData").join("Roaming").join("antigravity").join("mcp.json");
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
    let mcp_servers = map
        .entry("mcpServers".to_string())
        .or_insert(json!({}));
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
    let mut doc: toml_edit::DocumentMut =
        text.parse().unwrap_or_else(|_| toml_edit::DocumentMut::new());

    // Build the new [mcp_servers.memento-memory] block.
    let mut entry = toml_edit::Table::new();
    entry.insert("command", toml_edit::value(sidecar));
    let mut args = toml_edit::Array::new();
    args.fmt();
    entry.insert("args", toml_edit::value(args));
    let mut env_tbl = toml_edit::Table::new();
    env_tbl.insert("MEMENTO_SERVER_URL", toml_edit::value(server_url));
    env_tbl.insert("MEMENTO_SERVER_TOKEN", toml_edit::value(token));
    entry.insert("env", toml_edit::Item::Table(env_tbl));

    // Get or create [mcp_servers] parent table.
    if doc.get("mcp_servers").is_none() {
        doc.insert("mcp_servers", toml_edit::Item::Table(toml_edit::Table::new()));
    }
    let parent_tbl = doc["mcp_servers"]
        .as_table_mut()
        .context("mcp_servers exists but isn't a table")?;
    parent_tbl.insert("memento-memory", toml_edit::Item::Table(entry));

    atomic_write(path, doc.to_string().as_bytes())
}

fn atomic_write(path: &Path, contents: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, contents)?;
    fs::rename(&tmp, path)?;
    Ok(())
}
