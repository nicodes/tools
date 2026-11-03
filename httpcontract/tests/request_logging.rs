//! Keep dispatcher capture separate from concurrent no-subscriber request tests.
use axum::{Router, body::Body, extract::Request, middleware, routing::get};
use httpcontract::observe;
use serde_json::Value;
use tower::ServiceExt;

#[derive(Clone, Default)]
struct Capture(std::sync::Arc<std::sync::Mutex<Vec<u8>>>);
impl std::io::Write for Capture {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Capture {
    type Writer = Capture;
    fn make_writer(&'a self) -> Capture {
        self.clone()
    }
}

#[tokio::test]
async fn request_logs_use_route_patterns_and_the_response_correlation_id() {
    use tracing::instrument::WithSubscriber;
    let capture = Capture::default();
    let subscriber = tracing_subscriber::fmt()
        .json()
        .with_ansi(false)
        .with_writer(capture.clone())
        .finish();
    let routes = Router::new()
        .route("/private/{id}", get(|| async { "ok" }))
        .layer(middleware::from_fn(observe));
    let response = routes
        .oneshot(
            Request::builder()
                .uri("/private/private-user?token=private-query")
                .header("authorization", "Bearer private-credential")
                .body(Body::empty())
                .unwrap(),
        )
        .with_subscriber(subscriber)
        .await
        .unwrap();
    let logs = String::from_utf8(capture.0.lock().unwrap().clone()).unwrap();
    let record: Value = serde_json::from_str(logs.lines().last().unwrap()).unwrap();
    assert_eq!(record["fields"]["route"], "/private/{id}");
    assert_eq!(
        record["fields"]["request_id"],
        response.headers()["x-request-id"].to_str().unwrap()
    );
    assert!(!logs.contains("private-user"));
    assert!(!logs.contains("private-query"));
    assert!(!logs.contains("private-credential"));
}
