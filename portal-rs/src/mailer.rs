//! Outgoing e-mail (notify.py): one HTML template with a plain-text
//! alternative, sent through the SMTP account of Admin → Config with
//! STARTTLS. Without a configured account every send is a quiet `false`.
//!
//! One deliberate difference: Python's `smtplib.starttls()` does not verify
//! the server certificate; this sender does (the system roots, or the file
//! named by SSL_CERT_FILE). A relay with a self-signed certificate needs its
//! CA there.
use chrono::Utc;
use lettre::message::{header::ContentType, MultiPart, SinglePart};
use lettre::transport::smtp::authentication::Credentials;
use lettre::{AsyncSmtpTransport, AsyncTransport, Message, Tokio1Executor};
use sqlx::SqlitePool;

/// Shown by the mail client as the sender's name.
pub const APP_NAME: &str = "DGX platform";

/// `config.ADMIN_URL`: the dashboard of the notification button.
pub fn admin_url() -> String {
    format!("{}/admin", std::env::var("DGX_WEB").unwrap_or_else(|_| "https://example.org".into()).trim_end_matches('/'))
}

/// `instance_config.setting(name)`: the stored `cfg_<name>`, else the default.
async fn setting(pool: &SqlitePool, name: &str, default: &str) -> String {
    let v: Option<String> = sqlx::query_scalar("SELECT value FROM settings WHERE key=?").bind(format!("cfg_{name}")).fetch_optional(pool).await.ok().flatten();
    v.filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

/// `instance_config.smtp_config()`.
#[derive(Debug, Clone, Default)]
pub struct SmtpConfig {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
    pub from: String,
    pub admin_email: String,
}

impl SmtpConfig {
    pub fn configured(&self) -> bool {
        !self.host.is_empty() && !self.user.is_empty() && !self.password.is_empty()
    }

    /// `sender_address()`: the address of a `Name <addr>` SMTP_FROM, else the account.
    pub fn sender_address(&self) -> String {
        match (self.from.find('<'), self.from.rfind('>')) {
            (Some(a), Some(b)) if b > a + 1 => self.from[a + 1..b].to_string(),
            _ => self.user.clone(),
        }
    }
}

pub async fn smtp_config(pool: &SqlitePool) -> SmtpConfig {
    let port = setting(pool, "smtp_port", "587").await;
    SmtpConfig {
        host: setting(pool, "smtp_host", "").await,
        port: crate::memory::py_strip(&port).parse().ok().filter(|p| *p != 0).unwrap_or(587),
        user: setting(pool, "smtp_user", "").await,
        password: setting(pool, "smtp_pass", "").await,
        from: setting(pool, "smtp_from", "").await,
        admin_email: setting(pool, "admin_email", "").await,
    }
}

/// `html.escape(str(value))`.
fn esc(v: &str) -> String {
    v.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}

/// `_render_html(heading, rows, body, footnote, cta_url)`.
fn render_html(platform: &str, heading: &str, rows: &[(&str, String)], body: Option<&str>, footnote: Option<&str>, cta: Option<&str>) -> String {
    let rows_html: String = rows
        .iter()
        .map(|(label, value)| {
            format!(
                "<tr><td style=\"padding:7px 18px 7px 0;color:#6b7280;font-size:13px;font-weight:600;white-space:nowrap;vertical-align:top;\">{}</td><td style=\"padding:7px 0;color:#111827;font-size:14px;vertical-align:top;\">{}</td></tr>",
                esc(label),
                esc(value)
            )
        })
        .collect();
    let body_html = body
        .filter(|b| !b.is_empty())
        .map(|b| format!("<p style=\"margin:16px 0 0;font-size:14px;line-height:1.6;color:#374151;\">{}</p>", esc(b).replace('\n', "<br>")))
        .unwrap_or_default();
    let footnote_html = footnote
        .filter(|f| !f.is_empty())
        .map(|f| format!("<p style=\"margin:18px 0 0;font-size:12px;color:#9ca3af;line-height:1.5;\">{}</p>", esc(f)))
        .unwrap_or_default();
    let cta_html = cta
        .filter(|c| !c.is_empty())
        .map(|c| {
            format!(
                "<p style=\"margin:20px 0 0;\"><a href=\"{}\" target=\"_blank\" rel=\"noopener\" style=\"display:inline-block;padding:11px 22px;border-radius:8px;background:#0f172a;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none;\">Open the Admin dashboard&nbsp;→</a></p>",
                esc(c)
            )
        })
        .unwrap_or_default();
    let stamp = Utc::now().format("%d %b %Y, %H:%M UTC");
    format!(
        r#"<!doctype html>
<html lang="en"><body style="margin:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);">
<tr><td style="padding:22px 30px;background:#0f172a;">
 <span style="font-size:17px;font-weight:700;color:#ffffff;letter-spacing:.2px;">DGX&nbsp;<span style="color:#4ade80;">platform</span></span>
</td></tr>
<tr><td style="padding:26px 30px;">
 <h1 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a;">{heading}</h1>
 <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;">{rows_html}</table>
 {body_html}
 {footnote_html}
 {cta_html}
</td></tr>
<tr><td style="padding:16px 30px;background:#f9fafb;border-top:1px solid #eef0f2;color:#9ca3af;font-size:12px;">
 {platform}&nbsp;·&nbsp;Automated message&nbsp;·&nbsp;{stamp}
</td></tr>
</table>
</td></tr>
</table>
</body></html>"#,
        heading = esc(heading),
        platform = esc(platform),
    )
}

/// `_email_parts`: the plain-text alternative.
fn render_text(platform: &str, heading: &str, rows: &[(&str, String)], body: Option<&str>, footnote: Option<&str>) -> String {
    let mut t = vec![heading.to_string(), String::new()];
    t.extend(rows.iter().map(|(l, v)| format!("{l}: {v}")));
    if let Some(b) = body.filter(|b| !b.is_empty()) {
        t.push(b.to_string());
    }
    if let Some(f) = footnote.filter(|f| !f.is_empty()) {
        t.push(String::new());
        t.push(f.to_string());
    }
    t.push(String::new());
    t.push(format!("— {platform}"));
    t.join("\n")
}

/// One e-mail: the recipient, the subject line, then what the template shows.
pub struct Mail<'a> {
    pub to: &'a str,
    pub subject: &'a str,
    pub heading: &'a str,
    pub rows: Vec<(&'a str, String)>,
    pub body: Option<&'a str>,
    pub footnote: Option<&'a str>,
    pub cta_url: Option<String>,
}

/// `_send(...)`: true when the SMTP server accepted the message.
pub async fn send(pool: &SqlitePool, mail: Mail<'_>) -> bool {
    let smtp = smtp_config(pool).await;
    if !smtp.configured() || mail.to.is_empty() {
        return false;
    }
    let platform = crate::chat::support_tools::platform_name(pool).await;
    let text = render_text(&platform, mail.heading, &mail.rows, mail.body, mail.footnote);
    let html = render_html(&platform, mail.heading, &mail.rows, mail.body, mail.footnote, mail.cta_url.as_deref());
    let built = (|| {
        Message::builder()
            .from(format!("{APP_NAME} <{}>", smtp.sender_address()).parse().ok()?)
            .to(mail.to.parse().ok()?)
            .subject(mail.subject)
            .multipart(
                MultiPart::alternative()
                    .singlepart(SinglePart::builder().header(ContentType::TEXT_PLAIN).body(text))
                    .singlepart(SinglePart::builder().header(ContentType::TEXT_HTML).body(html)),
            )
            .ok()
    })();
    let Some(message) = built else {
        eprintln!("[email] erreur : invalid address");
        return false;
    };
    let transport = match AsyncSmtpTransport::<Tokio1Executor>::starttls_relay(&smtp.host) {
        Ok(t) => t.port(smtp.port).credentials(Credentials::new(smtp.user.clone(), smtp.password.clone())).build(),
        Err(e) => {
            eprintln!("[email] erreur : {e}");
            return false;
        }
    };
    match transport.send(message).await {
        Ok(_) => true,
        Err(e) => {
            eprintln!("[email] erreur : {e}");
            false
        }
    }
}

/// `send_user_email(to, subject, body)`: one person, the caller's text.
pub async fn send_user(pool: &SqlitePool, to: &str, subject: &str, body: &str) -> bool {
    send(pool, Mail { to, subject, heading: subject, rows: vec![], body: Some(body), footnote: None, cta_url: None }).await
}

/// `notify_maintenance_email(enabled, by_username, by_fullname)`.
pub async fn maintenance(pool: &SqlitePool, enabled: bool, by_username: &str, by_fullname: &str) -> bool {
    let to = smtp_config(pool).await.admin_email;
    let who = [by_fullname, by_username].into_iter().find(|s| !s.is_empty()).unwrap_or("—");
    send(
        pool,
        Mail {
            to: &to,
            subject: if enabled { "[DGX platform] Maintenance enabled" } else { "[DGX platform] Maintenance disabled" },
            heading: if enabled { "Maintenance mode enabled" } else { "Maintenance mode disabled" },
            rows: vec![("By", format!("{who} ({by_username})"))],
            body: Some(if enabled { "The portal is now blocking requests from non-admin accounts." } else { "The portal is accessible to everyone again." }),
            footnote: None,
            cta_url: Some(admin_url()),
        },
    )
    .await
}

/// `send_test_email()`: the Admin's SMTP test.
pub async fn test(pool: &SqlitePool) -> bool {
    let to = smtp_config(pool).await.admin_email;
    let platform = crate::chat::support_tools::platform_name(pool).await;
    send(
        pool,
        Mail {
            to: &to,
            subject: "[DGX platform] SMTP test",
            heading: "SMTP test",
            rows: vec![("Sender", APP_NAME.to_string()), ("Instance", platform)],
            body: Some("The SMTP configuration works. Nice!"),
            footnote: None,
            cta_url: Some(admin_url()),
        },
    )
    .await
}

/// Python's `f"{x:,.0f}"`: thousands separated by commas.
pub fn comma_grouped(x: f64) -> String {
    crate::chat::support_tools::grouped(x).replace(' ', ",")
}

/// `notify_email(model_id, username, fullname, reason)`: a model request.
pub async fn model_request(pool: &SqlitePool, model_id: &str, username: &str, fullname: Option<&str>, reason: &str) -> bool {
    let to = smtp_config(pool).await.admin_email;
    let subject = format!("[DGX platform] New model request — {model_id}");
    let who = fullname.filter(|f| !f.is_empty()).unwrap_or(username);
    send(
        pool,
        Mail {
            to: &to,
            subject: &subject,
            heading: "New model request",
            rows: vec![("User", format!("{who} ({username})")), ("Model", model_id.to_string()), ("Reason", if reason.is_empty() { "—".into() } else { reason.to_string() })],
            body: Some("A user is asking for this model to be added to the platform."),
            footnote: Some("Open the Admin dashboard to review and approve the request."),
            cta_url: Some(admin_url()),
        },
    )
    .await
}

/// `notify_budget_email(...)`: a token increase request.
pub async fn budget_request(pool: &SqlitePool, username: &str, fullname: &str, key_alias: &str, current: Option<f64>, reason: &str) -> bool {
    let to = smtp_config(pool).await.admin_email;
    let subject = format!("[DGX platform] Token increase request — {username}");
    send(
        pool,
        Mail {
            to: &to,
            subject: &subject,
            heading: "Token increase request",
            rows: vec![
                ("User", format!("{fullname} ({username})")),
                ("Key", key_alias.to_string()),
                ("Current budget", current.map_or("—".to_string(), |c| format!("{} tokens", comma_grouped(c)))),
                ("Reason", if reason.is_empty() { "—".into() } else { reason.to_string() }),
            ],
            body: Some("A user is asking for more tokens on their API key."),
            footnote: Some("Open the Admin dashboard to review and approve the request."),
            cta_url: Some(admin_url()),
        },
    )
    .await
}

/// `notify_media_request_email(category, username, fullname)`.
pub async fn media_request(pool: &SqlitePool, category: &str, username: &str, fullname: &str) -> bool {
    let to = smtp_config(pool).await.admin_email;
    let subject = format!("[DGX platform] {category} model requested");
    let heading = format!("{category} model requested");
    let who = if fullname.is_empty() { username } else { fullname };
    send(
        pool,
        Mail {
            to: &to,
            subject: &subject,
            heading: &heading,
            rows: vec![("User", format!("{who} ({username})")), ("Category", category.to_string())],
            body: Some("No model of this category is currently loaded. Launch one to enable it."),
            footnote: None,
            cta_url: Some(admin_url()),
        },
    )
    .await
}

/// `notify_infra_alert_email(kind, detail)`: an infra incident.
pub async fn infra_alert(pool: &SqlitePool, kind: &str, detail: &str) -> bool {
    let to = smtp_config(pool).await.admin_email;
    send(
        pool,
        Mail {
            to: &to,
            subject: &format!("[DGX platform] Alert — {kind}"),
            heading: "Platform alert",
            rows: vec![("Kind", kind.to_string()), ("Detail", if detail.is_empty() { "—".into() } else { detail.to_string() })],
            body: Some("Something went wrong on the platform. Please check the Admin dashboard."),
            footnote: None,
            cta_url: Some(admin_url()),
        },
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sender_address_follows_flask() {
        let mut c = SmtpConfig { user: "acct@example.org".into(), ..Default::default() };
        assert_eq!(c.sender_address(), "acct@example.org");
        c.from = "Portal <noreply@example.org>".into();
        assert_eq!(c.sender_address(), "noreply@example.org");
    }

    #[test]
    fn text_part_matches_the_python_layout() {
        let t = render_text("Bench", "SMTP test", &[("Sender", "DGX platform".into())], Some("Body"), Some("Note"));
        assert_eq!(t, "SMTP test\n\nSender: DGX platform\nBody\n\nNote\n\n— Bench");
    }

    #[test]
    fn html_escapes_values() {
        let h = render_html("B&B", "<h>", &[("k", "<v>".into())], Some("a\nb"), None, None);
        assert!(h.contains("&lt;h&gt;") && h.contains("&lt;v&gt;") && h.contains("a<br>b") && h.contains("B&amp;B"));
    }
}
