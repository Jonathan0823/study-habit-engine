use reqwest::Client;
use serde_json::json;
use uuid::Uuid;

use study_habit_engine_bot::{NormalizedUpdate, admit_update};

#[tokio::test]
async fn simulated_help_update_is_admitted_once_and_delivered_from_outbox() {
    let api = std::env::var("TEST_API_URL").expect("run through scripts/test-e2e.sh");
    let secret = std::env::var("BOT_SERVICE_TOKEN").unwrap_or_else(|_| "bot-secret".into());
    let client = Client::new();
    let claim: serde_json::Value = client
        .post(format!("{api}/v1/telegram/claim"))
        .bearer_auth(&secret)
        .json(&json!({"bot_id":"12345","owner_id":Uuid::new_v4()}))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let fence = claim["fence"].as_i64().unwrap();

    let make_update = || NormalizedUpdate {
        update_id: 900001,
        actor_id: 111,
        chat_id: 111,
        chat_type: "private".into(),
        kind: "command".into(),
        command: "help".into(),
        arguments: vec![],
        text: None,
    };
    let first = admit_update(&client, &api, &secret, fence, make_update())
        .await
        .unwrap();
    let retry = admit_update(&client, &api, &secret, fence, make_update())
        .await
        .unwrap();
    assert_eq!(first.operation_id, retry.operation_id);
    assert_eq!(first.status, "accepted");
    assert_eq!(first.callback_notice.as_deref(), Some("Accepted"));

    let delivery: serde_json::Value = client
        .post(format!("{api}/v1/deliveries/claim"))
        .bearer_auth(&secret)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(delivery["operation_id"], first.operation_id);
    assert!(
        delivery["text"]
            .as_str()
            .unwrap()
            .starts_with("/help - Show supported commands and their usage")
    );
    let result = client.post(format!("{api}/v1/deliveries/{}/result", delivery["delivery_id"].as_str().unwrap()))
        .bearer_auth(&secret)
        .json(&json!({"fence":delivery["fence"],"outcome":"sent","telegram_receipt":"test-message-1"}))
        .send().await.unwrap();
    assert_eq!(result.status(), reqwest::StatusCode::OK);

    let learning_start = admit_update(
        &client,
        &api,
        &secret,
        fence,
        NormalizedUpdate {
            update_id: 900002,
            actor_id: 111,
            chat_id: 111,
            chat_type: "private".into(),
            kind: "command".into(),
            command: "learn".into(),
            arguments: vec![],
            text: None,
        },
    )
    .await
    .unwrap();
    assert_eq!(learning_start.status, "accepted");

    let goal_reply = admit_update(
        &client,
        &api,
        &secret,
        fence,
        NormalizedUpdate {
            update_id: 900003,
            actor_id: 111,
            chat_id: 111,
            chat_type: "private".into(),
            kind: "text".into(),
            command: String::new(),
            arguments: vec![],
            text: Some("Learn Python for automation".into()),
        },
    )
    .await
    .unwrap();
    assert_eq!(goal_reply.status, "completed");
}
