use serde_json::Value;
use sessionauth::*;
use std::time::{Duration, UNIX_EPOCH};
#[test]
fn signed_security_and_organization_claims_match_independent_go_results() {
    let fixtures: Value =
        serde_json::from_str(include_str!("fixtures/go-verification.json")).unwrap();
    let key = serde_json::from_value(fixtures["jwk"].clone()).unwrap();
    let handler = |party: &str| party == "https://app.example.com";
    assert_eq!(fixtures["cases"].as_array().unwrap().len(), 17);
    for case in fixtures["cases"].as_array().unwrap() {
        let mut params = VerifyParams::new(case["token"].as_str().unwrap(), &key, &handler);
        params.now = UNIX_EPOCH + Duration::from_secs(fixtures["clock_unix"].as_u64().unwrap());
        params.leeway = Duration::from_secs(5);
        params.is_satellite = case["satellite"].as_bool().unwrap();
        params.proxy_url = case["proxy"].as_str();
        let result = verify(&params);
        assert_eq!(
            result.is_ok(),
            case["valid"].as_bool().unwrap(),
            "{}",
            case["name"]
        );
        if let Ok(claims) = result {
            assert_eq!(claims.subject, case["subject"]);
            assert_eq!(claims.version, case["version"]);
            assert_eq!(claims.organization_id, case["organization_id"]);
            assert_eq!(claims.organization_slug, case["organization_slug"]);
            assert_eq!(claims.organization_role, case["organization_role"]);
            let permissions: Vec<String> = case["organization_permissions"]
                .as_array()
                .map(|v| v.iter().map(|v| v.as_str().unwrap().to_owned()).collect())
                .unwrap_or_default();
            assert_eq!(claims.organization_permissions, permissions);
        }
        params.leeway = Duration::from_secs(6);
        assert!(verify(&params).is_err());
        params.leeway = Duration::ZERO;
        params.token = "";
        assert!(verify(&params).is_err());
    }
}
