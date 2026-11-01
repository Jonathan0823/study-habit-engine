#[tokio::main]
async fn main() {
    if let Err(error) = study_habit_engine_bot::run().await {
        eprintln!("bot stopped: {error}");
        std::process::exit(1);
    }
}
