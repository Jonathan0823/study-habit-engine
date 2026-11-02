use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Debug, Deserialize)]
pub struct Admission {
    pub operation_id: String,
    pub status: String,
    pub result: Value,
    pub callback_notice: Option<String>,
}

pub struct NormalizedUpdate {
    pub update_id: i64,
    pub actor_id: i64,
    pub chat_id: i64,
    pub chat_type: String,
    pub kind: String,
    pub command: String,
    pub arguments: Vec<String>,
    pub text: Option<String>,
}

pub async fn admit_update(
    client: &Client,
    api_url: &str,
    token: &str,
    poller_fence: i64,
    update: NormalizedUpdate,
) -> Result<Admission, String> {
    let payload = if update.kind == "text" {
        json!({"kind":"text","text":update.text})
    } else {
        json!({"kind":"command","command":update.command,"arguments":update.arguments})
    };
    let response = client
        .post(format!("{api_url}/v1/telegram/updates"))
        .bearer_auth(token)
        .json(&json!({
            "bot_id": std::env::var("TELEGRAM_BOT_ID").unwrap_or_else(|_| "12345".into()),
            "update_id": update.update_id.to_string(),
            "actor_id": update.actor_id.to_string(),
            "chat_id": update.chat_id.to_string(),
            "chat_type": update.chat_type,
            "poller_fence": poller_fence,
            "payload": payload
        }))
        .send()
        .await
        .map_err(|_| "admission request failed".to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "admission rejected with HTTP {}",
            response.status()
        ));
    }
    response
        .json()
        .await
        .map_err(|_| "invalid admission response".into())
}

#[derive(Deserialize)]
struct TelegramResponse<T> {
    ok: bool,
    result: T,
}

#[derive(Deserialize)]
struct TelegramUpdate {
    update_id: i64,
    message: Option<TelegramMessage>,
}

#[derive(Deserialize)]
struct TelegramMessage {
    from: Option<TelegramUser>,
    chat: TelegramChat,
    text: Option<String>,
}

#[derive(Deserialize)]
struct TelegramUser {
    id: i64,
}

#[derive(Deserialize)]
struct TelegramChat {
    id: i64,
    #[serde(rename = "type")]
    kind: String,
}

#[derive(Deserialize)]
struct PollerClaim {
    next_offset: i64,
    fence: i64,
}

#[derive(Deserialize)]
struct Delivery {
    delivery_id: String,
    fence: i64,
    chat_id: String,
    text: String,
}

#[derive(Serialize)]
struct DeliveryResult {
    fence: i64,
    outcome: &'static str,
    telegram_receipt: Option<String>,
}

async fn telegram<T: for<'de> Deserialize<'de>>(
    client: &Client,
    token: &str,
    method: &str,
    body: Value,
) -> Result<T, String> {
    let telegram_api =
        std::env::var("TELEGRAM_API_URL").unwrap_or_else(|_| "https://api.telegram.org".into());
    let response: TelegramResponse<T> = client
        .post(format!("{telegram_api}/bot{token}/{method}"))
        .json(&body)
        .send()
        .await
        .map_err(|_| "telegram request failed".to_string())?
        .error_for_status()
        .map_err(|_| "telegram request failed".to_string())?
        .json()
        .await
        .map_err(|_| "invalid telegram response".to_string())?;
    if !response.ok {
        return Err("telegram request failed".into());
    }
    Ok(response.result)
}

