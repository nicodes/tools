//! Bounded HTTP errors, request correlation and redacted transport diagnostics.
//! Install the middleware inside any response-compression layer. Domain success
//! schemas, authentication, authorization and business error codes remain owned
//! by the application. Pre-signed RPC payloads retain their own wire contract;
//! apply this layer to application HTTP routes before signing/compression.
use axum::{
    body::{Body, to_bytes},
    extract::{MatchedPath, Request},
    http::{HeaderValue, Method, StatusCode, header},
    middleware::Next,
    response::Response,
};
use serde_json::{Value, json};
use std::time::{Duration, Instant};
use tracing::Instrument;

const ERROR_BYTES: usize = 64 * 1024;
const MESSAGE_BYTES: usize = 1024;

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

fn code(value: Option<&Value>) -> Option<&str> {
    value.and_then(Value::as_str).filter(|text| {
        !text.is_empty()
            && text.len() <= 64
            && text
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
    })
}

fn message(text: &str) -> String {
    let mut end = text.len().min(MESSAGE_BYTES);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

fn envelope(value: Value, status: StatusCode, request_id: &str) -> Value {
    let mut root = value.as_object().cloned().unwrap_or_default();
    let previous = root.remove("error").unwrap_or(Value::Null);
    let mut error = previous.as_object().cloned().unwrap_or_default();
    let structured =
        code(error.get("code")).is_some() && error.get("message").and_then(Value::as_str).is_some();
    let selected_code = code(error.get("code"))
        .or_else(|| code(Some(&previous)))
        .or_else(|| code(root.get("code")))
        .unwrap_or_else(|| default_code(status))
        .to_owned();
    let canonical = status.canonical_reason().unwrap_or("Request failed");
    let selected_message = if status.is_server_error() && !structured {
        canonical
    } else {
        error
            .get("message")
            .and_then(Value::as_str)
            .or_else(|| root.get("message").and_then(Value::as_str))
            .or_else(|| root.get("error_message").and_then(Value::as_str))
            .or_else(|| {
                previous
                    .as_str()
                    .filter(|_| code(Some(&previous)).is_none())
            })
            .unwrap_or(canonical)
    };
    let selected_message = message(selected_message);
    for name in ["message", "error_message", "code", "request_id"] {
        root.remove(name);
    }
    error.insert("code".into(), Value::String(selected_code));
    error.insert("message".into(), Value::String(selected_message));
    error.insert("request_id".into(), Value::String(request_id.to_owned()));
    root.insert("error".into(), Value::Object(error));
    Value::Object(root)
}

async fn normalize(response: Response, request_id: &str, head: bool) -> Response {
    let (mut parts, body) = response.into_parts();
    let value =
        match tokio::time::timeout(Duration::from_secs(2), to_bytes(body, ERROR_BYTES)).await {
            Ok(Ok(bytes)) => serde_json::from_slice(&bytes).unwrap_or(Value::Null),
            _ => Value::Null,
        };
    let value = envelope(value, parts.status, request_id);
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
    Response::from_parts(parts, body)
}

/// Give every response one server-generated correlation ID. Error-body reads are
/// bounded in time and bytes; successful and streaming payloads are untouched.
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
        response = normalize(response, &request_id, method == Method::HEAD).await;
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
    use axum::response::IntoResponse;
    let value = Value::String(error_code.to_owned());
    let error_code = code(Some(&value)).unwrap_or_else(|| default_code(status));
    (
        status,
        axum::Json(json!({"error":{"code":error_code,"message":message(description)}})),
    )
        .into_response()
}

pub fn health(status: &str, revision: &str) -> axum::Json<Value> {
    axum::Json(json!({"status":status,"revision":revision}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Router, middleware, routing::get};
    use tower::ServiceExt;

    async fn body(response: Response) -> Value {
        serde_json::from_slice(&to_bytes(response.into_body(), ERROR_BYTES).await.unwrap()).unwrap()
    }

    #[tokio::test]
    async fn correlates_framework_and_structured_errors_without_trusting_input_ids() {
        let app = Router::new().route("/domain", get(|| async {
            use axum::response::IntoResponse;
            (StatusCode::CONFLICT, axum::Json(json!({"error":{"code":"already_saved","message":"Already saved","hint":"reload"},"operation_id":"opaque"}))).into_response()
        })).layer(middleware::from_fn(observe));
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
            let value = body(response).await;
            assert_eq!(value["error"]["request_id"], id);
            if path == "/domain" {
                assert_eq!(value["error"]["code"], "already_saved");
                assert_eq!(value["error"]["hint"], "reload");
                assert_eq!(value["operation_id"], "opaque");
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

    #[test]
    fn normalizes_legacy_fields_and_hides_unstructured_server_diagnostics() {
        let value = envelope(
            json!({"message":"Safe input hint","success":false}),
            StatusCode::BAD_REQUEST,
            "id",
        );
        assert_eq!(value["error"]["message"], "Safe input hint");
        assert_eq!(value["success"], false);
        assert!(value.get("message").is_none());
        let value = envelope(
            json!({"error_message":"private database detail"}),
            StatusCode::INTERNAL_SERVER_ERROR,
            "id",
        );
        assert_eq!(value["error"]["message"], "Internal Server Error");
        assert!(!value.to_string().contains("private database"));
    }

    #[test]
    fn bounds_unicode_messages_and_rejects_invalid_machine_codes() {
        let value = envelope(
            json!({"error":{"code":"not a machine code","message":"é".repeat(800)}}),
            StatusCode::BAD_REQUEST,
            "id",
        );
        assert_eq!(value["error"]["code"], "invalid_input");
        assert_eq!(
            value["error"]["message"].as_str().unwrap().len(),
            MESSAGE_BYTES
        );
    }

    #[tokio::test(start_paused = true)]
    async fn bounds_error_body_time_and_bytes() {
        let pending = Body::from_stream(futures_util::stream::pending::<
            Result<axum::body::Bytes, std::io::Error>,
        >());
        let response = Response::builder().status(503).body(pending).unwrap();
        assert_eq!(
            body(normalize(response, "id", false).await).await["error"]["code"],
            "service_unavailable"
        );
        let response = Response::builder()
            .status(400)
            .body(Body::from(vec![b'x'; ERROR_BYTES + 1]))
            .unwrap();
        assert_eq!(
            body(normalize(response, "id", false).await).await["error"]["code"],
            "invalid_input"
        );
    }

    #[tokio::test]
    async fn head_error_has_headers_and_no_body() {
        let response = normalize(
            Response::builder()
                .status(404)
                .header(header::CONTENT_LENGTH, "999")
                .body(Body::from("missing"))
                .unwrap(),
            "id",
            true,
        )
        .await;
        assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
        assert!(!response.headers().contains_key(header::CONTENT_LENGTH));
        assert!(
            to_bytes(response.into_body(), 1024)
                .await
                .unwrap()
                .is_empty()
        );
    }
}
