//! In-app account register / login.
//!
//! Runs the HTTP calls from Rust (reqwest) rather than the webview so it
//! works against arbitrary self-hosted servers — including plain-http LAN
//! boxes — without tripping the webview CSP, CORS, or mixed-content rules
//! a `fetch()` from the WebView origin would hit.

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri_plugin_shell::ShellExt;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

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

// ---------------------------------------------------------------------------
// GitHub sign-in — desktop loopback (RFC 8252) flow
// ---------------------------------------------------------------------------
//
// github.com/login/oauth/authorize sends `x-frame-options: deny`, so the
// dashboard iframe can NEVER render it: a full-page navigation from inside
// the frame just dies. The fix is the same one gh / gcloud / az use — run
// the authorization in the user's *system browser* and have the result
// handed back to the app over a one-shot HTTP listener on 127.0.0.1:
//
//   github_login  →  bind 127.0.0.1:0, mint nonce
//                 →  open system browser at
//                    {api}/api/auth/github/authorize?next=/auth/desktop?port=..&nonce=..
//   (browser)     →  github → server callback → JWT → web /auth/callback
//                 →  web /auth/desktop trades the JWT for the collector token
//                 →  redirects to http://127.0.0.1:<port>/?token=..&nonce=..
//   github_login  →  accepts that one request, verifies the nonce, answers
//                    with a "you can close this tab" page, returns the token.
//
// The server side is untouched: `next` stays a same-origin relative path, so
// the existing HMAC-signed state + redirect allow-list keep working. The hop
// to 127.0.0.1 happens in the browser, from the /auth/desktop page.

/// Total wall-clock budget for the browser round-trip. Long enough for a
/// GitHub login + 2FA, short enough that an abandoned flow doesn't leave a
/// socket parked on the loopback interface forever.
const LOGIN_TIMEOUT: Duration = Duration::from_secs(300);
/// Per-connection read budget. Browsers speculatively open (and then sit on)
/// TCP connections; without this a preconnect could wedge the accept loop.
const READ_TIMEOUT: Duration = Duration::from_secs(15);
/// Hard cap on how much of a request head we'll buffer, so a hostile local
/// process can't make us allocate without bound.
const MAX_REQUEST_BYTES: usize = 8 * 1024;

