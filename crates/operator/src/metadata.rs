//! Decode bounded dependency metadata without YAML aliases, tags or ambiguous keys.
use serde_json::{Map, Number, Value};
use yaml_rust2::{
    parser::{Event, Parser},
    scanner::TScalarStyle,
};

type Result<T> = std::result::Result<T, String>;
fn invalid() -> String {
    "invalid or unsupported dependency metadata YAML".into()
}
fn scalar(value: String, style: TScalarStyle) -> Result<(Value, bool)> {
    if style != TScalarStyle::Plain {
        return Ok((Value::String(value), true));
    }
    match value.as_str() {
        "" | "~" | "null" | "Null" | "NULL" => return Ok((Value::Null, false)),
        "true" | "True" | "TRUE" => return Ok((Value::Bool(true), false)),
        "false" | "False" | "FALSE" => return Ok((Value::Bool(false), false)),
        _ => {}
    }
    let plain = value.trim_start_matches(['+', '-']).as_bytes();
    if plain.get(..10).is_some_and(|date| {
        date[4] == b'-'
            && date[7] == b'-'
            && date[..4].iter().all(u8::is_ascii_digit)
            && date[5..7].iter().all(u8::is_ascii_digit)
            && date[8..10].iter().all(u8::is_ascii_digit)
    }) && plain
        .get(10)
        .is_none_or(|next| matches!(next, b'T' | b't' | b' '))
    {
        return Err("implicit dates are unsupported in dependency metadata; quote date and timestamp strings".into());
    }
    if value
        .as_bytes()
        .first()
        .is_some_and(|b| b.is_ascii_digit() || matches!(b, b'+' | b'-' | b'.'))
    {
        let clean = value.replace('_', "");
        let (negative, digits) = if let Some(s) = clean.strip_prefix('-') {
            (true, s)
        } else {
            (false, clean.strip_prefix('+').unwrap_or(&clean))
        };
        let (radix, digits) = if let Some(s) = digits.strip_prefix("0x") {
            (16, s)
        } else if let Some(s) = digits.strip_prefix("0o") {
            (8, s)
        } else if let Some(s) = digits.strip_prefix("0b") {
            (2, s)
        } else if digits.len() > 1 && digits.starts_with('0') {
            (8, &digits[1..])
        } else {
            (10, digits)
        };
        if let Ok(magnitude) = u64::from_str_radix(digits, radix) {
            if negative {
                if magnitude <= i64::MAX as u64 {
                    return Ok((Value::from(-(magnitude as i64)), false));
                }
                if magnitude == (i64::MAX as u64) + 1 {
                    return Ok((Value::from(i64::MIN), false));
                }
            } else {
                return Ok((Value::from(magnitude), false));
            }
        }
        if [".nan", ".inf", "+.inf", "-.inf"].contains(&clean.to_ascii_lowercase().as_str()) {
            return Err(invalid());
        }
        if let Ok(number) = clean.parse::<f64>() {
            return Number::from_f64(number)
                .map(|n| (Value::Number(n), false))
                .ok_or_else(invalid);
        }
    }
    Ok((Value::String(value), true))
}
fn next(parser: &mut Parser<std::str::Chars<'_>>) -> Result<Event> {
    parser
        .next_token()
        .map(|(event, _)| event)
        .map_err(|_| invalid())
}
fn value(
    parser: &mut Parser<std::str::Chars<'_>>,
    event: Event,
    depth: usize,
    nodes: &mut usize,
) -> Result<(Value, bool)> {
    *nodes += 1;
    if depth > 16 || *nodes > 4096 {
        return Err(invalid());
    }
    match event {
        Event::Scalar(text, style, 0, None) => scalar(text, style),
        Event::SequenceStart(0, None) => {
            let mut values = Vec::new();
            loop {
                let event = next(parser)?;
                if event == Event::SequenceEnd {
                    break;
                }
                values.push(value(parser, event, depth + 1, nodes)?.0);
            }
            Ok((Value::Array(values), false))
        }
        Event::MappingStart(0, None) => {
            let mut values = Map::new();
            loop {
                let event = next(parser)?;
                if event == Event::MappingEnd {
                    break;
                }
                let (key, is_string) = value(parser, event, depth + 1, nodes)?;
                let key = key.as_str().filter(|_| is_string).ok_or_else(invalid)?;
                if key == "<<" || values.contains_key(key) {
                    return Err("duplicate or invalid dependency metadata key".into());
                }
                let event = next(parser)?;
                values.insert(key.into(), value(parser, event, depth + 1, nodes)?.0);
            }
            Ok((Value::Object(values), false))
        }
        _ => Err(invalid()),
    }
}
pub fn metadata(body: &[u8]) -> Result<Value> {
    if body.is_empty() || body.len() > 65536 {
        return Err("dependency metadata must contain at most 64 KiB".into());
    }
    let source = std::str::from_utf8(body).map_err(|_| invalid())?;
    let mut parser = Parser::new_from_str(source);
    if next(&mut parser)? != Event::StreamStart || next(&mut parser)? != Event::DocumentStart {
        return Err(invalid());
    }
    let event = next(&mut parser)?;
    let parsed = value(&mut parser, event, 1, &mut 1)?.0;
    if next(&mut parser)? != Event::DocumentEnd || next(&mut parser)? != Event::StreamEnd {
        return Err("dependency metadata must contain one document".into());
    }
    let fields = parsed.as_object().ok_or_else(invalid)?;
    if fields.len() != 1
        || !fields
            .get("updated-dependencies")
            .is_some_and(Value::is_array)
    {
        return Err("unexpected dependency metadata fields or updates are not an array".into());
    }
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn implicit_dates_refuse_in_keys_and_values_while_quoted_dates_stay_literal() {
        for date in [
            "2026-10-31",
            "2026-10-31T12:30:00Z",
            "2026-10-31t12:30:00.123+02:00",
            "2026-10-31 12:30:00+02:00",
        ] {
            for source in [
                format!("updated-dependencies: [{{{date}: example}}]"),
                format!("updated-dependencies: [{{dependency-version: {date}}}]"),
            ] {
                assert!(
                    metadata(source.as_bytes())
                        .unwrap_err()
                        .contains("quote date"),
                    "{source}"
                );
            }
            for quote in ['\'', '"'] {
                let source = format!(
                    "updated-dependencies: [{{{quote}{date}{quote}: {quote}{date}{quote}}}]"
                );
                assert_eq!(
                    metadata(source.as_bytes()).unwrap()["updated-dependencies"][0][date],
                    date
                );
            }
        }
        assert_eq!(
            metadata(b"updated-dependencies: [true, null, 0x10, 1.5, example, 2026-10-31-release]")
                .unwrap()["updated-dependencies"],
            serde_json::json!([true, null, 16, 1.5, "example", "2026-10-31-release"])
        );
    }

    #[test]
    fn ambiguous_or_indirect_metadata_never_produces_an_authority_document() {
        let valid=b"updated-dependencies:\n- dependency-name: example\n  dependency-version: 1.2.3\n  update-type: version-update:semver-patch\n";
        assert_eq!(
            metadata(valid).unwrap()["updated-dependencies"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        for bad in [
            "",
            "updated-dependencies: null",
            "updated-dependencies: {}",
            "other: []",
            "updated-dependencies: []\nupdated-dependencies: []",
            "updated-dependencies: []\n---\nupdated-dependencies: []",
            "updated-dependencies: &x []",
            "updated-dependencies: [*x]",
            "updated-dependencies: !!seq []",
            "updated-dependencies:\n- dependency-name: example\n  dependency-name: other",
            "updated-dependencies: [{12: value}]",
            "updated-dependencies: [{2026-10-31: value}]",
            "updated-dependencies: [{<<: value}]",
            "updated-dependencies: [.inf]",
        ] {
            assert!(metadata(bad.as_bytes()).is_err(), "{bad}");
        }
        assert!(metadata(&vec![b'x'; 65537]).is_err());
        let nesting = format!("updated-dependencies: {}{}", "[".repeat(17), "]".repeat(17));
        assert!(metadata(nesting.as_bytes()).is_err());
        let nodes = format!("updated-dependencies: [{}]", vec!["0"; 4096].join(","));
        assert!(metadata(nodes.as_bytes()).is_err());
    }
}
