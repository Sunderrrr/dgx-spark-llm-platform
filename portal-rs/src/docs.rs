//! The API documentation page, parity with dgx-portal/app.py `docs`: a
//! standalone HTML page (no CDN, the box is LAN/netbird) listing the models
//! served now and the /v1 endpoints with curl, Python and JavaScript examples.

/// html.escape(s) with quote=True, as Python writes it.
pub fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// The whole page. `models` are the served model ids (may be empty).
pub fn html(platform: &str, public_api: &str, auto: &str, models: &[String]) -> String {
    let base = escape(public_api);
    let auto = escape(auto);
    let platform = escape(platform);
    let model_rows: String = if models.is_empty() {
        r#"<span style="color:#9ca3af">aucun modèle en cours</span>"#.to_string()
    } else {
        models.iter().map(|m| format!("<code>{}</code><br>", escape(m))).collect()
    };
    let curl_chat = format!(
        "curl {base}/v1/chat/completions \\\n  -H \"Authorization: Bearer $DGX_KEY\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{{\"model\": \"{auto}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Bonjour\"}}]}}'"
    );
    let py_chat = format!(
        "from openai import OpenAI\nclient = OpenAI(base_url=\"{base}/v1\", api_key=\"<ta clé>\")\nr = client.chat.completions.create(model=\"{auto}\",\n    messages=[{{\"role\": \"user\", \"content\": \"Bonjour\"}}])\nprint(r.choices[0].message.content)"
    );
    let js_chat = format!(
        "import OpenAI from \"openai\";\nconst client = new OpenAI({{ baseURL: \"{base}/v1\", apiKey: \"<ta clé>\" }});\nconst r = await client.chat.completions.create({{\n  model: \"{auto}\",\n  messages: [{{ role: \"user\", content: \"Bonjour\" }}]\n}});\nconsole.log(r.choices[0].message.content);"
    );
    let models_curl = escape(&format!("curl {base}/v1/models -H \"Authorization: Bearer $DGX_KEY\""));
    format!(
        r#"<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Docs API — {platform}</title></head>
<body style="margin:0;background:#f9fafb;color:#111827;font:15px/1.6 system-ui">
<div style="max-width:860px;margin:0 auto;padding:32px 20px">
<h1 style="font:700 26px system-ui;margin:0 0 4px">API {platform}</h1>
<p style="margin:0 0 24px;color:#6b7280">Compatible OpenAI — crée une clé puis appelle comme un provider classique. URL de base : <code>{base}</code></p>
<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:20px 24px;margin:0 0 24px">
<h2 style="font:700 16px system-ui;margin:0 0 8px">Modèles actuellement servis</h2>
{model_rows}
<p style="margin:8px 0 0;color:#374151">L'alias <code>{auto}</code> pointe toujours vers le modèle de chat en cours : branche-le une fois, plus besoin de renommer.</p>
</div>
<h2 style="font:700 18px system-ui">GET /v1/models</h2>
<p style="color:#6b7280">Liste les modèles disponibles.</p>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{models_curl}</pre>
<h2 style="font:700 18px system-ui">POST /v1/chat/completions</h2>
<p style="color:#6b7280">Génération (streaming via <code>"stream": true</code>).</p>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{curl_chat}</pre>
<h3 style="font:700 15px system-ui">Python</h3>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{py_chat}</pre>
<h3 style="font:700 15px system-ui">JavaScript</h3>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 16px;border-radius:8px;overflow:auto">{js_chat}</pre>
<p style="margin:32px 0 0;color:#9ca3af;font:12px system-ui">Gère tes clés depuis les réglages. Les tokens comptent sur ton budget de compte.</p>
</div></body></html>"#,
        platform = platform,
        base = base,
        model_rows = model_rows,
        auto = auto,
        models_curl = models_curl,
        curl_chat = escape(&curl_chat),
        py_chat = escape(&py_chat),
        js_chat = escape(&js_chat),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escapes_like_python() {
        assert_eq!(escape("a&b<c>\"d'"), "a&amp;b&lt;c&gt;&quot;d&#x27;");
    }

    #[test]
    fn lists_models_or_says_none() {
        let none = html("DGX", "https://x/v1", "auto-model", &[]);
        assert!(none.contains("aucun modèle en cours"));
        let some = html("DGX", "https://x/v1", "auto-model", &["qwen<1>".to_string()]);
        assert!(some.contains("<code>qwen&lt;1&gt;</code><br>"));
        assert!(some.contains("<title>Docs API — DGX</title>"));
    }
}
