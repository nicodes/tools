//! Conservative serving-pool policy; products own their driver, queries and schemas.
use std::{collections::BTreeMap, fmt, future::Future, sync::LazyLock, time::Duration};
use tokio::time::{Instant, Timeout};

pub const MAX_CONNECTIONS: i32 = 4;
pub const QUERY_TIMEOUT: Duration = Duration::from_secs(15);
pub const LOCK_TIMEOUT: Duration = Duration::from_secs(3);
pub const MAX_COLLECTION_ROWS: usize = 1000;
pub const MAX_RESULT_BYTES: usize = 4 << 20;
pub const MAX_INPUT_BYTES: usize = 1 << 20;

/// Driver-independent serving settings. Apply all configured values when constructing
/// the driver pool; migration and offline maintenance credentials are outside this policy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServingConfig {
    pub max_connections: i32,
    pub min_connections: i32,
    pub min_idle_connections: i32,
    pub connect_timeout: Duration,
    pub runtime_params: BTreeMap<String, String>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidConfiguration;
impl fmt::Display for InvalidConfiguration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("invalid serving database configuration or timeout")
    }
}
impl std::error::Error for InvalidConfiguration {}

/// Preserve every tighter supported setting. A refusal leaves the input unchanged.
pub fn configure(config: &mut ServingConfig) -> Result<(), InvalidConfiguration> {
    if config.max_connections <= 0 {
        return Err(InvalidConfiguration);
    }
    let mut timeouts = BTreeMap::new();
    for (name, ceiling) in [
        ("statement_timeout", QUERY_TIMEOUT),
        ("lock_timeout", LOCK_TIMEOUT),
    ] {
        let raw = config.runtime_params.get(name).map_or("", |s| s.trim());
        timeouts.insert(
            name.to_owned(),
            timeout_milliseconds(raw, ceiling)?.to_string(),
        );
    }
    config.max_connections = config.max_connections.min(MAX_CONNECTIONS);
    config.min_connections = config.min_connections.min(config.max_connections);
    config.min_idle_connections = config.min_idle_connections.min(config.max_connections);
    if config.connect_timeout.is_zero() || config.connect_timeout > QUERY_TIMEOUT {
        config.connect_timeout = QUERY_TIMEOUT;
    }
    config.runtime_params.extend(timeouts);
    Ok(())
}
static POSTGRES_DURATION: LazyLock<regex::Regex> = LazyLock::new(|| {
    regex::Regex::new(r"^([0-9]+(?:\.[0-9]+)?)[ \t\n\f\r]*(ms|s|min|h|d)?$")
        .expect("constant PostgreSQL duration grammar")
});
pub fn timeout_milliseconds(raw: &str, ceiling: Duration) -> Result<i64, InvalidConfiguration> {
    let ceiling: i64 = ceiling
        .as_millis()
        .try_into()
        .map_err(|_| InvalidConfiguration)?;
    if ceiling < 1 {
        return Err(InvalidConfiguration);
    }
    if raw.is_empty() {
        return Ok(ceiling);
    }
    let parts = POSTGRES_DURATION
        .captures(raw)
        .ok_or(InvalidConfiguration)?;
    let value: f64 = parts[1].parse().map_err(|_| InvalidConfiguration)?;
    if !value.is_finite() {
        return Err(InvalidConfiguration);
    }
    let unit = match parts.get(2).map_or("", |s| s.as_str()) {
        "" | "ms" => 1.0,
        "s" => 1000.0,
        "min" => 60_000.0,
        "h" => 3_600_000.0,
        "d" => 86_400_000.0,
        _ => unreachable!(),
    };
    let milliseconds = value * unit;
    if milliseconds == 0.0 || milliseconds > ceiling as f64 {
        return Ok(ceiling);
    }
    if milliseconds < 1.0 {
        return Err(InvalidConfiguration);
    }
    Ok(milliseconds as i64)
}
/// Covers the entire serving operation, including pool acquisition, while retaining
/// a tighter parent deadline. Dropping the timed-out future cancels its awaiting work.
#[derive(Clone, Copy, Debug)]
pub struct OperationDeadline(Instant);
impl OperationDeadline {
    pub fn new(parent: Option<Instant>) -> Self {
        let own = Instant::now() + QUERY_TIMEOUT;
        Self(parent.map_or(own, |at| at.min(own)))
    }
    pub fn at(self) -> Instant {
        self.0
    }
    pub fn run<F: Future>(self, operation: F) -> Timeout<F> {
        tokio::time::timeout_at(self.0, operation)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tighter_dsn_settings_survive_and_invalid_timeouts_are_atomic() {
        let mut config = ServingConfig {
            max_connections: 2,
            min_connections: 1,
            min_idle_connections: 1,
            connect_timeout: Duration::from_secs(1),
            runtime_params: BTreeMap::from([
                ("statement_timeout".into(), "0.5s".into()),
                ("lock_timeout".into(), "250ms".into()),
            ]),
        };
        configure(&mut config).unwrap();
        assert_eq!(config.max_connections, 2);
        assert_eq!(config.connect_timeout, Duration::from_secs(1));
        assert_eq!(config.runtime_params["statement_timeout"], "500");
        assert_eq!(config.runtime_params["lock_timeout"], "250");
        config.max_connections = 100;
        config
            .runtime_params
            .insert("lock_timeout".into(), "0.1ms".into());
        let before = config.clone();
        assert!(configure(&mut config).is_err());
        assert_eq!(config, before);
        config
            .runtime_params
            .insert("statement_timeout".into(), "2min".into());
        config
            .runtime_params
            .insert("lock_timeout".into(), "0".into());
        config.min_connections = 90;
        config.min_idle_connections = 80;
        config.connect_timeout = Duration::ZERO;
        configure(&mut config).unwrap();
        assert_eq!(
            (
                config.max_connections,
                config.min_connections,
                config.min_idle_connections
            ),
            (4, 4, 4)
        );
        assert_eq!(config.connect_timeout, QUERY_TIMEOUT);
        assert_eq!(config.runtime_params["statement_timeout"], "15000");
        assert_eq!(config.runtime_params["lock_timeout"], "3000");
    }
    #[test]
    fn postgres_duration_syntax_does_not_turn_tighter_values_into_unlimited_queries() {
        for raw in [
            "-1", "NaN", "Infinity", "0.001ms", "1e9", "1sec", ".5s", "1\u{a0}s",
        ] {
            assert!(
                timeout_milliseconds(raw, Duration::from_secs(1)).is_err(),
                "{raw}"
            );
        }
        for (raw, want) in [
            ("", 1000),
            ("0", 1000),
            ("2s", 1000),
            ("0.25s", 250),
            ("1ms", 1),
            ("0.001s", 1),
        ] {
            assert_eq!(
                timeout_milliseconds(raw, Duration::from_secs(1)).unwrap(),
                want
            );
        }
        assert!(timeout_milliseconds("1", Duration::from_nanos(1)).is_err());
    }
    #[tokio::test]
    async fn whole_operation_keeps_a_tighter_parent_deadline() {
        let parent = Instant::now() + Duration::from_millis(10);
        let child = OperationDeadline::new(Some(parent));
        assert_eq!(child.at(), parent);
        assert!(child.run(std::future::pending::<()>()).await.is_err());
    }
}