/// 128-bit hex nonce, no `rand` dependency.
///
/// `RandomState::new()` keys SipHash with 128 bits pulled from the OS CSPRNG
/// (`getrandom`/`arc4random` under the hood, seeded once per thread). SipHash
/// is a PRF, so hashing *distinct* fixed inputs under that secret key yields
/// outputs an attacker can't predict without the key — two 64-bit digests give
/// us 128 bits. SystemTime nanos are folded in as a cheap extra distinguisher;
/// the security rests on the OS-seeded key, not on the clock.
fn random_nonce() -> String {
    let state = RandomState::new();
    let salt = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let mut out = String::with_capacity(32);
    for domain in [0u64, 1u64] {
        let mut h = state.build_hasher();
        h.write_u64(domain);
        h.write_u64(salt);
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

/// Percent-encode for use as a query-string *value*: only RFC 3986 unreserved
/// characters survive. Written by hand rather than pulling in `percent-encoding`
/// (reqwest has it transitively, but this crate must not lean on that). The
/// `?` and `&` inside `next` MUST be escaped or FastAPI would parse them as
/// params of /github/authorize instead of as part of `next`.
fn percent_encode(input: &str) -> String {
    let mut out = String::with_capacity(input.len() * 3);
    for b in input.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(*b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Inverse of the above, for the values the browser hands back on loopback.
/// Both values we care about (token, nonce) are hex today, so this is a no-op
/// in practice — it's here so we stay correct if that ever changes.
fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(v) => {
                        out.push(v);
                        i += 3;
                    }
                    Err(_) => {
                        out.push(bytes[i]);
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Pull one param out of a `a=1&b=2` query string.
fn query_param(query: &str, key: &str) -> Option<String> {
    query.split('&').find_map(|pair| {
        let (k, v) = pair.split_once('=')?;
        (k == key).then(|| percent_decode(v))
    })
}

fn html_response(status: &str, body: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 {status}\r\n\
         Content-Type: text/html; charset=utf-8\r\n\
         Content-Length: {len}\r\n\
         Cache-Control: no-store\r\n\
         Connection: close\r\n\
         \r\n\
         {body}",
        len = body.len()
    )
    .into_bytes()
}

fn success_page() -> Vec<u8> {
    html_response(
        "200 OK",
        "<!doctype html><meta charset=\"utf-8\"><title>Memento</title>\
         <div style=\"font:16px/1.6 -apple-system,Segoe UI,system-ui,sans-serif;\
         max-width:32rem;margin:20vh auto;text-align:center;color:#1f2430\">\
         <h1 style=\"font-size:20px;margin:0 0 .5rem\">登录成功 · Signed in</h1>\
         <p style=\"margin:0;color:#5b6472\">可以关闭此页面并返回 Memento。<br>\
         You can close this tab and return to Memento.</p></div>",
    )
}

fn error_page() -> Vec<u8> {
    html_response(
        "400 Bad Request",
        "<!doctype html><meta charset=\"utf-8\"><title>Memento</title>\
         <div style=\"font:16px/1.6 -apple-system,Segoe UI,system-ui,sans-serif;\
         max-width:32rem;margin:20vh auto;text-align:center;color:#1f2430\">\
         <h1 style=\"font-size:20px;margin:0 0 .5rem\">登录失败 · Sign-in failed</h1>\
         <p style=\"margin:0;color:#5b6472\">请返回 Memento 重试。<br>\
         Please return to Memento and try again.</p></div>",
    )
}

/// Serve the loopback redirect and hand back the collector token.
///
/// Loops on accept because browsers open speculative/extra connections
/// (preconnect, favicon) that would otherwise consume our single shot; those
/// get a 404 and are dropped. The *first* request that actually carries a
/// `token` ends the flow either way — success or nonce mismatch — and the
/// caller then drops the listener, so exactly one authorization result is
/// ever served.
async fn await_loopback_token(listener: TcpListener, nonce: &str) -> Result<String, CmdError> {
    loop {
        let (mut stream, _peer) = listener.accept().await?;

        // --- read the request line, capped ---------------------------------
        let mut buf: Vec<u8> = Vec::with_capacity(512);
        let mut chunk = [0u8; 512];
        let line = loop {
            if let Some(pos) = buf.windows(2).position(|w| w == b"\r\n") {
                break Some(String::from_utf8_lossy(&buf[..pos]).into_owned());
            }
            if buf.len() >= MAX_REQUEST_BYTES {
                break None;
            }
            match tokio::time::timeout(READ_TIMEOUT, stream.read(&mut chunk)).await {
                Ok(Ok(0)) | Ok(Err(_)) | Err(_) => break None,
                Ok(Ok(n)) => buf.extend_from_slice(&chunk[..n]),
            }
        };
        let Some(line) = line else {
            continue; // preconnect / junk / oversized head → just drop it
        };

        // "GET /?token=..&nonce=.. HTTP/1.1"
        let target = line.split_whitespace().nth(1).unwrap_or("");
        let query = target.split_once('?').map(|(_, q)| q).unwrap_or("");
        let Some(token) = query_param(query, "token").filter(|t| !t.is_empty()) else {
            let _ = stream.write_all(&html_response("404 Not Found", "not found")).await;
            let _ = stream.flush().await;
            let _ = stream.shutdown().await;
            continue; // e.g. GET /favicon.ico — not our redirect
        };

        // --- nonce check ---------------------------------------------------
        // Plain `==` on a 128-bit random value: a timing side-channel would
        // need the attacker to already be running code on this machine and
        // still buys them nothing against 2^128 of unpredictable entropy.
        let got = query_param(query, "nonce").unwrap_or_default();
        if got != nonce {
            let _ = stream.write_all(&error_page()).await;
            let _ = stream.flush().await;
            let _ = stream.shutdown().await;
            return Err(CmdError {
                message: "GitHub sign-in failed: state mismatch (nonce). Please try again.".into(),
            });
        }

        // Write + flush *before* the socket drops, or the browser reports a
        // connection reset instead of showing the "you can close this" page.
        stream.write_all(&success_page()).await?;
        stream.flush().await?;
        let _ = stream.shutdown().await;
        return Ok(token);
    }
}

/// Sign in with GitHub from the desktop app.
///
/// Opens the system browser, waits for the one-shot loopback callback, and
/// resolves the durable per-user collector token — the desktop's actual
/// identity. Everything downstream (MCP config, sidecar, and the web session
/// via `mint_web_token` → /auth/handoff) already keys off that token.
#[tauri::command]
pub async fn github_login(app: tauri::AppHandle, server_url: String) -> Result<AuthResult, CmdError> {
    let base = normalize_api_url(&server_url);
    if base.is_empty() {
        return Err(CmdError {
            message: "Server URL is empty".into(),
        });
    }

    // Bind first: the port has to be in the URL we're about to open.
    let listener = TcpListener::bind("127.0.0.1:0").await.map_err(|e| CmdError {
        message: format!("Couldn't open a local callback port: {e}"),
    })?;
    let port = listener.local_addr()?.port();
    let nonce = random_nonce();

    let next = format!("/auth/desktop?port={port}&nonce={nonce}");
    let auth_url = format!(
        "{base}/api/auth/github/authorize?next={}",
        percent_encode(&next)
    );

    // Shell::open is soft-deprecated in favour of tauri-plugin-opener, but the
    // shell plugin is already wired up (plugin + `"shell": {"open": true}` scope
    // + capability) and still ships `open`. Adding a whole new plugin just for
    // this one call would mean a new dep on a 3-platform cross-compile matrix.
    #[allow(deprecated)]
    app.shell().open(&auth_url, None).map_err(|e| CmdError {
        message: format!("Couldn't open your browser: {e}"),
    })?;

    let collector_token = tokio::time::timeout(LOGIN_TIMEOUT, await_loopback_token(listener, &nonce))
        .await
        .map_err(|_| CmdError {
            message: "GitHub sign-in timed out. Please try again.".into(),
        })??;
    // listener is dropped here — the callback port is closed either way.

    // The loopback only carries the collector token; resolve email/role the
    // same way the password `login` path does: collector token → JWT → /me.
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .user_agent(concat!("memento-app/", env!("CARGO_PKG_VERSION")))
        .build()?;

    let resp = client
        .post(format!("{base}/api/auth/token-exchange"))
        .header("X-Collector-Token", &collector_token)
        .send()
        .await?;
    if !resp.status().is_success() {
        return Err(CmdError {
            message: error_detail(resp).await,
        });
    }
    let tok: TokenResponse = resp.json().await?;

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

    Ok(AuthResult {
        collector_token: u.collector_token.unwrap_or(collector_token),
        server_url: base,
        role: u.role,
        email: u.email,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn next_param_is_fully_escaped() {
        // The whole point: ? and & inside `next` must not survive as literals,
        // or FastAPI reads `nonce` as a param of /github/authorize.
        let encoded = percent_encode("/auth/desktop?port=51234&nonce=abc123");
        assert_eq!(
            encoded,
            "%2Fauth%2Fdesktop%3Fport%3D51234%26nonce%3Dabc123"
        );
        assert!(!encoded.contains('?') && !encoded.contains('&') && !encoded.contains('/'));
    }

    #[test]
    fn query_params_round_trip() {
        let q = "token=deadbeef&nonce=cafe%2Fbabe";
        assert_eq!(query_param(q, "token").unwrap(), "deadbeef");
        assert_eq!(query_param(q, "nonce").unwrap(), "cafe/babe");
        assert_eq!(query_param(q, "missing"), None);
    }

    #[test]
    fn nonce_is_128_bits_of_hex_and_not_repeated() {
        let a = random_nonce();
        let b = random_nonce();
        assert_eq!(a.len(), 32, "32 hex chars = 128 bits");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    /// Drive the real listener over a real loopback socket: good nonce → token
    /// returned + a 200 written back to the browser.
    #[tokio::test]
    async fn loopback_returns_token_on_matching_nonce() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let nonce = "0123456789abcdef0123456789abcdef".to_string();

        let n = nonce.clone();
        let server = tokio::spawn(async move { await_loopback_token(listener, &n).await });

        let mut c = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        c.write_all(
            format!("GET /?token=tok_abc&nonce={nonce} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                .as_bytes(),
        )
        .await
        .unwrap();
        let mut resp = String::new();
        c.read_to_string(&mut resp).await.unwrap();

        assert_eq!(server.await.unwrap().unwrap(), "tok_abc");
        assert!(resp.starts_with("HTTP/1.1 200 OK"), "got: {resp}");
        assert!(resp.contains("Memento"));
    }

    /// A wrong nonce must fail the login, not silently accept the token.
    #[tokio::test]
    async fn loopback_rejects_bad_nonce() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();

        let server =
            tokio::spawn(async move { await_loopback_token(listener, "expected-nonce").await });

        let mut c = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        c.write_all(b"GET /?token=tok_abc&nonce=attacker HTTP/1.1\r\n\r\n")
            .await
            .unwrap();
        let mut resp = String::new();
        c.read_to_string(&mut resp).await.unwrap();

        assert!(server.await.unwrap().is_err());
        assert!(resp.starts_with("HTTP/1.1 400"), "got: {resp}");
    }

    /// Browsers open speculative connections and fetch /favicon.ico; neither
    /// may consume our one shot.
    #[tokio::test]
    async fn favicon_request_does_not_consume_the_listener() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let nonce = "aaaabbbbccccddddeeeeffff00001111".to_string();

        let n = nonce.clone();
        let server = tokio::spawn(async move { await_loopback_token(listener, &n).await });

        let mut junk = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        junk.write_all(b"GET /favicon.ico HTTP/1.1\r\n\r\n").await.unwrap();
        let mut junk_resp = String::new();
        junk.read_to_string(&mut junk_resp).await.unwrap();
        assert!(junk_resp.starts_with("HTTP/1.1 404"), "got: {junk_resp}");

        // ...and the real callback still lands.
        let mut c = tokio::net::TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        c.write_all(format!("GET /?token=real&nonce={nonce} HTTP/1.1\r\n\r\n").as_bytes())
            .await
            .unwrap();
        let mut resp = String::new();
        c.read_to_string(&mut resp).await.unwrap();

        assert_eq!(server.await.unwrap().unwrap(), "real");
        assert!(resp.starts_with("HTTP/1.1 200 OK"));
    }
}
