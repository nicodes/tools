//! Typed HTTP errors, request correlation and redacted transport diagnostics.
//! Install `observe` inside any response-compression layer. Applications create
//! failures with this module's factories; the middleware completes their JSON
//! body with the server-generated request ID. It never reads an error body.
//! Domain success schemas, authorization and business codes remain app-owned.
//! Private component RPC and signed payloads keep their own domain contracts;
//! install this layer on the application's public HTTP routes.
use axum::{
    body::Body,
    extract::{MatchedPath, Request},
    http::{HeaderValue, Method, StatusCode, header},
    middleware::Next,
    response::Response,
};
use serde_json::{Value, json};
use std::{
    pin::Pin,
    task::{Context, Poll},
    time::Instant,
};
use tracing::Instrument;

const MESSAGE_BYTES: usize = 1024;

/// Enable redacted request diagnostics on stderr without changing machine stdout
/// or replacing an application-owned tracing subscriber.
pub fn init_logging() {
    use tracing_subscriber::{Layer, layer::SubscriberExt, util::SubscriberInitExt};
    let filter = tracing_subscriber::filter::Targets::new()
        .with_target("http_contract", tracing::Level::INFO)
        .with_target("httpcontract", tracing::Level::INFO);
    let _ = tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .json()
                .with_ansi(false)
                .with_writer(std::io::stderr)
                .with_filter(filter),
        )
        .try_init();
}

#[derive(Clone, Debug)]
pub struct RequestId(String);

impl RequestId {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

fn default_code(status: StatusCode) -> &'static str {
    match status.as_u16() {
        400 | 422 => "invalid_input",
        401 => "unauthenticated",
        403 => "forbidden",
        404 => "not_found",
        405 => "method_not_allowed",
        409 => "conflict",
        413 => "payload_too_large",
        415 => "unsupported_media_type",
        429 => "rate_limited",
        500 => "internal_error",
        502 => "bad_gateway",
        503 => "service_unavailable",
        504 => "deadline_exceeded",
        _ => "http_error",
    }
}

fn code(text: &str) -> Option<&str> {
    (!text.is_empty()
        && text.len() <= 64
        && text
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c)))
    .then_some(text)
}