pub async fn run() -> Result<(), String> {
    let api_url = std::env::var("API_URL").map_err(|_| "API_URL is required")?;
    let token =
        std::env::var("TELEGRAM_BOT_TOKEN").map_err(|_| "TELEGRAM_BOT_TOKEN is required")?;
    let service_token =
        std::env::var("BOT_SERVICE_TOKEN").map_err(|_| "BOT_SERVICE_TOKEN is required")?;
    let bot_id = std::env::var("TELEGRAM_BOT_ID").map_err(|_| "TELEGRAM_BOT_ID is required")?;
    let client = Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|_| "HTTP client initialization failed")?;
    let owner_id = Uuid::new_v4();
    let mut offset = 0;
    loop {
        let claim_response = client
            .post(format!("{api_url}/v1/telegram/claim"))
            .bearer_auth(&service_token)
            .json(&json!({"bot_id":bot_id,"owner_id":owner_id}))
            .send()
            .await
            .map_err(|error| format!("poller claim request failed: {error}"))?;
        if claim_response.status() == StatusCode::CONFLICT {
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            continue;
        }
        let claim_status = claim_response.status();
        let claim: PollerClaim = claim_response
            .error_for_status()
            .map_err(|_| format!("poller claim failed with HTTP {claim_status}"))?
            .json()
            .await
            .map_err(|_| "invalid poller claim")?;
        offset = claim.next_offset.max(offset);
        let updates: Vec<TelegramUpdate> = telegram(
            &client,
            &token,
            "getUpdates",
            json!({"offset":offset,"timeout":10}),
        )
        .await?;
        for update in updates {
            let (sender, chat_id, chat_type, kind, command, arguments, text) = match update.message
            {
                Some(message) => {
                    let sender = message.from.map(|u| u.id).unwrap_or_default();
                    let message_text = message.text.unwrap_or_default();
                    if message_text.starts_with('/') {
                        let mut parts = message_text.split_whitespace();
                        let raw_command = parts.next().unwrap_or("");
                        let command = raw_command
                            .trim_start_matches('/')
                            .split('@')
                            .next()
                            .unwrap_or("");
                        let command = if command.is_empty() {
                            "unsupported"
                        } else {
                            command
                        }
                        .to_string();
                        (
                            sender,
                            message.chat.id,
                            message.chat.kind,
                            "command".to_string(),
                            command,
                            parts.map(str::to_string).collect(),
                            None,
                        )
                    } else if !message_text.trim().is_empty() {
                        (
                            sender,
                            message.chat.id,
                            message.chat.kind,
                            "text".to_string(),
                            String::new(),
                            vec![],
                            Some(message_text),
                        )
                    } else {
                        (
                            sender,
                            message.chat.id,
                            message.chat.kind,
                            "command".to_string(),
                            "unsupported".to_string(),
                            vec![],
                            None,
                        )
                    }
                }
                None => (
                    0,
                    0,
                    "private".to_string(),
                    "command".to_string(),
                    "unsupported".to_string(),
                    vec![],
                    None,
                ),
            };
            let _ = admit_update(
                &client,
                &api_url,
                &service_token,
                claim.fence,
                NormalizedUpdate {
                    update_id: update.update_id,
                    actor_id: sender,
                    chat_id,
                    chat_type,
                    kind,
                    command,
                    arguments,
                    text,
                },
            )
            .await?;
            offset = update.update_id + 1;
        }
        dispatch_one(&client, &api_url, &service_token, &token).await;
    }
}

async fn dispatch_one(client: &Client, api_url: &str, service_token: &str, telegram_token: &str) {
    let response = match client
        .post(format!("{api_url}/v1/deliveries/claim"))
        .bearer_auth(service_token)
        .send()
        .await
    {
        Ok(r) => r,
        Err(_) => return,
    };
    if response.status() == StatusCode::NO_CONTENT {
        return;
    }
    let delivery: Delivery = match response.json().await {
        Ok(d) => d,
        Err(_) => return,
    };
    let sent = telegram::<Value>(
        client,
        telegram_token,
        "sendMessage",
        json!({"chat_id":delivery.chat_id,"text":delivery.text}),
    )
    .await;
    let (outcome, receipt) = match sent {
        Ok(value) => ("sent", value.get("message_id").map(|id| id.to_string())),
        Err(_) => ("transient_failure", None),
    };
    let _ = client
        .post(format!(
            "{api_url}/v1/deliveries/{}/result",
            delivery.delivery_id
        ))
        .bearer_auth(service_token)
        .json(&DeliveryResult {
            fence: delivery.fence,
            outcome,
            telegram_receipt: receipt,
        })
        .send()
        .await;
}
