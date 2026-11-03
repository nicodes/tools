//! Local Clerk session verification. Callers own issuer pinning, origin policy,
//! bounded key retrieval and error mapping; this crate performs no network I/O.
use jsonwebtoken::{DecodingKey, Validation, decode, decode_header, jwk::Jwk};
use serde_json::Value;
use std::{
    fmt,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidSession;
impl fmt::Display for InvalidSession {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("missing or invalid session claims")
    }
}
impl std::error::Error for InvalidSession {}
pub struct VerifyParams<'a> {
    pub token: &'a str,
    pub jwk: &'a Jwk,
    pub authorized_party: &'a dyn Fn(&str) -> bool,
    pub now: SystemTime,
    pub leeway: Duration,
    pub is_satellite: bool,
    pub proxy_url: Option<&'a str>,
}
impl<'a> VerifyParams<'a> {
    pub fn new(token: &'a str, jwk: &'a Jwk, authorized_party: &'a dyn Fn(&str) -> bool) -> Self {
        Self {
            token,
            jwk,
            authorized_party,
            now: SystemTime::now(),
            leeway: Duration::ZERO,
            is_satellite: false,
            proxy_url: None,
        }
    }
}
#[derive(Clone, Debug)]
pub struct SessionClaims {
    pub subject: String,
    pub expiry: i64,
    pub issuer: String,
    pub authorized_party: String,
    pub version: i64,
    pub organization_id: String,
    pub organization_slug: String,
    pub organization_role: String,
    pub organization_permissions: Vec<String>,
    /// Signature-verified claims, including product-specific fields.
    pub raw: Value,
}
fn text<'a>(claims: &'a Value, name: &str) -> Result<&'a str, InvalidSession> {
    match &claims[name] {
        Value::Null => Ok(""),
        Value::String(s) => Ok(s),
        _ => Err(InvalidSession),
    }
}
fn timestamp(claims: &Value, name: &str) -> Result<Option<i64>, InvalidSession> {
    match &claims[name] {
        Value::Null => Ok(None),
        Value::Number(n) => {
            let seconds = n.as_f64().ok_or(InvalidSession)?;
            if !seconds.is_finite() || seconds < i64::MIN as f64 || seconds >= i64::MAX as f64 {
                return Err(InvalidSession);
            }
            Ok(Some(seconds.trunc() as i64))
        }
        _ => Err(InvalidSession),
    }
}
fn date(seconds: i64) -> Result<SystemTime, InvalidSession> {
    if seconds >= 0 {
        UNIX_EPOCH.checked_add(Duration::from_secs(seconds as u64))
    } else {
        UNIX_EPOCH.checked_sub(Duration::from_secs(seconds.unsigned_abs()))
    }
    .ok_or(InvalidSession)
}
pub fn verify(params: &VerifyParams<'_>) -> Result<SessionClaims, InvalidSession> {
    if params.token.is_empty()
        || params.token.len() > 16384
        || params.leeway > Duration::from_secs(5)
    {
        return Err(InvalidSession);
    }
    let algorithm = params
        .jwk
        .common
        .key_algorithm
        .as_ref()
        .ok_or(InvalidSession)?
        .to_string()
        .parse::<jsonwebtoken::Algorithm>()
        .map_err(|_| InvalidSession)?;
    if decode_header(params.token).map_err(|_| InvalidSession)?.alg != algorithm {
        return Err(InvalidSession);
    }
    let key = DecodingKey::from_jwk(params.jwk).map_err(|_| InvalidSession)?;
    let mut validation = Validation::new(algorithm);
    validation.required_spec_claims.clear();
    // Validate all three dates against the caller's clock below; signature and
    // algorithm verification remain enabled in jsonwebtoken.
    validation.validate_exp = false;
    validation.validate_nbf = false;
    validation.validate_aud = false;
    let raw = decode::<Value>(params.token, &key, &validation)
        .map_err(|_| InvalidSession)?
        .claims;
    if !raw.is_object() {
        return Err(InvalidSession);
    }
    let subject = text(&raw, "sub")?;
    let expiry = timestamp(&raw, "exp")?.ok_or(InvalidSession)?;
    let earliest = params
        .now
        .checked_sub(params.leeway)
        .ok_or(InvalidSession)?;
    let latest = params
        .now
        .checked_add(params.leeway)
        .ok_or(InvalidSession)?;
    if subject.is_empty() || earliest > date(expiry)? {
        return Err(InvalidSession);
    }
    for claim in ["nbf", "iat"] {
        if let Some(seconds) = timestamp(&raw, claim)?
            && latest < date(seconds)?
        {
            return Err(InvalidSession);
        }
    }
    let issuer = text(&raw, "iss")?;
    if !params.is_satellite {
        let valid = params.proxy_url.map_or_else(
            || issuer.starts_with("https://clerk.") || issuer.contains(".clerk.accounts"),
            |proxy| issuer == proxy,
        );
        if !valid {
            return Err(InvalidSession);
        }
    }
    let party = text(&raw, "azp")?;
    if !(params.authorized_party)(party) {
        return Err(InvalidSession);
    }
    let mut claims = SessionClaims {
        subject: subject.into(),
        expiry,
        issuer: issuer.into(),
        authorized_party: party.into(),
        version: 1,
        organization_id: text(&raw, "org_id")?.into(),
        organization_slug: text(&raw, "org_slug")?.into(),
        organization_role: text(&raw, "org_role")?.into(),
        organization_permissions: match &raw["org_permissions"] {
            Value::Null => Vec::new(),
            Value::Array(values) => values
                .iter()
                .map(|v| v.as_str().map(str::to_owned).ok_or(InvalidSession))
                .collect::<Result<_, _>>()?,
            _ => return Err(InvalidSession),
        },
        raw: Value::Null,
    };
    if timestamp(&raw, "v")? == Some(2) && !text(&raw["o"], "id")?.is_empty() {
        let organization = &raw["o"];
        claims.version = 2;
        claims.organization_id = text(organization, "id")?.into();
        claims.organization_slug = text(organization, "slg")?.into();
        claims.organization_role = format!("org:{}", text(organization, "rol")?);
        claims.organization_permissions.clear();
        let permissions: Vec<_> = text(organization, "per")?.split(',').collect();
        let mappings: Vec<_> = text(organization, "fpm")?.split(',').collect();
        for (i, feature) in text(&raw, "fea")?.split(',').enumerate() {
            let scope: Vec<_> = feature.split(':').collect();
            if scope.len() < 2 || !scope[0].contains('o') {
                continue;
            }
            let Some(mapping) = mappings.get(i).and_then(|m| m.parse::<i64>().ok()) else {
                continue;
            };
            if mapping <= 0 {
                continue;
            }
            let bits = (64 - mapping.leading_zeros()) as usize;
            if bits > permissions.len() {
                continue;
            }
            for (j, permission) in permissions.iter().take(bits).enumerate() {
                if mapping & (1_i64 << j) != 0 {
                    claims
                        .organization_permissions
                        .push(format!("org:{}:{permission}", scope[1]));
                }
            }
        }
    }
    claims.raw = raw;
    Ok(claims)
}