fn message(text: &str) -> String {
    let mut end = text.len().min(MESSAGE_BYTES);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

#[derive(Clone, Debug)]
struct PublicFailure {
    code: String,
    message: String,
}

impl PublicFailure {
    fn new(status: StatusCode, error_code: &str, description: &str) -> Self {
        let description = if status.is_server_error() {
            status.canonical_reason().unwrap_or("Request failed")
        } else {
            description
        };
        Self {
            code: code(error_code)
                .unwrap_or_else(|| default_code(status))
                .to_owned(),
            message: message(description),
        }
    }

    fn into_envelope(self, request_id: &str) -> Value {
        json!({"error": {"code": self.code, "message": self.message, "request_id": request_id}})
    }
}

struct RetainedErrorBody {
    payload: Body,
    // The original body may own an admission permit or another response lease.
    _source: Body,
}
impl http_body::Body for RetainedErrorBody {
    type Data = axum::body::Bytes;
    type Error = axum::Error;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<http_body::Frame<Self::Data>, Self::Error>>> {
        Pin::new(&mut self.payload).poll_frame(cx)
    }
    fn size_hint(&self) -> http_body::SizeHint {
        self.payload.size_hint()
    }
    fn is_end_stream(&self) -> bool {
        self.payload.is_end_stream()
    }
}

fn render_failure(response: Response, request_id: &str, head: bool) -> Response {
    let (mut parts, source) = response.into_parts();
    let failure = parts
        .extensions
        .remove::<PublicFailure>()
        .unwrap_or_else(|| {
            PublicFailure::new(
                parts.status,
                default_code(parts.status),
                parts.status.canonical_reason().unwrap_or("Request failed"),
            )
        });
    let value = failure.into_envelope(request_id);
    parts.headers.remove(header::CONTENT_LENGTH);
    parts.headers.remove(header::CONTENT_ENCODING);
    parts.headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    let body = if head {
        Body::empty()
    } else {
        Body::from(serde_json::to_vec(&value).expect("JSON values serialize"))
    };
    Response::from_parts(
        parts,
        Body::new(RetainedErrorBody {
            payload: body,
            _source: source,
        }),
    )
}

/// Give every response one server-generated correlation ID. Typed failures are
/// serialized once; framework failures get safe status defaults. Arbitrary error
/// bodies are never parsed, and successful/streaming payloads stay untouched.
pub async fn observe(mut request: Request, next: Next) -> Response {
    let started = Instant::now();
    let request_id = uuid::Uuid::new_v4().simple().to_string();
    let id = HeaderValue::from_str(&request_id).expect("UUID is a header value");
    request
        .extensions_mut()
        .insert(RequestId(request_id.clone()));
    let method = request.method().clone();
    let route = request
        .extensions()
        .get::<MatchedPath>()
        .map(|value| value.as_str().to_owned())
        .unwrap_or_else(|| "unmatched".into());
    let span = tracing::info_span!("http_request", request_id = %request_id, method = %method, route = %route);
    let mut response = next.run(request).instrument(span).await;
    if response.status().is_client_error() || response.status().is_server_error() {
        response = render_failure(response, &request_id, method == Method::HEAD);
    }
    response.headers_mut().insert("x-request-id", id);
    let exposed = response
        .headers()
        .get(header::ACCESS_CONTROL_EXPOSE_HEADERS)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if !exposed
        .split(',')
        .any(|value| value.trim().eq_ignore_ascii_case("x-request-id"))
    {
        let exposed = if exposed.is_empty() {
            "x-request-id".to_owned()
        } else {
            format!("{exposed}, x-request-id")
        };
        if let Ok(value) = HeaderValue::from_str(&exposed) {
            response
                .headers_mut()
                .insert(header::ACCESS_CONTROL_EXPOSE_HEADERS, value);
        }
    }
    tracing::info!(target: "http_contract", request_id, method = %method, route,
        status = response.status().as_u16(), duration_ms = started.elapsed().as_millis() as u64);
    response
}

pub fn failure(status: StatusCode, error_code: &str, description: &str) -> Response {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = status;
    response
        .extensions_mut()
        .insert(PublicFailure::new(status, error_code, description));
    response
}

/// Create an application failure with the standard machine code for its status.
/// Its body is completed by `observe`, alongside the request correlation ID.
pub fn failure_for_status(status: StatusCode, description: &str) -> Response {
    failure(status, default_code(status), description)
}

pub fn health(status: &str, revision: &str) -> axum::Json<Value> {
    axum::Json(json!({"status":status,"revision":revision}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Router, body::to_bytes, middleware, routing::get};
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };
    use tower::ServiceExt;

    const ERROR_BYTES: usize = 64 * 1024;

    struct LeasedBody {
        body: Body,
        released: Arc<AtomicBool>,
    }
    impl Drop for LeasedBody {
        fn drop(&mut self) {
            self.released.store(true, Ordering::SeqCst);
        }
    }
    impl axum::body::HttpBody for LeasedBody {
        type Data = axum::body::Bytes;
        type Error = axum::Error;
        fn poll_frame(
            mut self: Pin<&mut Self>,
            cx: &mut Context<'_>,
        ) -> Poll<Option<Result<http_body::Frame<Self::Data>, Self::Error>>> {
            Pin::new(&mut self.body).poll_frame(cx)
        }
    }

    async fn body(response: Response) -> Value {
        serde_json::from_slice(&to_bytes(response.into_body(), ERROR_BYTES).await.unwrap()).unwrap()
    }

    #[tokio::test]
    async fn retains_response_lease_until_payload_consumed_or_dropped() {
        for consume in [false, true] {
            let released = Arc::new(AtomicBool::new(false));
            let source = Body::new(LeasedBody {
                body: Body::from("not inspected"),
                released: released.clone(),
            });
            let response = render_failure(
                Response::builder().status(400).body(source).unwrap(),
                "id",
                false,
            );
            assert!(!released.load(Ordering::SeqCst));
            if consume {
                let _ = body(response).await;
            } else {
                drop(response);
            }
            assert!(released.load(Ordering::SeqCst));
        }
    }

    #[tokio::test]
    async fn correlates_typed_and_framework_failures_without_trusting_input_ids() {
        let app = Router::new()
            .route(
                "/domain",
                get(|| async {
                    let mut response = failure(
                        StatusCode::TOO_MANY_REQUESTS,
                        "daily_notes_exhausted",
                        "Daily limit reached",
                    );
                    response
                        .headers_mut()
                        .insert("retry-after", HeaderValue::from_static("5"));
                    response
                }),
            )
            .layer(middleware::from_fn(observe));
        for path in ["/domain", "/missing"] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .uri(path)
                        .header("x-request-id", "untrusted")
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            let id = response.headers()["x-request-id"]
                .to_str()
                .unwrap()
                .to_owned();
            assert_eq!(id.len(), 32);
            assert_ne!(id, "untrusted");
            if path == "/domain" {
                assert_eq!(response.headers()["retry-after"], "5");
            }
            let value = body(response).await;
            assert_eq!(value.as_object().unwrap().len(), 1);
            assert_eq!(value["error"].as_object().unwrap().len(), 3);
            assert_eq!(value["error"]["request_id"], id);
            if path == "/domain" {
                assert_eq!(value["error"]["code"], "daily_notes_exhausted");
                assert_eq!(value["error"]["message"], "Daily limit reached");
            } else {
                assert_eq!(value["error"]["code"], "not_found");
            }
        }
    }

    #[tokio::test]
    async fn preserves_success_bytes_and_existing_exposed_headers() {
        let app = Router::new()
            .route(
                "/",
                get(|| async {
                    Response::builder()
                        .header(header::ACCESS_CONTROL_EXPOSE_HEADERS, "x-app-version")
                        .body(Body::from(vec![0_u8, 1, 255]))
                        .unwrap()
                }),
            )
            .layer(middleware::from_fn(observe));
        let response = app.oneshot(Request::new(Body::empty())).await.unwrap();
        assert_eq!(
            response.headers()[header::ACCESS_CONTROL_EXPOSE_HEADERS],
            "x-app-version, x-request-id"
        );
        assert_eq!(
            &to_bytes(response.into_body(), 1024).await.unwrap()[..],
            &[0, 1, 255]
        );
    }

    #[tokio::test]
    async fn does_not_accept_retired_or_arbitrary_json_body_contracts() {
        for value in [
            json!({"message":"old input hint","success":false}),
            json!({"error_message":"old input hint"}),
            json!({"error":"old_code"}),
            json!({"error":{"code":"old_code","message":"old input hint","request_id":"spoofed"}}),
        ] {
            let response = Response::builder()
                .status(400)
                .body(Body::from(value.to_string()))
                .unwrap();
            let result = body(render_failure(response, "server-id", false)).await;
            assert_eq!(
                result,
                json!({"error":{"code":"invalid_input","message":"Bad Request","request_id":"server-id"}})
            );
        }
    }

    #[tokio::test]
    async fn bounds_unicode_messages_and_rejects_invalid_machine_codes() {
        let response = failure(
            StatusCode::BAD_REQUEST,
            "not a machine code",
            &"é".repeat(800),
        );
        let value = body(render_failure(response, "id", false)).await;
        assert_eq!(value["error"]["code"], "invalid_input");
        assert_eq!(
            value["error"]["message"].as_str().unwrap().len(),
            MESSAGE_BYTES
        );
    }

    #[tokio::test]
    async fn redacts_server_diagnostics_even_for_typed_failures() {
        let response = failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "daily_operation_pending",
            "private database detail",
        );
        let value = body(render_failure(response, "id", false)).await;
        assert_eq!(value["error"]["code"], "daily_operation_pending");
        assert_eq!(value["error"]["message"], "Service Unavailable");
        assert!(!value.to_string().contains("private database"));
    }

    #[tokio::test]
    async fn never_polls_error_bodies_or_waits_for_their_streams() {
        let polled = Arc::new(AtomicBool::new(false));
        let marker = polled.clone();
        let source = Body::from_stream(futures_util::stream::poll_fn(move |_| {
            marker.store(true, Ordering::SeqCst);
            Poll::<Option<Result<axum::body::Bytes, std::io::Error>>>::Pending
        }));
        let response = Response::builder().status(503).body(source).unwrap();
        let value = body(render_failure(response, "id", false)).await;
        assert_eq!(value["error"]["code"], "service_unavailable");
        assert!(!polled.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn head_error_has_headers_and_no_body() {
        let response = render_failure(
            Response::builder()
                .status(404)
                .header(header::CONTENT_LENGTH, "999")
                .header(header::CONTENT_ENCODING, "gzip")
                .body(Body::from("missing"))
                .unwrap(),
            "id",
            true,
        );
        assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
        assert!(!response.headers().contains_key(header::CONTENT_LENGTH));
        assert!(!response.headers().contains_key(header::CONTENT_ENCODING));
        assert!(
            to_bytes(response.into_body(), 1024)
                .await
                .unwrap()
                .is_empty()
        );
    }
}
