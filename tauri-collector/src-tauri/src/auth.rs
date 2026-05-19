//! In-app account register / login.
//!
//! Runs the HTTP calls from Rust (reqwest) rather than the webview so it
//! works against arbitrary self-hosted servers — including plain-http LAN
//! boxes — without tripping the webview CSP, CORS, or mixed-content rules
//! a `fetch()` from the WebView origin would hit.

use serde::{Deserialize, Serialize};

use crate::ipc::CmdError;

#[derive(Debug, Deserialize)]
pub struct AuthArgs {
    pub server_url: String,
    /// "register" | "login"
    pub mode: String,
    pub email: String,
    pub password: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub invite_code: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct AuthResult {
    pub collector_token: String,
    /// Normalized API base actually used (e.g. :3001 → :8001).
    pub server_url: String,
    pub role: String,
    pub email: String,
}

#[derive(Deserialize)]
struct UserResponse {
    email: String,
    role: String,
    collector_token: Option<String>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
}

#[derive(Deserialize)]
struct ApiError {
    detail: Option<String>,
}

/// Mirror dist/app.js `normalizeApiUrl`: users typically paste the web UI
/// URL (:3001) but the API the token pairs with lives on :8001. Also drop
/// any trailing slash so `{base}/api/...` never doubles up.
fn normalize_api_url(input: &str) -> String {
    let base = input.trim().trim_end_matches('/');
    if base.contains(":3001") {
        base.replace(":3001", ":8001")
    } else {
        base.to_string()
    }
}

/// Pull FastAPI's `{"detail": "..."}` out of an error response so the user
/// sees "Email already registered" instead of a bare "HTTP 400".
async fn error_detail(resp: reqwest::Response) -> String {
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();
    if let Ok(ApiError { detail: Some(d) }) = serde_json::from_str::<ApiError>(&body) {
        return d;
    }
    format!("HTTP {status}")
}

/// Exchange the saved collector token for a short-lived web JWT so the
/// embedded dashboard can be opened already-authenticated (no second
/// login). Hits /api/auth/token-exchange, which any per-user collector
/// token can call. Minted fresh on every dashboard open so expiry is a
/// non-issue.
#[tauri::command]
pub async fn mint_web_token(server_url: String, collector_token: String) -> Result<String, CmdError> {
    let base = normalize_api_url(&server_url);
    if base.is_empty() || collector_token.is_empty() {
        return Err(CmdError {
            message: "Server URL or token missing".into(),
        });
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .user_agent(concat!("memento-app/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let resp = client
        .post(format!("{base}/api/auth/token-exchange"))
        .header("X-Collector-Token", collector_token)
        .send()
        .await?;
    if !resp.status().is_success() {
        return Err(CmdError {
            message: error_detail(resp).await,
        });
    }
    let tok: TokenResponse = resp.json().await?;
    Ok(tok.access_token)
}

#[tauri::command]
pub async fn auth_request(args: AuthArgs) -> Result<AuthResult, CmdError> {
    let base = normalize_api_url(&args.server_url);
    if base.is_empty() {
        return Err(CmdError {
            message: "Server URL is empty".into(),
        });
    }

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .user_agent(concat!("memento-app/", env!("CARGO_PKG_VERSION")))
        .build()?;

    match args.mode.as_str() {
        "register" => {
            let resp = client
                .post(format!("{base}/api/auth/register"))
                .json(&serde_json::json!({
                    "email": args.email,
                    "password": args.password,
                    "name": args.name,
                    "invite_code": args.invite_code,
                }))
                .send()
                .await?;
            if !resp.status().is_success() {
                return Err(CmdError {
                    message: error_detail(resp).await,
                });
            }
            let u: UserResponse = resp.json().await?;
            let token = u.collector_token.ok_or_else(|| CmdError {
                message: "Server didn't return a collector token for this account".into(),
            })?;
            Ok(AuthResult {
                collector_token: token,
                server_url: base,
                role: u.role,
                email: u.email,
            })
        }
        "login" => {
            let resp = client
                .post(format!("{base}/api/auth/login"))
                .json(&serde_json::json!({
                    "email": args.email,
                    "password": args.password,
                }))
                .send()
                .await?;
            if !resp.status().is_success() {
                return Err(CmdError {
                    message: error_detail(resp).await,
                });
            }
            let tok: TokenResponse = resp.json().await?;

            // login only returns a JWT; the collector token lives on /me.
            let me = client
                .get(format!("{base}/api/auth/me"))
                .bearer_auth(&tok.access_token)
                .send()
                .await?;
            if !me.status().is_success() {
                return Err(CmdError {
                    message: error_detail(me).await,
                });
            }
            let u: UserResponse = me.json().await?;
            let token = u.collector_token.ok_or_else(|| CmdError {
                message: "This account has no collector token yet".into(),
            })?;
            Ok(AuthResult {
                collector_token: token,
                server_url: base,
                role: u.role,
                email: u.email,
            })
        }
        other => Err(CmdError {
            message: format!("Unknown auth mode: {other}"),
        }),
    }
}
