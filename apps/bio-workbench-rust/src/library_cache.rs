//! Disposable list summaries. Record reads, edits and prediction inputs remain authoritative.
use crate::rpc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::PathBuf, time::SystemTime};

const TTL_MS: u64 = 30_000;
const MAX_ENTRIES: usize = 32;
const MAX_BYTES: usize = 8 * 1024 * 1024;
const VERSION: u64 = 1;

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Scope {
    pub endpoint: String,
    pub project: String,
    pub archived: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub scope: Scope,
    pub offset: u64,
    generation: u64,
    serial: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Listing {
    pub records: Vec<Value>,
    pub projects: Vec<Value>,
    pub next_offset: Option<u64>,
    pub filtered_count: u64,
    pub total_count: u64,
    fetched_ms: u64,
    used_ms: u64,
}

#[derive(Default)]
pub struct Cache {
    path: Option<PathBuf>,
    entries: BTreeMap<Scope, Listing>,
    pending: BTreeMap<Scope, Request>,
    indexes: BTreeMap<(String, bool), (u64, Vec<Value>)>,
    generation: u64,
    serial: u64,
}

impl Cache {
    /// Corrupt/oversize/old-format caches never affect the actual session draft.
    pub fn open(path: PathBuf) -> Self {
        let mut cache = Self {
            path: Some(path.clone()),
            ..Default::default()
        };
        let Ok(value) = rpc::read_json(&path, MAX_BYTES) else {
            return cache;
        };
        if value["version"] != VERSION {
            return cache;
        }
        let Ok(entries) = serde_json::from_value::<Vec<(Scope, Listing)>>(value["entries"].clone())
        else {
            return cache;
        };
        if entries.len() > MAX_ENTRIES {
            return cache;
        }
        for (scope, mut listing) in entries {
            if !valid_scope(&scope)
                || !listing
                    .records
                    .iter()
                    .chain(&listing.projects)
                    .all(valid_summary)
            {
                return Self {
                    path: Some(path),
                    ..Default::default()
                };
            }
            listing.records = listing.records.iter().map(summary).collect();
            listing.projects = listing.projects.iter().map(summary).collect();
            // A restart always revalidates with the head while showing the saved rows.
            listing.fetched_ms = 0;
            cache.entries.insert(scope, listing);
        }
        cache
    }

    pub fn get(&mut self, scope: &Scope, now: u64) -> Option<Listing> {
        let listing = self.entries.get_mut(scope)?;
        listing.used_ms = now;
        Some(listing.clone())
    }

    pub fn busy(&self, scope: &Scope) -> bool {
        self.pending.contains_key(scope)
    }

    /// Navigation coalesces active requests and reuses recently fetched pages.
    /// Manual refresh bypasses freshness; an existing request already does that work.
    pub fn begin(&mut self, scope: Scope, offset: u64, force: bool, now: u64) -> Option<Request> {
        if self
            .pending
            .get(&scope)
            .is_some_and(|pending| !(force && offset == 0 && pending.offset != 0))
        {
            return None;
        }
        if offset == 0
            && !force
            && self.entries.get(&scope).is_some_and(|entry| {
                entry.fetched_ms != 0
                    && now
                        .checked_sub(entry.fetched_ms)
                        .is_some_and(|age| age < TTL_MS)
            })
        {
            return None;
        }
        if offset != 0
            && self
                .entries
                .get(&scope)
                .is_some_and(|entry| entry.next_offset != Some(offset))
        {
            return None;
        }
        self.serial += 1;
        let request = Request {
            scope: scope.clone(),
            offset,
            generation: self.generation,
            serial: self.serial,
        };
        self.pending.insert(scope, request.clone());
        Some(request)
    }

    pub fn active(&self, request: &Request) -> bool {
        request.generation == self.generation && self.pending.get(&request.scope) == Some(request)
    }

    pub fn failed(&mut self, request: &Request) -> bool {
        if !self.active(request) {
            return false;
        }
        self.pending.remove(&request.scope);
        true
    }

    /// The request owns its scope even if navigation changed while SSH was running.
    #[cfg(test)]
    pub fn accept(
        &mut self,
        request: &Request,
        value: &Value,
        now: u64,
    ) -> Result<Option<Listing>, String> {
        self.accept_with_previous(request, value, now, None)
    }

    pub fn accept_with_previous(
        &mut self,
        request: &Request,
        value: &Value,
        now: u64,
        previous_records: Option<&[Value]>,
    ) -> Result<Option<Listing>, String> {
        if !self.failed(request) {
            return Ok(None);
        }
        let expected_project =
            (!request.scope.project.is_empty()).then_some(request.scope.project.as_str());
        if value["project_ref"].as_str() != expected_project
            || value["archived"].as_bool() != Some(request.scope.archived)
        {
            return Err("The head returned a different library listing scope.".into());
        }
        let records = value["records"]
            .as_array()
            .ok_or("The head returned an incomplete library listing.")?;
        let projects = value["projects"]
            .as_array()
            .ok_or("The head returned an incomplete project listing.")?;
        if !records.iter().chain(projects).all(valid_summary) {
            return Err("The head returned an invalid library summary.".into());
        }
        let mut listing = if request.offset == 0 {
            Listing {
                records: Vec::new(),
                projects: Vec::new(),
                next_offset: None,
                filtered_count: 0,
                total_count: 0,
                fetched_ms: now,
                used_ms: now,
            }
        } else {
            if let Some(entry) = self
                .entries
                .get(&request.scope)
                .filter(|entry| entry.next_offset == Some(request.offset))
            {
                entry.clone()
            } else if let Some(records) = previous_records {
                // Cache eviction must not disable pagination in the active view.
                Listing {
                    records: records.to_vec(),
                    projects: Vec::new(),
                    next_offset: None,
                    filtered_count: 0,
                    total_count: 0,
                    fetched_ms: 0,
                    used_ms: now,
                }
            } else {
                return Err(
                    "This library page no longer follows the current listing. Refresh the library."
                        .into(),
                );
            }
        };
        for record in records {
            if !listing
                .records
                .iter()
                .any(|existing| existing["ref"] == record["ref"])
            {
                listing.records.push(summary(record));
            }
        }
        listing.projects = projects.iter().map(summary).collect();
        listing.next_offset = value["next_offset"].as_u64();
        if listing
            .next_offset
            .is_some_and(|offset| offset <= request.offset)
        {
            return Err("The head returned a non-advancing library page.".into());
        }
        listing.filtered_count = value["filtered_count"]
            .as_u64()
            .unwrap_or(listing.records.len() as u64);
        listing.total_count = value["total_count"]
            .as_u64()
            .unwrap_or(listing.filtered_count);
        listing.used_ms = now;
        let index_key = (request.scope.endpoint.clone(), request.scope.archived);
        if let Some((_, projects)) = self
            .indexes
            .get(&index_key)
            .filter(|(serial, _)| *serial > request.serial)
        {
            listing.projects = projects.clone();
        } else {
            self.indexes
                .insert(index_key, (request.serial, listing.projects.clone()));
        }
        // Every list reply carries the current project index. Keep cached root
        // navigation in sync even when this response was for a child scope.
        for (scope, entry) in &mut self.entries {
            if scope.endpoint == request.scope.endpoint && scope.archived == request.scope.archived
            {
                entry.projects = listing.projects.clone();
            }
        }
        self.entries.insert(request.scope.clone(), listing.clone());
        self.trim();
        self.save();
        Ok(Some(listing))
    }

    /// Clear before a mutation and again when it settles. Old reads cannot resurrect rows.
    pub fn invalidate(&mut self) {
        self.generation += 1;
        self.entries.clear();
        self.pending.clear();
        self.indexes.clear();
        if !self.save()
            && let Some(path) = &self.path
        {
            // Unlinking an obsolete disposable cache can still succeed when a
            // full filesystem prevents writing its empty replacement.
            let _ = std::fs::remove_file(path);
        }
    }

    fn snapshot(&self) -> Value {
        json!({"version":VERSION,"entries":self.entries.iter().collect::<Vec<_>>()})
    }

    fn trim(&mut self) {
        while self.entries.len() > MAX_ENTRIES
            || serde_json::to_vec(&self.snapshot()).map_or(MAX_BYTES + 1, |bytes| bytes.len())
                > MAX_BYTES
        {
            let Some(scope) = self
                .entries
                .iter()
                .min_by_key(|(_, entry)| entry.used_ms)
                .map(|(scope, _)| scope.clone())
            else {
                break;
            };
            self.entries.remove(&scope);
        }
        self.indexes.retain(|(endpoint, archived), _| {
            self.entries
                .keys()
                .any(|scope| &scope.endpoint == endpoint && &scope.archived == archived)
        });
    }

    fn save(&self) -> bool {
        if let Some(path) = &self.path {
            // Cache persistence is best effort, never a reason to reject real library work.
            if let Ok(bytes) = serde_json::to_vec(&self.snapshot()) {
                return rpc::atomic_bytes(path, &bytes).is_ok();
            }
            return false;
        }
        true
    }
}

fn valid_scope(scope: &Scope) -> bool {
    !scope.endpoint.is_empty() && scope.endpoint.len() <= 1024 && scope.project.len() <= 256
}

fn valid_summary(value: &Value) -> bool {
    value.is_object()
        && value["ref"]
            .as_str()
            .is_some_and(|reference| !reference.is_empty() && reference.len() <= 256)
}

/// Explicit allowlist ensures a future expanded server response cannot cache sequences,
/// purpose documents, attachments or other full-record data through this list cache.
fn summary(value: &Value) -> Value {
    const FIELDS: &[&str] = &[
        "ref",
        "kind",
        "id",
        "name",
        "revision",
        "sha256",
        "inventory_id",
        "alt_name",
        "verbose_name",
        "modality",
        "archived",
        "status",
        "molecule_type",
        "aliases",
        "tags",
        "sequence_length",
        "molecular_form",
        "review_status",
        "review_reason",
        "submission_allowed",
        "encoded_by_ref",
        "parent_ref",
        "derivation_kind",
        "member_count",
    ];
    Value::Object(
        FIELDS
            .iter()
            .filter_map(|field| {
                value
                    .get(*field)
                    .filter(|value| {
                        value.is_null()
                            || value.is_string()
                            || value.is_boolean()
                            || value.is_number()
                            || (matches!(*field, "aliases" | "tags")
                                && value
                                    .as_array()
                                    .is_some_and(|values| values.iter().all(Value::is_string)))
                    })
                    .map(|value| ((*field).to_owned(), value.clone()))
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scope(project: &str) -> Scope {
        Scope {
            endpoint: "harrison@root:head:22".into(),
            project: project.into(),
            archived: false,
        }
    }

    fn response(scope: &Scope, references: &[&str], next: Option<u64>) -> Value {
        json!({"records":references.iter().map(|reference| json!({"ref":reference,"alt_name":"Example","sha256":"a".repeat(64)})).collect::<Vec<_>>(),
            "projects":[{"ref":"project:example@1","kind":"project","name":"Project"}],
            "project_ref":if scope.project.is_empty() { Value::Null } else { json!(scope.project) },
            "archived":scope.archived,"next_offset":next,"filtered_count":references.len(),"total_count":20})
    }

    fn fetch(cache: &mut Cache, scope: &Scope, references: &[&str], now: u64) {
        let request = cache.begin(scope.clone(), 0, false, now).unwrap();
        cache
            .accept(&request, &response(scope, references, None), now)
            .unwrap()
            .unwrap();
    }

    #[test]
    fn root_project_root_project_reuses_rows_and_coalesces_first_open() {
        let mut cache = Cache::default();
        let root = scope("");
        let project = scope("project:example@1");
        let first = cache.begin(root.clone(), 0, false, 100).unwrap();
        assert!(cache.begin(root.clone(), 0, false, 100).is_none());
        cache
            .accept(&first, &response(&root, &["construct:root@1"], None), 100)
            .unwrap();
        fetch(&mut cache, &project, &["construct:member@1"], 200);
        for key in [&root, &project, &root, &project] {
            assert_eq!(cache.get(key, 300).unwrap().records.len(), 1);
            assert!(cache.begin(key.clone(), 0, false, 300).is_none());
        }
    }

    #[test]
    fn ttl_manual_refresh_and_failure_keep_visible_cached_rows() {
        let mut cache = Cache::default();
        let key = scope("");
        fetch(&mut cache, &key, &["construct:a@1"], 100);
        assert!(cache.begin(key.clone(), 0, false, 30_099).is_none());
        let stale = cache.begin(key.clone(), 0, false, 30_100).unwrap();
        assert!(cache.begin(key.clone(), 0, true, 30_101).is_none());
        assert!(cache.get(&key, 30_101).is_some());
        assert!(cache.failed(&stale));
        assert!(cache.get(&key, 30_102).is_some());
        let forced = cache.begin(key.clone(), 0, true, 30_103).unwrap();
        cache
            .accept(&forced, &response(&key, &["construct:a@2"], None), 30_104)
            .unwrap();
        assert!(cache.begin(key.clone(), 0, false, 30_105).is_none());
        assert!(cache.begin(key, 0, true, 30_105).is_some());
    }

    #[test]
    fn scopes_are_independent_and_foreign_scope_replies_are_rejected() {
        let mut cache = Cache::default();
        let root = scope("");
        let project = scope("project:example@1");
        let first = cache.begin(root.clone(), 0, false, 100).unwrap();
        let second = cache.begin(project.clone(), 0, false, 100).unwrap();
        cache
            .accept(
                &second,
                &response(&project, &["construct:member@1"], None),
                101,
            )
            .unwrap();
        cache
            .accept(&first, &response(&root, &["construct:root@1"], None), 102)
            .unwrap();
        assert_eq!(
            cache.get(&project, 102).unwrap().records[0]["ref"],
            "construct:member@1"
        );
        let bad = cache.begin(project.clone(), 0, true, 103).unwrap();
        assert!(
            cache
                .accept(&bad, &response(&root, &[], None), 104)
                .is_err()
        );
        assert_eq!(
            cache.get(&project, 104).unwrap().records[0]["ref"],
            "construct:member@1"
        );
    }

    #[test]
    fn later_index_cannot_be_regressed_by_an_older_scope_response() {
        let mut cache = Cache::default();
        let root = scope("");
        let project = scope("project:example@1");
        let old = cache.begin(project.clone(), 0, false, 100).unwrap();
        let newer = cache.begin(root.clone(), 0, false, 101).unwrap();
        let mut fresh = response(&root, &[], None);
        fresh["projects"] = json!([{"ref":"project:example@2"}]);
        cache.accept(&newer, &fresh, 103).unwrap();
        let late = cache
            .accept(&old, &response(&project, &[], None), 104)
            .unwrap()
            .unwrap();
        assert_eq!(late.projects[0]["ref"], "project:example@2");
        assert_eq!(
            cache.get(&root, 105).unwrap().projects[0]["ref"],
            "project:example@2"
        );
        // Absence (e.g. archived elsewhere) is also protected, not just higher revisions.
        let old = cache.begin(project.clone(), 0, true, 106).unwrap();
        let newer = cache.begin(root.clone(), 0, true, 107).unwrap();
        fresh["projects"] = json!([]);
        cache.accept(&newer, &fresh, 108).unwrap();
        cache
            .accept(&old, &response(&project, &[], None), 109)
            .unwrap();
        assert!(cache.get(&root, 110).unwrap().projects.is_empty());
    }

    #[test]
    fn pagination_survives_navigation_but_refresh_replaces_all_old_pages() {
        let mut cache = Cache::default();
        let key = scope("project:example@1");
        let first = cache.begin(key.clone(), 0, false, 100).unwrap();
        cache
            .accept(&first, &response(&key, &["construct:a@1"], Some(500)), 100)
            .unwrap();
        assert!(cache.begin(key.clone(), 250, false, 101).is_none());
        let second = cache.begin(key.clone(), 500, false, 101).unwrap();
        cache
            .accept(
                &second,
                &response(&key, &["construct:a@1", "construct:b@1"], Some(1000)),
                102,
            )
            .unwrap();
        assert_eq!(cache.get(&key, 103).unwrap().records.len(), 2);
        assert_eq!(cache.get(&key, 103).unwrap().next_offset, Some(1000));
        assert!(cache.begin(key.clone(), 0, false, 103).is_none());
        let old_page = cache.begin(key.clone(), 1000, false, 104).unwrap();
        let refreshed = cache.begin(key.clone(), 0, true, 105).unwrap();
        assert_eq!(cache.get(&key, 106).unwrap().records.len(), 2);
        cache
            .accept(
                &refreshed,
                &response(&key, &["construct:a@2"], Some(500)),
                106,
            )
            .unwrap();
        assert!(
            cache
                .accept(&old_page, &response(&key, &["construct:c@1"], None), 107)
                .unwrap()
                .is_none()
        );
        let shown = cache.get(&key, 108).unwrap();
        assert_eq!(shown.records.len(), 1);
        assert_eq!(shown.records[0]["ref"], "construct:a@2");
        assert_eq!(shown.next_offset, Some(500));
    }

    #[test]
    fn mutations_invalidate_every_scope_and_pending_old_pages_even_across_restart() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("library-list-cache.json");
        let mut cache = Cache::open(path.clone());
        let root = scope("");
        let project = scope("project:example@1");
        let archive = Scope {
            archived: true,
            ..root.clone()
        };
        for key in [&root, &project, &archive] {
            fetch(&mut cache, key, &["construct:a@1"], 100);
        }
        let late = cache.begin(root.clone(), 0, true, 101).unwrap();
        cache.invalidate();
        assert!(cache.entries.is_empty());
        let fresh = cache.begin(root.clone(), 0, false, 102).unwrap();
        assert!(
            cache
                .accept(&late, &response(&root, &["construct:a@1"], None), 103)
                .unwrap()
                .is_none()
        );
        assert!(cache.active(&fresh));
        assert!(Cache::open(path).entries.is_empty());
    }

    #[test]
    fn persisted_summaries_restore_stale_without_sequences_and_are_endpoint_isolated() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("cache.json");
        let key = scope("");
        let mut cache = Cache::open(path.clone());
        let request = cache.begin(key.clone(), 0, false, 100).unwrap();
        let mut value = response(&key, &["construct:a@1"], None);
        value["records"][0]["identity"] = json!({"sequence":"ACDE"});
        value["records"][0]["sequence"] = json!("ACDE");
        value["records"][0]["attachments"] = json!([{"path":"source.fa"}]);
        value["records"][0]["name"] = json!({"sequence":"ACDE"});
        cache.accept(&request, &value, 100).unwrap();
        let disk = std::fs::read_to_string(&path).unwrap();
        assert!(!disk.contains("ACDE"));
        assert!(!disk.contains("source.fa"));
        let mut restored = Cache::open(path);
        assert_eq!(
            restored.get(&key, 101).unwrap().records[0]["ref"],
            "construct:a@1"
        );
        assert!(restored.begin(key.clone(), 0, false, 101).is_some());
        for endpoint in [
            "other@root:head:22",
            "harrison@user:head:22",
            "harrison@root:other:22",
            "harrison@root:head:2222",
        ] {
            assert!(
                restored
                    .get(
                        &Scope {
                            endpoint: endpoint.into(),
                            ..key.clone()
                        },
                        102
                    )
                    .is_none()
            );
        }
    }

    #[test]
    fn invalid_cache_never_breaks_session_state_or_exceeds_storage_bounds() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("cache.json");
        let draft = directory.path().join("draft.json");
        std::fs::write(&draft, "retained inputs").unwrap();
        for bytes in [
            b"{".to_vec(),
            br#"{"version":2,"entries":[]}"#.to_vec(),
            vec![b' '; MAX_BYTES + 1],
        ] {
            std::fs::write(&path, bytes).unwrap();
            assert!(Cache::open(path.clone()).entries.is_empty());
        }
        let mut cache = Cache::open(path.clone());
        for index in 0..40 {
            fetch(
                &mut cache,
                &scope(&format!("project:example-{index}@1")),
                &["construct:a@1"],
                100 + index,
            );
        }
        assert_eq!(cache.entries.len(), MAX_ENTRIES);
        assert!(std::fs::metadata(&path).unwrap().len() <= MAX_BYTES as u64);
        assert_eq!(std::fs::read_to_string(&draft).unwrap(), "retained inputs");
    }

    #[test]
    fn cache_eviction_does_not_disable_pagination_in_the_active_view() {
        let mut cache = Cache::default();
        let key = scope("project:example@1");
        let first = cache.begin(key.clone(), 0, false, 100).unwrap();
        let listing = cache
            .accept(&first, &response(&key, &["construct:a@1"], Some(500)), 100)
            .unwrap()
            .unwrap();
        cache.entries.remove(&key); // The current rows remain visible after LRU eviction.
        let next = cache.begin(key.clone(), 500, false, 101).unwrap();
        let result = cache
            .accept_with_previous(
                &next,
                &response(&key, &["construct:b@1"], None),
                102,
                Some(&listing.records),
            )
            .unwrap()
            .unwrap();
        assert_eq!(result.records.len(), 2);
        assert_eq!(result.records[0]["ref"], "construct:a@1");
        assert_eq!(result.records[1]["ref"], "construct:b@1");
    }
}
