//! `dict(row)` for a SQLite row: every column, in order, each value with its
//! OWN storage type (SQLite types values, not columns) — as Python's sqlite3
//! hands them to jsonify.
use serde_json::{json, Map, Value};
use sqlx::{Column, Row, TypeInfo, ValueRef};

pub fn row_dict(r: &sqlx::sqlite::SqliteRow) -> Value {
    let mut m = Map::new();
    for (i, col) in r.columns().iter().enumerate() {
        let kind = r.try_get_raw(i).map(|v| if v.is_null() { "NULL".to_string() } else { v.type_info().name().to_string() }).unwrap_or_else(|_| "NULL".into());
        let v = match kind.as_str() {
            "INTEGER" => r.try_get::<i64, _>(i).map(|x| json!(x)).unwrap_or(Value::Null),
            "REAL" => r.try_get::<f64, _>(i).map(|x| json!(x)).unwrap_or(Value::Null),
            "TEXT" => r.try_get::<String, _>(i).map(Value::String).unwrap_or(Value::Null),
            "BLOB" => r.try_get::<Vec<u8>, _>(i).map(|b| Value::String(String::from_utf8_lossy(&b).into_owned())).unwrap_or(Value::Null),
            _ => Value::Null,
        };
        m.insert(col.name().to_string(), v);
    }
    Value::Object(m)
}

pub async fn rows(pool: &sqlx::SqlitePool, sql: &str) -> Result<Vec<Value>, sqlx::Error> {
    Ok(sqlx::query(sql).fetch_all(pool).await?.iter().map(row_dict).collect())
}
