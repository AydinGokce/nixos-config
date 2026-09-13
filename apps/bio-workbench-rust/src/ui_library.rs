//! Project library curation with immutable revisions and durable head undo/redo.
use super::*;

const MAX_DOCUMENT_BYTES: usize = 1024 * 1024;

#[derive(Default)]
pub(super) struct Explorer {
    pub records: Vec<Value>,
    pub projects: Vec<Value>,
    pub selected: String,
    pub detail: Value,
    pub(super) sequence: ui_sequence::Viewer,
    expanded: BTreeSet<String>,
    pub error: String,
    pub detail_error: String,
    query: String,
    kind: String,
    molecule: String,
    project: String,
    review_only: bool,
    loaded: bool,
    history_refresh_again: bool,
    scope: String,
    cache: Option<library_cache::Cache>,
    next_offset: Option<u64>,
    filtered_count: u64,
    total_count: u64,
    tab: usize,
    source_document: bool,
    archive: bool,
    scoped_archive: bool,
    pub(super) undo_history: Value,
    write_error: String,
    edit: Option<Edit>,
    added: String,
}

#[derive(Clone)]
struct Edit {
    reference: String,
    sha256: String,
    field: String,
    value: String,
    original: String,
    focus: bool,
}

impl Edit {
    fn new(detail: &Value, field: &str) -> Self {
        let value = edit_value(detail, field);
        Self {
            reference: text(detail, "ref").into(),
            sha256: presentation(detail, "sha256").into(),
            field: field.into(),
            value: value.into(),
            original: value.into(),
            focus: true,
        }
    }

    fn can_save(&self) -> bool {
        !self.sha256.is_empty()
            && self.value != self.original
            && (matches!(self.field.as_str(), "alt_name" | "description") || !self.value.is_empty())
            && (self.field != "description" || self.value.len() <= MAX_DOCUMENT_BYTES)
    }

    fn save_params(&self) -> Option<Value> {
        if !self.can_save() {
            return None;
        }
        let mut patch = json!({});
        patch[&self.field] = json!(self.value);
        Some(json!({"ref":self.reference,"expected_sha256":self.sha256,"patch":patch}))
    }
}

fn edit_value<'a>(detail: &'a Value, field: &str) -> &'a str {
    match field {
        "sequence" => text(&detail["record"]["identity"], "sequence"),
        "description" => detail["description"]
            .as_str()
            .unwrap_or_else(|| text(&detail["description"], "text")),
        _ => presentation(detail, field),
    }
}

impl Explorer {
    pub fn restore(extra: &BTreeMap<String, Value>) -> Self {
        let value = extra.get("library_explorer").unwrap_or(&Value::Null);
        Self {
            query: text(value, "query").into(),
            molecule: text(value, "molecule").into(),
            review_only: value["review_only"].as_bool().unwrap_or(false),
            ..Default::default()
        }
    }

    pub fn preferences(&self) -> Value {
        json!({"selected":self.selected,"query":self.query,"kind":self.kind,
            "molecule":self.molecule,"project":self.project,"review_only":self.review_only})
    }

    fn open_project(&mut self, reference: &str) {
        self.project = reference.into();
        self.expanded.clear();
        self.query.clear();
        self.molecule.clear();
        self.review_only = false;
    }

    fn toggle_all_parents(&mut self) {
        let parents = expandable_parents(&self.records);
        if parents.iter().any(|parent| self.expanded.contains(parent)) {
            self.expanded.clear();
        } else {
            self.expanded.extend(parents);
        }
    }

    fn visible(&self, record: &Value) -> bool {
        if (!self.kind.is_empty() && text(record, "kind") != self.kind)
            || (!self.molecule.is_empty() && text(record, "molecule_type") != self.molecule)
            || (self.review_only && text(record, "review_status") != "review_required")
        {
            return false;
        }
        let haystack = record.to_string().to_lowercase();
        self.query
            .to_lowercase()
            .split_whitespace()
            .all(|word| haystack.contains(word))
    }

    fn reconcile_edit(&mut self, value: &Value, sent: &Value) {
        let Some(edit) = self.edit.as_mut() else {
            return;
        };
        let Some(change) = rows(value, "changed_refs")
            .iter()
            .find(|change| text(change, "before_ref") == edit.reference)
        else {
            return;
        };
        if text(sent, "ref") == edit.reference
            && sent["patch"][&edit.field].as_str() == Some(&edit.value)
        {
            self.edit = None;
        } else {
            // An unrelated write or a recovered older save must not discard new typing.
            edit.reference = text(change, "after_ref").into();
            edit.sha256.clear();
        }
    }

    fn accept_detail(&mut self, reference: &str, value: Value) -> bool {
        if self.selected != reference {
            return false;
        }
        if text(&value, "ref") != reference || !value["record"].is_object() {
            self.detail_error =
                "The head returned a different or incomplete library record.".into();
            return false;
        }
        if let Some(edit) = self
            .edit
            .as_mut()
            .filter(|edit| edit.reference == reference && edit.sha256.is_empty())
        {
            edit.sha256 = presentation(&value, "sha256").into();
            edit.original = edit_value(&value, &edit.field).into();
        }
        let identity = &value["record"]["identity"];
        let view = if value["sequence_view"].is_object() {
            value["sequence_view"].clone()
        } else {
            json!({"ref":reference,"molecule_type":identity["molecule_type"],"length":text(identity,"sequence").len(),"circular":identity["circular"],"available":!text(identity,"sequence").is_empty(),"derivation_kind":"explicit"})
        };
        self.sequence
            .accept(reference, view, text(identity, "sequence"));
        self.sequence
            .refresh_receipt(reference, text(&value, "sha256"));
        self.detail = value;
        self.detail_error.clear();
        true
    }
}

fn parent_reference(record: &Value) -> &str {
    let parent = text(record, "parent_ref");
    if parent.is_empty() {
        text(record, "encoded_by_ref")
    } else {
        parent
    }
}

fn nucleotide_parent(record: &Value) -> bool {
    text(record, "kind") == "construct" && matches!(text(record, "molecule_type"), "dna" | "rna")
}

fn expandable_parents(records: &[Value]) -> BTreeSet<String> {
    let linked: BTreeSet<_> = records
        .iter()
        .filter(|record| text(record, "molecule_type") == "protein")
        .map(|record| {
            parent_reference(record)
                .split('@')
                .next()
                .unwrap_or_default()
        })
        .collect();
    records
        .iter()
        .filter(|record| nucleotide_parent(record))
        .map(|record| text(record, "ref").split('@').next().unwrap_or_default())
        .filter(|family| linked.contains(family))
        .map(str::to_owned)
        .collect()
}

fn hierarchy(
    records: &[Value],
    expanded: &BTreeSet<String>,
    visible: impl Fn(&Value) -> bool,
) -> Vec<(Value, usize, usize)> {
    let parents: BTreeSet<_> = records
        .iter()
        .filter(|r| nucleotide_parent(r))
        .map(|r| {
            text(r, "ref")
                .split('@')
                .next()
                .unwrap_or_default()
                .to_owned()
        })
        .collect();
    let mut children: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for record in records {
        let parent = parent_reference(record)
            .split('@')
            .next()
            .unwrap_or_default();
        if text(record, "molecule_type") == "protein" && parents.contains(parent) {
            children.entry(parent.into()).or_default().push(record);
        }
    }
    let mut result = Vec::new();
    for record in records {
        if text(record, "kind") == "project" {
            continue;
        }
        let parent = parent_reference(record)
            .split('@')
            .next()
            .unwrap_or_default();
        if text(record, "molecule_type") == "protein" && parents.contains(parent) {
            continue;
        }
        let family = text(record, "ref").split('@').next().unwrap_or_default();
        let child = children.get(family).cloned().unwrap_or_default();
        let matches: Vec<_> = child.iter().filter(|r| visible(r)).collect();
        if !visible(record) && matches.is_empty() {
            continue;
        }
        result.push((record.clone(), 0, child.len()));
        if expanded.contains(family) {
            for child in matches {
                result.push(((*child).clone(), 1, 0));
            }
        }
    }
    result
}

fn latest_project(current: &str, projects: &[Value]) -> Option<String> {
    let (family, revision) = current.rsplit_once('@')?;
    let revision = revision.parse::<u64>().ok()?;
    projects
        .iter()
        .filter_map(|project| {
            let reference = text(project, "ref");
            let (candidate, next) = reference.rsplit_once('@')?;
            let next = next.parse::<u64>().ok()?;
            (candidate == family && next > revision).then_some((next, reference))
        })
        .max_by_key(|(revision, _)| *revision)
        .map(|(_, reference)| reference.into())
}

fn project_in_index(current: &str, projects: &[Value]) -> bool {
    let family = current.split('@').next();
    projects
        .iter()
        .any(|project| text(project, "ref").split('@').next() == family)
}

fn pinned_input(detail: &Value, chain: String) -> Result<Input, String> {
    let reference = text(detail, "ref");
    let record = &detail["record"];
    let kind = text(record, "kind");
    let expected = format!(
        "{}:{}@{}",
        kind,
        text(record, "id"),
        record["revision"].as_u64().unwrap_or(0)
    );
    if reference != expected || record["revision"].as_u64().unwrap_or(0) == 0 {
        return Err("The selected record has no verified immutable revision.".into());
    }
    if detail
        .pointer("/submission/allowed")
        .and_then(Value::as_bool)
        != Some(true)
    {
        let reason = detail
            .pointer("/submission/reason")
            .and_then(Value::as_str)
            .unwrap_or("");
        return Err(if reason.is_empty() {
            "This record is not available as a prediction input.".into()
        } else {
            reason.into()
        });
    }
    let molecule = if kind == "assembly" {
        "assembly"
    } else if kind != "construct" {
        return Err("Select a construct or assembly to add an input.".into());
    } else {
        match text(&record["identity"], "molecule_type") {
            "small_molecule" => "ligand",
            "protein" => "protein",
            "dna" => "dna",
            "rna" => "rna",
            _ => return Err(
                "This molecular identity needs a supported model adapter before it can be added."
                    .into(),
            ),
        }
    };
    Ok(Input {
        id: uid(),
        name: if display_name(detail).is_empty() {
            reference.into()
        } else {
            display_name(detail).into()
        },
        molecule_type: molecule.into(),
        chain_id: chain,
        source: json!({"kind":"library","ref":reference}),
        ..Default::default()
    })
}

fn kind_label(record: &Value) -> &str {
    if !text(record, "modality").is_empty() {
        return text(record, "modality");
    }
    if text(record, "molecular_form") == "plasmid" {
        return "plasmid";
    }
    match text(record, "kind") {
        "project" => "PROJECT",
        "assembly" => "ASSEMBLY",
        "monomer" => "MONOMER",
        _ => match text(record, "molecule_type") {
            "protein" => "PROTEIN",
            "dna" => "DNA",
            "rna" => "RNA",
            "small_molecule" => "LIGAND",
            "mixed_polymer" => "MIXED",
            _ => "CONSTRUCT",
        },
    }
}

fn review_label(record: &Value) -> (&str, Color32) {
    match text(record, "review_status") {
        "review_required" => ("REVIEW REQUIRED", AMBER),
        "reference_matched" => ("REFERENCE MATCHED", GREEN),
        other if !other.is_empty() => (other, Color32::LIGHT_GRAY),
        _ => (text(record, "status"), Color32::LIGHT_GRAY),
    }
}

impl Workbench {
    pub(super) fn open_library(&mut self) {
        if self.sidebar_tab != 2 {
            self.library_projects();
        }
        self.sidebar_tab = 2;
        self.library_navigate();
        self.library_refresh_history();
    }

    pub(super) fn open_library_archive(&mut self) {
        self.sidebar_tab = 2;
        self.library.archive = true;
        self.library.project.clear();
        self.library.expanded.clear();
        self.library.selected.clear();
        self.library.detail = Value::Null;
        self.library.query.clear();
        self.library.kind.clear();
        self.library.molecule.clear();
        self.library.review_only = false;
        self.library.edit = None;
        self.library_navigate();
        self.library_refresh_history();
    }

    fn library_projects(&mut self) {
        self.library.archive = false;
        self.library.project.clear();
        self.library.expanded.clear();
        self.library.selected.clear();
        self.library.detail = Value::Null;
        self.library.edit = None;
        self.library.query.clear();
        self.library_navigate();
    }

    fn library_refresh_history(&mut self) {
        if self.busy(&Purpose::LibraryHistory) {
            self.library.history_refresh_again = true;
            return;
        }
        self.request("library.history", json!({}), Purpose::LibraryHistory);
    }

    pub(super) fn library_received_history(&mut self, value: Value) {
        self.library.undo_history = value;
        if self.library.history_refresh_again {
            self.library.history_refresh_again = false;
            self.library_refresh_history();
        }
    }

    fn library_writing(&self) -> bool {
        self.pending
            .values()
            .any(|pending| matches!(pending.purpose, Purpose::LibraryWrite(_)))
    }

    fn library_write(&mut self, method: &str, mut params: Value) {
        if self.library_writing() {
            return;
        }
        params["request_key"] = json!(uid());
        self.library.write_error.clear();
        self.library_invalidate_lists();
        if self
            .request(method, params.clone(), Purpose::LibraryWrite(params))
            .is_none()
        {
            self.library.write_error =
                "Could not send this edit. Check the connection and try again.".into();
        }
    }

    fn library_archive_record(&mut self, record: &Value, archived: bool) {
        let reference = text(record, "ref");
        let sha256 = presentation(record, "sha256");
        if reference.is_empty() || sha256.is_empty() {
            self.library.write_error =
                "This record needs a current revision receipt. Refresh the library before editing."
                    .into();
            return;
        }
        self.library_write(
            "library.edit",
            json!({"ref":reference,"expected_sha256":sha256,"patch":{"archived":archived}}),
        );
    }

    pub(super) fn library_received_write(&mut self, value: Value, sent: &Value) {
        self.library_invalidate_lists();
        self.library.reconcile_edit(&value, sent);
        self.library.sequence.write_received(&value, sent);
        let selected = self.library.selected.clone();
        for change in rows(&value, "changed_refs") {
            if text(change, "before_ref").is_empty() {
                continue;
            }
            if self.library.project == text(change, "before_ref") {
                self.library.project = text(change, "after_ref").into();
            }
            if self.library.selected == text(change, "before_ref") {
                self.library.selected = text(change, "after_ref").into();
            }
        }
        if !selected.is_empty()
            && self.library.selected == selected
            && !text(&value, "ref").is_empty()
        {
            // Only follow a target when it was the record being inspected.
            if selected.split('@').next() == text(&value, "ref").split('@').next() {
                self.library.selected = text(&value, "ref").into();
            }
        }
        if (sent["parent_ref"].is_string() && sent["translation"].is_object())
            || (sent["project_ref"].is_string() && sent["sequence"].is_string())
        {
            self.library.selected = text(&value, "ref").into();
            self.library.tab = 1;
        }
        self.library.write_error.clear();
        self.library.undo_history = value["history"].clone();
        self.library.detail = Value::Null;
        self.library.records.clear();
        self.library.projects.clear();
        self.library.loaded = false;
        self.library_refresh();
        self.library_refresh_history();
        if !self.library.selected.is_empty() {
            self.library_select(&self.library.selected.clone());
        }
        self.log("Library edit saved as a new revision on the head.");
        self.persist();
    }

    fn library_begin_edit(&mut self, detail: &Value, field: &str) {
        if field == "sequence"
            && detail["record"]["identity"]["encoded_by"]["translation"].is_object()
        {
            if let Some(action) = self.library.sequence.definition_editor(detail) {
                self.library_sequence_action(action);
            }
            return;
        }
        if self.library_writing() || detail["is_latest"] == false {
            return;
        }
        if self
            .library
            .edit
            .as_ref()
            .is_some_and(|edit| edit.field == "description" && edit.value != edit.original)
        {
            self.library.write_error =
                "Save or cancel the project description before editing another field.".into();
            return;
        }
        self.library.edit = Some(Edit::new(detail, field));
        self.library.write_error.clear();
    }

    fn library_save_edit(&mut self) {
        if let Some(params) = self.library.edit.as_ref().and_then(Edit::save_params) {
            self.library_write("library.edit", params);
        }
    }

    fn library_scope(&self) -> Option<library_cache::Scope> {
        Some(library_cache::Scope {
            endpoint: self.session.as_ref()?.connection.identity(),
            project: self.library.project.clone(),
            archived: self.library.archive,
        })
    }

    fn library_cache(&mut self) -> Option<&mut library_cache::Cache> {
        if self.library.cache.is_none() {
            self.library.cache = Some(library_cache::Cache::open(
                self.session.as_ref()?.library_cache_path(),
            ));
        }
        self.library.cache.as_mut()
    }

    fn library_list_busy(&self) -> bool {
        self.library_scope().is_some_and(|scope| {
            self.library
                .cache
                .as_ref()
                .is_some_and(|cache| cache.busy(&scope))
        })
    }

    fn library_show_listing(
        &mut self,
        scope: &library_cache::Scope,
        listing: library_cache::Listing,
    ) {
        self.library.records = listing.records;
        self.library.projects = listing.projects;
        self.library
            .sequence
            .refresh_projects(&self.library.projects);
        self.library.next_offset = listing.next_offset;
        self.library.filtered_count = listing.filtered_count;
        self.library.total_count = listing.total_count;
        self.library.scope = scope.project.clone();
        self.library.scoped_archive = scope.archived;
        self.library.loaded = true;
    }

    /// Sidebar navigation follows the current project revision; deliberately opened
    /// historical record details retain their own independent pinned reference.
    fn library_reconcile_project(&mut self, projects: &[Value]) -> bool {
        if let Some(reference) = latest_project(&self.library.project, projects) {
            let follow_detail =
                self.library.selected == self.library.project && self.library.edit.is_none();
            self.library.project = reference.clone();
            if follow_detail {
                self.library_select(&reference);
            }
            true
        } else {
            false
        }
    }

    /// Navigation uses cached rows immediately; manual Refresh bypasses the TTL.
    fn library_navigate(&mut self) {
        self.library_fetch(false, 0);
    }

    pub(super) fn library_refresh(&mut self) {
        self.library_fetch(true, 0);
    }

    fn library_fetch(&mut self, force: bool, offset: u64) {
        let Some(mut scope) = self.library_scope() else {
            self.library.error =
                "Library connection unavailable. Check the connection and refresh.".into();
            return;
        };
        let now = library_cache::now_ms();
        if offset == 0 {
            let cached = self
                .library_cache()
                .and_then(|cache| cache.get(&scope, now));
            if let Some(listing) = cached {
                if self.library_reconcile_project(&listing.projects) {
                    scope = self.library_scope().expect("existing session");
                    if let Some(listing) = self
                        .library_cache()
                        .and_then(|cache| cache.get(&scope, now))
                    {
                        self.library_show_listing(&scope, listing);
                    }
                } else {
                    self.library_show_listing(&scope, listing);
                }
            }
            if self.library.scope != scope.project || self.library.scoped_archive != scope.archived
            {
                self.library.records.clear();
                self.library.projects.clear();
                self.library.next_offset = None;
                self.library.loaded = false;
            }
        }
        self.library.error.clear();
        // Its completion starts a fresh generation, so navigation never fills
        // the cache while a mutation may still be publishing new revisions.
        if self.library_writing() {
            return;
        }
        let Some(request) = self
            .library_cache()
            .and_then(|cache| cache.begin(scope, offset, force, now))
        else {
            return;
        };
        let mut params = json!({"limit":500,"offset":offset,"archived":request.scope.archived});
        if !request.scope.project.is_empty() {
            params["project_ref"] = json!(request.scope.project);
        }
        if self
            .request("library.list", params, Purpose::Library(request.clone()))
            .is_none()
        {
            if let Some(cache) = self.library.cache.as_mut() {
                cache.failed(&request);
            }
            self.library.error =
                "Library connection unavailable. Check the connection and refresh.".into();
        }
    }

    pub(super) fn library_load_page(&mut self, offset: u64) {
        if self.library.scope == self.library.project
            && self.library.scoped_archive == self.library.archive
            && self.library.next_offset == Some(offset)
        {
            self.library_fetch(false, offset);
        }
    }

    pub(super) fn library_received_list(&mut self, request: &library_cache::Request, value: Value) {
        let previous_records = (request.offset != 0
            && self.library_scope().as_ref() == Some(&request.scope)
            && self.library.next_offset == Some(request.offset))
        .then_some(self.library.records.as_slice());
        let Some(cache) = self.library.cache.as_mut() else {
            return;
        };
        match cache.accept_with_previous(request, &value, library_cache::now_ms(), previous_records)
        {
            Ok(Some(listing)) => {
                // A fresh index can advance a cached project even if that index
                // request finished after the user already opened the project.
                let same_endpoint = self
                    .session
                    .as_ref()
                    .is_some_and(|session| session.connection.identity() == request.scope.endpoint);
                if same_endpoint
                    && !request.scope.archived
                    && !self.library.archive
                    && !self.library.project.is_empty()
                    && !project_in_index(&self.library.project, &listing.projects)
                {
                    // Only a freshly accepted complete index establishes that
                    // the active project was archived by another client.
                    self.library.project.clear();
                    if self.library.edit.is_none() {
                        self.library.selected.clear();
                        self.library.detail = Value::Null;
                    }
                    self.library_navigate();
                } else if same_endpoint
                    && !request.scope.archived
                    && self.library_reconcile_project(&listing.projects)
                {
                    self.library_navigate();
                } else if self.library_scope().as_ref() == Some(&request.scope) {
                    self.library_show_listing(&request.scope, listing);
                    self.library.error.clear();
                }
            }
            Err(error) if self.library_scope().as_ref() == Some(&request.scope) => {
                self.library.error = error
            }
            _ => {}
        }
    }

    pub(super) fn library_invalidate_lists(&mut self) {
        if let Some(cache) = self.library_cache() {
            cache.invalidate();
        }
    }

    pub(super) fn library_select(&mut self, reference: &str) {
        if reference.is_empty() {
            return;
        }
        if self.library.selected != reference {
            self.library.edit = None;
            self.library.selected = reference.into();
            if let Some(record) = self
                .library
                .records
                .iter()
                .find(|r| text(r, "ref") == reference)
            {
                let parent = parent_reference(record)
                    .split('@')
                    .next()
                    .unwrap_or_default();
                if !parent.is_empty() {
                    self.library.expanded.insert(parent.into());
                }
                if text(record, "molecular_form") == "plasmid"
                    || text(record, "molecule_type") == "protein"
                {
                    self.library.tab = 1;
                }
            }
            self.library.detail = Value::Null;
            self.library.detail_error.clear();
            self.library.added.clear();
        }
        if self
            .request(
                "library.get",
                json!({"ref":reference}),
                Purpose::LibraryRecord(reference.into()),
            )
            .is_none()
            && !self.busy(&Purpose::LibraryRecord(reference.into()))
        {
            self.library.detail_error =
                "Record unavailable. Check the connection and retry.".into();
        }
    }

    pub(super) fn library_received_record(&mut self, reference: &str, value: Value) {
        let archived_project = value["archived"] == true
            && text(&value["record"], "kind") == "project"
            && self.library.project == reference;
        if self.library.accept_detail(reference, value) && archived_project {
            self.library.project.clear();
            self.library_refresh();
        }
    }

    pub(super) fn library_failed(&mut self, purpose: &Purpose, message: &str) {
        match purpose {
            Purpose::Library(request) => {
                let active = self
                    .library
                    .cache
                    .as_mut()
                    .is_some_and(|cache| cache.failed(request));
                if active && self.library_scope().as_ref() == Some(&request.scope) {
                    self.library.error = message.into();
                }
            }
            Purpose::LibraryWrite(_) => {
                self.library_invalidate_lists();
                self.library_refresh();
                self.library.write_error = message.into();
            }
            Purpose::LibraryHistory => self.library.write_error = message.into(),
            Purpose::LibrarySequence(params) if text(params, "ref") == self.library.selected => {
                self.library.sequence.error = message.into()
            }
            Purpose::LibraryProductPreview(params) => {
                self.library.sequence.preview_failed(params, message)
            }
            Purpose::LibraryRecord(reference) if reference == &self.library.selected => {
                self.library.detail_error = message.into()
            }
            _ => {}
        }
    }

    fn library_sequence_action(&mut self, action: ui_sequence::Action) {
        match action {
            ui_sequence::Action::Options(params) => {
                self.request(
                    "library.sequence",
                    params.clone(),
                    Purpose::LibrarySequence(params),
                );
            }
            ui_sequence::Action::Preview(params) => {
                if self
                    .request(
                        "library.product_preview",
                        params.clone(),
                        Purpose::LibraryProductPreview(params.clone()),
                    )
                    .is_none()
                {
                    self.library.sequence.preview_failed(&params,"Could not request a translation preview. Check the connection and try again.");
                }
            }
            ui_sequence::Action::Write(method, params) => self.library_write(method, params),
            ui_sequence::Action::Parent(reference) => {
                self.library.tab = 1;
                self.library_select(&reference);
            }
            ui_sequence::Action::EditSequence => {
                self.library_begin_edit(&self.library.detail.clone(), "sequence")
            }
        }
    }

    pub(super) fn library_received_sequence(&mut self, params: &Value, value: Value) {
        let reference = text(params, "ref");
        if reference == self.library.selected {
            self.library.sequence.received_options(
                params,
                value,
                text(&self.library.detail["record"]["identity"], "sequence"),
            );
        }
    }

    pub(super) fn library_toolbar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if ui.button("Refresh library").clicked() {
                self.library_refresh();
                self.library_refresh_history();
            }
            let writing = self.library_writing();
            for (field, label, method) in [
                ("undo", "Undo", "library.undo"),
                ("redo", "Redo", "library.redo"),
            ] {
                let entry = self.library.undo_history[field].clone();
                let operation = text(&entry, "operation_id");
                if ui
                    .add_enabled(!writing && !operation.is_empty(), egui::Button::new(label))
                    .on_hover_text(text(&entry, "label"))
                    .clicked()
                {
                    self.library_write(method, json!({"operation_id":operation}));
                }
            }
            if writing {
                ui.spinner();
            }
            ui.separator();
            ui.label(
                RichText::new(if self.library.archive {
                    "LIBRARY ARCHIVE"
                } else {
                    "HEAD / MOLECULAR LIBRARY"
                })
                .strong()
                .color(AMBER),
            );
            if !self.library.project.is_empty()
                && !self.library.archive
                && ui
                    .add_enabled(!writing, egui::Button::new("+ Standalone protein"))
                    .clicked()
                && let Some(project) = self
                    .library
                    .projects
                    .iter()
                    .find(|p| text(p, "ref") == self.library.project)
            {
                self.library
                    .sequence
                    .open_standalone(text(project, "ref"), text(project, "sha256"));
            }
            if ui.button("Molecular viewer").clicked() {
                self.sidebar_tab = 1;
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("Connection…").clicked() {
                    self.connection_open = true;
                }
                ui.small(&self.connection.host);
            });
        });
    }

    pub(super) fn library_sidebar(&mut self, ui: &mut egui::Ui) {
        let projects = self.library.project.is_empty() && !self.library.archive;
        if !projects && ui.button("< Back to projects").clicked() {
            self.library_projects();
            return;
        }
        Self::section(
            ui,
            if self.library.archive {
                "ARCHIVE"
            } else if projects {
                "PROJECTS"
            } else {
                "CONSTRUCTS"
            },
        );
        if !projects && !self.library.archive {
            let project = self
                .library
                .projects
                .iter()
                .find(|item| text(item, "ref") == self.library.project);
            let name = project
                .map(|value| text(value, "name"))
                .unwrap_or(&self.library.project)
                .to_owned();
            if ui.link(name).on_hover_text("Open project brief").clicked() {
                self.library_select(&self.library.project.clone());
            }
        }
        ui.add(
            egui::TextEdit::singleline(&mut self.library.query)
                .hint_text(if projects {
                    "Search projects…"
                } else {
                    "Search IDs, alt names, source names…"
                })
                .desired_width(f32::INFINITY),
        );
        if !projects {
            ui.horizontal_wrapped(|ui| {
                egui::ComboBox::from_id_salt("library-molecule")
                    .width(108.)
                    .selected_text(if self.library.molecule.is_empty() {
                        "All modalities"
                    } else {
                        &self.library.molecule
                    })
                    .show_ui(ui, |ui| {
                        for molecule in [
                            "",
                            "protein",
                            "dna",
                            "rna",
                            "small_molecule",
                            "mixed_polymer",
                        ] {
                            ui.selectable_value(
                                &mut self.library.molecule,
                                molecule.into(),
                                if molecule.is_empty() {
                                    "All modalities"
                                } else {
                                    molecule
                                },
                            );
                        }
                    });
                if !self.library.archive {
                    let parents = expandable_parents(&self.library.records);
                    let any_expanded = parents
                        .iter()
                        .any(|parent| self.library.expanded.contains(parent));
                    if ui
                        .add_enabled(
                            !parents.is_empty(),
                            egui::Button::new(if any_expanded {
                                "Collapse all"
                            } else {
                                "Expand all"
                            }),
                        )
                        .clicked()
                    {
                        self.library.toggle_all_parents();
                    }
                }
                ui.checkbox(&mut self.library.review_only, "Needs review");
            });
        }
        if !self.library.error.is_empty() {
            ui.colored_label(RED, &self.library.error);
        }
        if !self.library.write_error.is_empty() {
            ui.colored_label(RED, &self.library.write_error);
        }
        if self.library_list_busy() {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.small(if self.library.loaded {
                    "Refreshing library…"
                } else {
                    "Loading library…"
                });
            });
        }
        if self.library.scope != self.library.project
            || self.library.scoped_archive != self.library.archive
        {
            return;
        }
        let visible: Vec<(Value, usize, usize)> = if projects {
            self.library
                .projects
                .iter()
                .filter(|item| {
                    self.library
                        .query
                        .to_lowercase()
                        .split_whitespace()
                        .all(|word| item.to_string().to_lowercase().contains(word))
                })
                .cloned()
                .map(|r| (r, 0, 0))
                .collect()
        } else if self.library.archive {
            self.library
                .records
                .iter()
                .filter(|r| self.library.visible(r))
                .cloned()
                .map(|r| (r, 0, 0))
                .collect()
        } else {
            hierarchy(&self.library.records, &self.library.expanded, |r| {
                self.library.visible(r)
            })
        };
        ui.weak(format!(
            "{} {}",
            visible.len(),
            if projects { "projects" } else { "records" }
        ));
        let mut selected = None;
        let mut archived = None;
        let writing = self.library_writing();
        for (record, depth, child_count) in &visible {
            let reference = text(record, "ref");
            let is_selected = self.library.selected == reference;
            egui::Frame::NONE
                .outer_margin(egui::Margin {
                    left: (*depth as i8).saturating_mul(18),
                    ..Default::default()
                })
                .fill(if is_selected {
                    Color32::from_rgb(53, 73, 87)
                } else {
                    Color32::from_rgb(40, 42, 45)
                })
                .inner_margin(egui::Margin::symmetric(6, 5))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        if *child_count > 0 {
                            let family =
                                reference.split('@').next().unwrap_or(reference).to_owned();
                            let expanded = self.library.expanded.contains(&family);
                            if ui
                                .small_button(if expanded { "-" } else { "+" })
                                .on_hover_text("Show or hide protein products")
                                .clicked()
                            {
                                if expanded {
                                    self.library.expanded.remove(&family);
                                } else {
                                    self.library.expanded.insert(family);
                                }
                            }
                        }
                        let reserved = if self.library.archive { 57. } else { 23. };
                        let width = (ui.available_width() - reserved - ui.spacing().item_spacing.x)
                            .max(24.);
                        let name = display_name(record);
                        let label = if name.is_empty() {
                            RichText::new("no alt name").small().italics().weak()
                        } else {
                            RichText::new(name).strong()
                        };
                        let (rect, response) =
                            ui.allocate_exact_size(Vec2::new(width, 24.), egui::Sense::click());
                        let mut label_ui = ui.new_child(
                            egui::UiBuilder::new()
                                .id_salt(("library-row-label", reference))
                                .max_rect(rect)
                                .layout(egui::Layout::left_to_right(egui::Align::Center)),
                        );
                        let label_response = label_ui.add(
                            egui::Label::new(label)
                                .truncate()
                                .sense(egui::Sense::click()),
                        );
                        if response.on_hover_text(reference).clicked()
                            || label_response.on_hover_text(reference).clicked()
                        {
                            selected =
                                Some((reference.to_owned(), text(record, "kind") == "project"));
                        }
                        if self.library.archive {
                            if ui
                                .add_enabled(!writing, egui::Button::new("Restore").small())
                                .on_hover_text("Restore to library")
                                .clicked()
                            {
                                archived = Some((record.clone(), false));
                            }
                        } else if icon_button(
                            ui,
                            Icon::Trash,
                            !writing,
                            "Archive; Undo restores this entry",
                        )
                        .clicked()
                        {
                            archived = Some((record.clone(), true));
                        }
                    });
                    ui.horizontal_wrapped(|ui| {
                        let id = presentation(record, "inventory_id");
                        if !id.is_empty() {
                            chip(ui, id, AMBER);
                        }
                        chip(ui, kind_label(record), Color32::from_rgb(145, 178, 198));
                        if *child_count > 0 {
                            ui.weak(format!(
                                "{} protein{}",
                                child_count,
                                if *child_count == 1 { "" } else { "s" }
                            ));
                        }
                        if text(record, "derivation_kind") == "derived" {
                            ui.weak("derived");
                        }
                        if let Some(length) = record["sequence_length"].as_u64() {
                            ui.weak(format!(
                                "{length} {}",
                                if text(record, "molecule_type") == "protein" {
                                    "aa"
                                } else {
                                    "nt"
                                }
                            ));
                        }
                        if projects {
                            ui.weak(format!(
                                "{} members",
                                record["member_count"].as_u64().unwrap_or(0)
                            ));
                        }
                    });
                    if *depth == 0 && !parent_reference(record).is_empty() {
                        ui.horizontal_wrapped(|ui| {
                            ui.weak("Parent outside this view");
                            if ui
                                .small_button("Open parent")
                                .on_hover_text(parent_reference(record))
                                .clicked()
                            {
                                selected = Some((parent_reference(record).to_owned(), false));
                            }
                        });
                    }
                    let (status, color) = review_label(record);
                    if status == "REVIEW REQUIRED" {
                        ui.label(RichText::new(status).small().color(color))
                            .on_hover_text(text(record, "review_reason"));
                    }
                });
            ui.add_space(2.);
        }
        if let Some((mut reference, is_project)) = selected {
            if is_project && !self.library.archive {
                self.library.open_project(&reference);
                self.library_navigate();
                reference = self.library.project.clone();
            }
            self.library_select(&reference);
        }
        if let Some((record, archive)) = archived {
            self.library_archive_record(&record, archive);
        }
        if let Some(offset) = self.library.next_offset {
            if ui
                .add_enabled(
                    !self.library_list_busy(),
                    egui::Button::new("Load more records"),
                )
                .clicked()
            {
                self.library_load_page(offset);
            }
            ui.small("Search includes loaded records; load more for the rest.");
        } else if visible.is_empty() && self.library.loaded {
            ui.weak(if projects {
                "No projects match."
            } else if self.library.archive {
                "The archive is empty."
            } else {
                "No constructs match."
            });
        }
    }

    pub(super) fn library_details(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        egui::Frame::NONE.inner_margin(12).show(ui, |ui| {
            ui.set_min_size(ui.available_size());
            let actions=self.library.sequence.show_dialogs(ui,self.library_writing(),&self.library.records);
            for action in actions {self.library_sequence_action(action);}
            if self.library.selected.is_empty() {
                ui.heading("Molecular library");
                ui.label("Choose a project or construct on the left to inspect its identity, purpose, and retained source files.");
                ui.weak("The head stores immutable revisions. Adding a record to Inputs preserves the selected revision.");
                return;
            }
            if !self.library.detail_error.is_empty() {
                ui.colored_label(RED, &self.library.detail_error);
                if ui.button("Retry record").clicked() { self.library_select(&self.library.selected.clone()); }
                return;
            }
            if self.library.detail.is_null() {
                ui.horizontal(|ui| { ui.spinner(); ui.label(format!("Loading {}…", self.library.selected)); });
                return;
            }
            let detail = self.library.detail.clone();
            let record = &detail["record"];
            self.library_detail_header(ui,&detail,ctx);
            if !self.library.write_error.is_empty() { ui.colored_label(RED,&self.library.write_error); }
            let review = &record["identity"]["product_review"];
            if !text(review, "status").is_empty() {
                let color = if text(review, "status") == "review_required" { AMBER } else { GREEN };
                ui.colored_label(color, text(review, "status").replace('_', " ").to_uppercase());
            }
            if matches!(text(record, "kind"), "construct" | "assembly") && text(&record["identity"],"molecular_form")!="plasmid" {
                let input = pinned_input(&detail, self.state.next_chain());
                ui.horizontal_wrapped(|ui| {
                    if ui.add_enabled(input.is_ok(), egui::Button::new(RichText::new("▶ Prepare prediction").strong().color(GREEN)).min_size(Vec2::new(180.,30.))).clicked()
                        && let Ok(input) = input.clone()
                    {
                        let id = input.id.clone();
                        self.state.inputs.push(input);
                        self.flash_input(&id);
                        self.sidebar_tab = 0;
                        self.library.added = format!("Added {} to the run composer.", text(&detail, "ref"));
                        self.log(self.library.added.clone());
                        self.persist();
                    }
                    if text(&record["identity"], "molecule_type") == "protein"
                        && ui.button("Design binders…").clicked()
                    {
                        self.binder_library_target(text(&detail, "ref"));
                    }
                    if let Err(reason) = input { ui.colored_label(AMBER, reason); }
                });
                if !self.library.added.is_empty() { ui.colored_label(GREEN, &self.library.added); }
            }
            ui.add_space(5.);
            ui.horizontal_wrapped(|ui| {
                for (index, label) in ["Purpose", "Sequence / identity", "Relationships", "Attachments", "Record JSON"].iter().enumerate() {
                    ui.selectable_value(&mut self.library.tab, index, *label);
                }
            });
            ui.separator();
            egui::ScrollArea::both().id_salt(("library-detail", self.library.selected.clone(), self.library.tab))
                .auto_shrink([false, false]).show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    self.library_edit_sequence(ui);
                    if self.library.tab==0 {self.library_run_controls(ui, &detail);}
                    match self.library.tab {
                        0 => self.library_purpose(ui, &detail, ctx),
                        1 => { self.library_identity(ui, &detail, ctx); self.library_run_controls(ui, &detail); },
                        2 => self.library_relations(ui, &detail),
                        3 => self.library_attachments(ui, &detail),
                        _ => {
                            if ui.button("Copy record JSON").clicked() { ctx.copy_text(serde_json::to_string_pretty(record).unwrap_or_default()); }
                            readonly(ui, &serde_json::to_string_pretty(record).unwrap_or_default());
                        }
                    }
                });
        });
    }

    fn library_detail_header(&mut self, ui: &mut egui::Ui, detail: &Value, ctx: &egui::Context) {
        let record = &detail["record"];
        let project = text(record, "kind") == "project";
        let field = if project { "name" } else { "alt_name" };
        let editable = detail["is_latest"] != false && !self.library_writing();
        let name = display_name(detail);
        let editing = self
            .library
            .edit
            .as_ref()
            .is_some_and(|edit| edit.field == field && edit.reference == text(detail, "ref"));
        let mut save = false;
        let mut cancel = false;
        ui.horizontal_wrapped(|ui| {
            if editing {
                let edit = self.library.edit.as_mut().unwrap();
                let response = ui.add_enabled(
                    editable,
                    egui::TextEdit::singleline(&mut edit.value)
                        .desired_width((ui.available_width() - 320.).clamp(160., 500.))
                        .hint_text(if project { "Project name" } else { "Alt name" }),
                );
                if edit.focus {
                    response.request_focus();
                    edit.focus = false;
                }
                save = ui
                    .add_enabled(
                        editable
                            && edit.value != edit.original
                            && (!project || !edit.value.trim().is_empty()),
                        egui::Button::new("Save"),
                    )
                    .clicked()
                    || (response.has_focus()
                        && ui.input(|input| input.key_pressed(egui::Key::Enter)));
                cancel = ui
                    .add_enabled(editable, egui::Button::new("Cancel"))
                    .clicked()
                    || (response.has_focus()
                        && ui.input(|input| input.key_pressed(egui::Key::Escape)));
            } else {
                let label = if name.is_empty() {
                    RichText::new("no alt name").size(13.).italics().weak()
                } else {
                    RichText::new(name).strong().size(20.)
                };
                if ui
                    .add(egui::Label::new(label).sense(egui::Sense::click()))
                    .on_hover_text(if editable {
                        "Double-click to edit"
                    } else {
                        "Open the current revision to edit"
                    })
                    .double_clicked()
                    && editable
                {
                    self.library_begin_edit(detail, field);
                }
            }
            let id = presentation(detail, "inventory_id");
            if !id.is_empty() {
                chip(ui, id, AMBER);
            }
            let modality = presentation(detail, "modality");
            if !modality.is_empty() {
                chip(ui, modality, Color32::from_rgb(145, 178, 198));
            }
            if detail["archived"] == true {
                chip(ui, "archived", AMBER);
                if ui
                    .add_enabled(editable, egui::Button::new("Restore"))
                    .clicked()
                {
                    self.library_archive_record(detail, false);
                }
            } else if icon_button(
                ui,
                Icon::Trash,
                editable,
                "Archive; Undo restores this entry",
            )
            .clicked()
            {
                self.library_archive_record(detail, true);
            }
            if !text(&record["identity"], "sequence").is_empty()
                && icon_button(
                    ui,
                    Icon::Pencil,
                    editable,
                    "Edit nucleotide or amino-acid sequence",
                )
                .clicked()
            {
                self.library_begin_edit(detail, "sequence");
            }
        });
        if cancel {
            self.library.edit = None;
        } else if save {
            self.library_save_edit();
        }
        let verbose = presentation(detail, "verbose_name");
        if !project && !verbose.is_empty() {
            ui.horizontal_wrapped(|ui| {
                chip(ui, verbose, Color32::from_rgb(172, 182, 191));
            });
        }
        ui.horizontal_wrapped(|ui| {
            ui.monospace(text(detail, "ref"));
            if ui.button("Copy ref").clicked() {
                ctx.copy_text(text(detail, "ref").into());
            }
            let mut revision = text(detail, "ref").to_owned();
            egui::ComboBox::from_id_salt("library-revision")
                .selected_text(format!("Revision {}", record["revision"]))
                .show_ui(ui, |ui| {
                    for item in rows(detail, "revisions") {
                        let reference = item.as_str().unwrap_or_else(|| text(item, "ref"));
                        ui.selectable_value(&mut revision, reference.to_owned(), reference);
                    }
                });
            if revision != text(detail, "ref") {
                self.library_select(&revision);
            }
            if detail["is_latest"] == false && ui.link("Open current revision to edit").clicked() {
                self.library_select(text(detail, "latest_ref"));
            }
        });
    }

    fn library_edit_sequence(&mut self, ui: &mut egui::Ui) {
        let writing = self.library_writing();
        let Some(edit) = self
            .library
            .edit
            .as_mut()
            .filter(|edit| edit.field == "sequence")
        else {
            return;
        };
        let (save, cancel) = Self::sequence_editor(ui, edit, writing);
        if cancel {
            self.library.edit = None;
        } else if save {
            self.library_save_edit();
        }
    }

    fn sequence_editor(ui: &mut egui::Ui, edit: &mut Edit, writing: bool) -> (bool, bool) {
        let mut save = false;
        let mut cancel = false;
        egui::Frame::group(ui.style()).show(ui,|ui| {
            Self::section(ui,"EDIT SEQUENCE");
            ui.weak("Save creates a new revision. Previous annotations remain original source evidence, and existing runs retain their original input.");
            ui.weak("Enter the exact uppercase sequence without spaces or line breaks. Modified polymers require a compatible residue mapping.");
            ui.horizontal(|ui| {
                save=ui.add_enabled(!writing && !edit.value.is_empty() && edit.value!=edit.original,egui::Button::new("Save sequence")).clicked();
                cancel=ui.add_enabled(!writing,egui::Button::new("Cancel")).clicked();
                ui.weak(format!("{} characters",edit.value.len()));
            });
            egui::ScrollArea::vertical()
                .id_salt(("edit-sequence-scroll", &edit.reference))
                .max_height(220.)
                .auto_shrink([false, true])
                .show(ui, |ui| {
                    let response=ui.add_enabled(!writing,egui::TextEdit::multiline(&mut edit.value).font(egui::TextStyle::Monospace).desired_width(f32::INFINITY).desired_rows(7));
                    if edit.focus { response.request_focus();edit.focus=false; }
                });
        });
        (save, cancel)
    }

    fn library_purpose(&mut self, ui: &mut egui::Ui, detail: &Value, ctx: &egui::Context) {
        let description = &detail["description"];
        let document = edit_value(detail, "description");
        let project = text(&detail["record"], "kind") == "project";
        let editing = self.library.edit.as_ref().is_some_and(|edit| {
            edit.field == "description" && edit.reference == text(detail, "ref")
        });
        let writing = self.library_writing();
        let editable = detail["is_latest"] != false && !writing && self.library.edit.is_none();
        let mut begin_edit = false;
        ui.horizontal_wrapped(|ui| {
            ui.label(
                RichText::new(if project {
                    "PROJECT BRIEF"
                } else {
                    "CONSTRUCT PURPOSE"
                })
                .strong()
                .color(AMBER),
            );
            if !editing {
                ui.checkbox(&mut self.library.source_document, "Markdown source");
            }
            let (copy, pencil) = document_buttons(ui, project, editable);
            if copy.clicked() {
                let value = self
                    .library
                    .edit
                    .as_ref()
                    .filter(|_| editing)
                    .map_or(document, |edit| edit.value.as_str());
                ctx.copy_text(value.into());
            }
            begin_edit = pencil.is_some_and(|pencil| pencil.clicked());
        });
        if begin_edit {
            self.library_begin_edit(detail, "description");
        }
        if let Some(edit) = self
            .library
            .edit
            .as_mut()
            .filter(|edit| edit.field == "description" && edit.reference == text(detail, "ref"))
        {
            let (save, cancel) = document_editor(ui, edit, writing);
            if cancel {
                self.library.edit = None;
                self.library.write_error.clear();
            } else if save {
                self.library_save_edit();
            }
        } else {
            if description["incomplete"] == true
                || document.contains("bio-library:purpose-scaffold:v1 incomplete")
            {
                ui.colored_label(
                    AMBER,
                    "Purpose document is incomplete. This record still needs manual context.",
                );
            }
            if document.is_empty() {
                ui.weak("No purpose document is attached to this revision.");
            } else if self.library.source_document {
                readonly(ui, document);
            } else {
                markdown(ui, document);
            }
        }
        if !rows(detail, "members").is_empty() {
            ui.add_space(10.);
            Self::section(ui, "PROJECT MEMBERS / PINNED REVISIONS");
            let mut selected = None;
            for member in rows(detail, "members") {
                ui.horizontal_wrapped(|ui| {
                    let reference = text(member, "source_ref");
                    if ui.link(reference).clicked() {
                        selected = Some(reference.to_owned());
                    }
                    let name = display_name(member);
                    ui.label(if name.is_empty() { "no alt name" } else { name });
                    let (label, color) = review_label(member);
                    if !label.is_empty() {
                        ui.colored_label(color, label.replace('_', " "));
                    }
                });
                if !text(member, "role").is_empty() {
                    ui.weak(text(member, "role"));
                }
                ui.separator();
            }
            if let Some(reference) = selected {
                self.library_select(&reference);
            }
        }
    }

    fn library_identity(&mut self, ui: &mut egui::Ui, detail: &Value, _ctx: &egui::Context) {
        let record = &detail["record"];
        let identity = &record["identity"];
        egui::CollapsingHeader::new("Identity, aliases and provenance").show(ui, |ui| {
            Self::section(ui, "IDENTITY METADATA");
            let mut metadata = identity.clone();
            if let Some(object) = metadata.as_object_mut() {
                object.remove("sequence");
            }
            readonly(
                ui,
                &serde_json::to_string_pretty(&metadata).unwrap_or_default(),
            );
            Self::section(ui, "ALIASES & TAGS");
            for field in ["aliases", "tags"] {
                ui.label(format!(
                    "{}: {}",
                    field,
                    rows(record, field)
                        .iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            }
            if !text(record, "notes").is_empty() {
                ui.label(text(record, "notes"));
            }
            Self::section(ui, "PROVENANCE");
            readonly(
                ui,
                &serde_json::to_string_pretty(&record["provenance"]).unwrap_or_default(),
            );
        });
        let actions =
            self.library
                .sequence
                .show(ui, detail, self.library_writing(), &self.library.records);
        for action in actions {
            self.library_sequence_action(action);
        }
        let sequence = text(&self.library.sequence.view, "sequence");
        if !sequence.is_empty() && text(identity, "molecule_type") != "protein" {
            egui::CollapsingHeader::new("Raw sequence / numbered positions")
                .show(ui, |ui| readonly(ui, &numbered_sequence(sequence)));
        }
    }

    fn library_relations(&mut self, ui: &mut egui::Ui, detail: &Value) {
        let mut selected = None;
        Self::section(ui, "MOLECULAR & SOURCE RELATIONSHIPS");
        for relation in rows(detail, "relations") {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(text(relation, "relation").replace('_', " ")).color(AMBER));
                if ui.link(text(relation, "ref")).clicked() {
                    selected = Some(text(relation, "ref").to_owned());
                }
                ui.weak(text(relation, "label"));
            });
        }
        if rows(detail, "relations").is_empty() {
            ui.weak("No source or component relationships are recorded.");
        }
        Self::section(ui, "PROJECT MEMBERSHIP");
        for project in rows(detail, "projects") {
            ui.horizontal_wrapped(|ui| {
                if ui.link(text(project, "ref")).clicked() {
                    selected = Some(text(project, "ref").to_owned());
                }
                ui.label(text(project, "name"));
            });
        }
        if rows(detail, "projects").is_empty() {
            ui.weak("No project revision lists this exact record.");
        }
        Self::section(ui, "REVISION HISTORY");
        for revision in rows(detail, "revisions") {
            let reference = revision.as_str().unwrap_or_else(|| text(revision, "ref"));
            ui.horizontal_wrapped(|ui| {
                if ui
                    .selectable_label(reference == self.library.selected, reference)
                    .clicked()
                {
                    selected = Some(reference.into());
                }
                ui.weak(text(revision, "created_at"));
            });
        }
        if let Some(reference) = selected {
            self.library_select(&reference);
        }
    }

    fn library_attachments(&mut self, ui: &mut egui::Ui, detail: &Value) {
        ui.weak("Download the original retained bytes. Size and SHA-256 are verified against this revision before export.");
        let Some(attachments) = detail["record"]["attachments"].as_array() else {
            ui.label("No attachments.");
            return;
        };
        let mut requested = None;
        for receipt in attachments {
            let name = text(receipt, "path")
                .strip_prefix("attachments/")
                .unwrap_or("");
            if name.is_empty() {
                continue;
            }
            ui.push_id(name, |ui| {
                ui.separator();
                ui.horizontal_wrapped(|ui| {
                    let purpose =
                        Purpose::LibraryAttachment(text(detail, "ref").into(), name.to_owned());
                    if ui
                        .add_enabled(!self.busy(&purpose), egui::Button::new("Save…"))
                        .clicked()
                    {
                        requested = Some((name.to_owned(), receipt.clone()));
                    }
                    ui.label(RichText::new(name).strong());
                    ui.weak(format!("{} bytes", receipt["bytes"].as_u64().unwrap_or(0)));
                    if let Some(pending) = self
                        .pending
                        .values()
                        .find(|pending| pending.purpose == purpose)
                    {
                        ui.spinner();
                        if pending.total > 0 {
                            ui.small(format!("{} / {} bytes", pending.done, pending.total));
                        }
                    }
                });
                ui.monospace(text(receipt, "sha256"));
            });
        }
        if let Some((name, receipt)) = requested {
            let reference = text(detail, "ref");
            if let Some(session) = self.session.as_mut() {
                match session.library_attachment(reference, &name, &receipt) {
                    Ok(id) => {
                        self.pending.insert(
                            id,
                            Pending {
                                purpose: Purpose::LibraryAttachment(reference.into(), name.clone()),
                                label: format!("Library attachment {name}"),
                                done: 0,
                                total: receipt["bytes"].as_u64().unwrap_or(0),
                            },
                        );
                    }
                    Err(error) => self.log(error.to_string()),
                }
            }
        }
    }

    pub(super) fn library_received_attachment(
        &mut self,
        reference: &str,
        name: &str,
        value: Value,
        ctx: &egui::Context,
    ) {
        let metadata = &value["metadata"];
        if text(metadata, "ref") != reference || text(metadata, "name") != name {
            self.log("Library export rejected: the retained attachment identity changed.");
            return;
        }
        let source = PathBuf::from(text(&value, "local_path"));
        let name = name.to_owned();
        let metadata = metadata.clone();
        let sender = self.ui_tx.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            if let Some(destination) = rfd::FileDialog::new().set_file_name(&name).save_file() {
                let result = (|| {
                    let (size, sha) = rpc::file_hash(&source).map_err(|e| e.to_string())?;
                    if Some(size) != metadata["size"].as_u64() || sha != text(&metadata, "sha256") {
                        return Err("Verified library cache changed before export.".into());
                    }
                    std::fs::copy(source, &destination).map_err(|e| e.to_string())?;
                    Ok(destination)
                })();
                let _ = sender.send(UiEvent::Exported(result));
                ctx.request_repaint();
            }
        });
    }
}

/// Presentation fields are distinct from immutable source identity. Empty alt names
/// deliberately remain empty rather than falling back to a verbose source name.
fn presentation<'a>(value: &'a Value, field: &str) -> &'a str {
    if let Some(text) = value.get(field).and_then(Value::as_str) {
        return text;
    }
    if let Some(text) = value["record"].get(field).and_then(Value::as_str) {
        return text;
    }
    ""
}

fn display_name(value: &Value) -> &str {
    let record = if value["record"].is_object() {
        &value["record"]
    } else {
        value
    };
    if text(record, "kind") == "project" {
        text(record, "name")
    } else {
        presentation(value, "alt_name")
    }
}

fn chip(ui: &mut egui::Ui, label: &str, color: Color32) {
    egui::Frame::NONE
        .fill(Color32::from_rgb(31, 35, 39))
        .stroke(egui::Stroke::new(1., Color32::from_rgb(70, 77, 84)))
        .corner_radius(3)
        .inner_margin(egui::Margin::symmetric(5, 2))
        .show(ui, |ui| {
            ui.add(
                egui::Label::new(RichText::new(label).small().color(color)).wrap_mode(
                    if label.len() < 50 {
                        egui::TextWrapMode::Extend
                    } else {
                        egui::TextWrapMode::Wrap
                    },
                ),
            );
        });
}

#[derive(Clone, Copy)]
enum Icon {
    Trash,
    Pencil,
}

fn document_buttons(
    ui: &mut egui::Ui,
    project: bool,
    editable: bool,
) -> (egui::Response, Option<egui::Response>) {
    ui.scope(|ui| {
        // Text and drawn-icon buttons share one height, including custom font sizes.
        let height = (ui.text_style_height(&egui::TextStyle::Button)
            + 2. * ui.spacing().button_padding.y)
            .max(ui.spacing().interact_size.y)
            .max(23.);
        ui.spacing_mut().interact_size.y = height;
        let copy = ui.add(egui::Button::new("Copy document").min_size(Vec2::new(0., height)));
        let pencil = project.then(|| {
            icon_button(
                ui,
                Icon::Pencil,
                editable,
                "Edit project description (current revision)",
            )
        });
        (copy, pencil)
    })
    .inner
}

fn document_editor(ui: &mut egui::Ui, edit: &mut Edit, writing: bool) -> (bool, bool) {
    let mut save = false;
    let mut cancel = false;
    egui::Frame::group(ui.style()).show(ui, |ui| {
        ui.horizontal(|ui| {
            save = ui
                .add_enabled(
                    !writing && edit.can_save(),
                    egui::Button::new("Save document"),
                )
                .clicked();
            cancel = ui
                .add_enabled(!writing, egui::Button::new("Cancel"))
                .clicked();
            if writing {
                ui.spinner();
                ui.weak("Saving…");
            } else {
                ui.weak("Markdown · saves a new revision");
            }
        });
        if edit.value.len() > MAX_DOCUMENT_BYTES {
            ui.colored_label(
                RED,
                "Project descriptions must be at most 1 MiB of UTF-8 text.",
            );
        }
        egui::ScrollArea::vertical()
            .id_salt(("edit-document-scroll", &edit.reference))
            .max_height(320.)
            .auto_shrink([false, true])
            .show(ui, |ui| {
                let response = ui.add_enabled(
                    !writing,
                    egui::TextEdit::multiline(&mut edit.value)
                        .id_salt(("edit-project-document", &edit.reference))
                        .font(egui::TextStyle::Monospace)
                        .desired_width(f32::INFINITY)
                        .desired_rows(12),
                );
                if edit.focus {
                    response.request_focus();
                    edit.focus = false;
                }
            });
    });
    (save, cancel)
}

fn icon_button(ui: &mut egui::Ui, icon: Icon, enabled: bool, tooltip: &str) -> egui::Response {
    let response = ui
        .add_enabled(enabled, egui::Button::new("").min_size(Vec2::splat(23.)))
        .on_hover_text(tooltip);
    let center = response.rect.center();
    let stroke = egui::Stroke::new(
        1.4,
        if enabled {
            Color32::from_gray(200)
        } else {
            Color32::from_gray(85)
        },
    );
    let point = |x, y| center + Vec2::new(x, y);
    match icon {
        Icon::Trash => {
            ui.painter()
                .line_segment([point(-5., -4.), point(5., -4.)], stroke);
            ui.painter()
                .line_segment([point(-2., -6.), point(2., -6.)], stroke);
            ui.painter().add(egui::Shape::line(
                vec![
                    point(-4., -2.),
                    point(-3., 6.),
                    point(3., 6.),
                    point(4., -2.),
                ],
                stroke,
            ));
            for x in [-1.3, 1.3] {
                ui.painter()
                    .line_segment([point(x, -1.), point(x, 4.)], stroke);
            }
        }
        Icon::Pencil => {
            ui.painter().add(egui::Shape::closed_line(
                vec![
                    point(-5., 3.),
                    point(2., -4.),
                    point(5., -1.),
                    point(-2., 6.),
                    point(-6., 7.),
                ],
                stroke,
            ));
            ui.painter()
                .line_segment([point(0., -2.), point(3., 1.)], stroke);
        }
    }
    response
}

fn readonly(ui: &mut egui::Ui, value: &str) {
    let mut view = value;
    ui.add(
        egui::TextEdit::multiline(&mut view)
            .font(egui::TextStyle::Monospace)
            .desired_width(f32::INFINITY)
            .desired_rows(value.lines().count().clamp(2, 25)),
    );
}

pub(super) fn wrapped_sequence(sequence: &str) -> String {
    sequence
        .as_bytes()
        .chunks(80)
        .map(|line| String::from_utf8_lossy(line))
        .collect::<Vec<_>>()
        .join("\n")
}

fn numbered_sequence(sequence: &str) -> String {
    sequence
        .as_bytes()
        .chunks(80)
        .enumerate()
        .map(|(index, line)| format!("{:>7}  {}", index * 80 + 1, String::from_utf8_lossy(line)))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Small native Markdown presentation. The source view always exposes exact bytes;
/// HTML is never evaluated, and document text never executes commands or requests.
fn markdown(ui: &mut egui::Ui, source: &str) {
    let mut code = false;
    let mut comment = false;
    let lines: Vec<_> = source.lines().collect();
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index];
        let start = index;
        index += 1;
        let trimmed = line.trim();
        if trimmed.starts_with("<!--") {
            comment = true;
        }
        if comment {
            if trimmed.contains("-->") {
                comment = false;
            }
            continue;
        }
        if trimmed.starts_with("```") {
            code = !code;
            continue;
        }
        if code {
            ui.monospace(line);
            continue;
        }
        if trimmed.is_empty() {
            ui.add_space(5.);
            continue;
        }
        if let Some((end, table)) = markdown_table(&lines, start) {
            let columns = table[0].len();
            let width =
                ((ui.available_width() - (columns - 1) as f32 * 12.) / columns as f32).max(70.);
            egui::Grid::new(("purpose-table", start))
                .num_columns(columns)
                .striped(true)
                .min_col_width(width)
                .max_col_width(width)
                .spacing([12., 8.])
                .show(ui, |ui| {
                    for (row, cells) in table.iter().enumerate() {
                        for cell in cells {
                            if row == 0 {
                                ui.label(RichText::new(*cell).strong().color(AMBER));
                            } else {
                                ui.add(egui::Label::new(inline_markdown(cell)).wrap());
                            }
                        }
                        ui.end_row();
                    }
                });
            ui.add_space(6.);
            index = end;
            continue;
        }
        let level = trimmed.bytes().take_while(|byte| *byte == b'#').count();
        if (1..=6).contains(&level) && trimmed.as_bytes().get(level) == Some(&b' ') {
            ui.add_space(5.);
            ui.label(
                RichText::new(&trimmed[level + 1..])
                    .strong()
                    .size(if level < 3 { 15. } else { 12.5 })
                    .color(AMBER),
            );
        } else if matches!(trimmed, "---" | "***" | "___") {
            ui.separator();
        } else {
            let display = if let Some(tail) = trimmed
                .strip_prefix("- ")
                .or_else(|| trimmed.strip_prefix("* "))
            {
                format!("• {tail}")
            } else {
                trimmed.to_owned()
            };
            ui.label(inline_markdown(&display));
        }
    }
}

fn markdown_table<'a>(lines: &[&'a str], start: usize) -> Option<(usize, Vec<Vec<&'a str>>)> {
    fn cells(line: &str) -> Vec<&str> {
        line.trim()
            .trim_matches('|')
            .split('|')
            .map(str::trim)
            .collect()
    }
    let header = cells(lines.get(start)?);
    if !(2..=8).contains(&header.len()) {
        return None;
    }
    let separators = cells(lines.get(start + 1)?);
    if separators.len() != header.len()
        || !separators.iter().all(|cell| {
            let dashes = cell.trim_matches(':');
            dashes.len() >= 3 && dashes.bytes().all(|byte| byte == b'-')
        })
    {
        return None;
    }
    let mut table = vec![header];
    let mut end = start + 2;
    while let Some(line) = lines.get(end) {
        let row = cells(line);
        if row.len() != table[0].len() {
            break;
        }
        table.push(row);
        end += 1;
    }
    Some((end, table))
}

fn inline_markdown(value: &str) -> egui::text::LayoutJob {
    let mut job = egui::text::LayoutJob::default();
    let mut rest = value;
    let mut strong = false;
    let mut code = false;
    while !rest.is_empty() {
        if rest.starts_with("**") && !code {
            strong = !strong;
            rest = &rest[2..];
            continue;
        }
        if rest.starts_with('`') {
            code = !code;
            rest = &rest[1..];
            continue;
        }
        let end = rest
            .char_indices()
            .skip(1)
            .find_map(|(index, _)| {
                let tail = &rest[index..];
                (tail.starts_with('`') || (tail.starts_with("**") && !code)).then_some(index)
            })
            .unwrap_or(rest.len());
        let font = if code {
            egui::FontId::monospace(11.5)
        } else {
            egui::FontId::proportional(12.)
        };
        job.append(
            &rest[..end],
            0.,
            egui::TextFormat {
                font_id: font,
                color: if strong {
                    Color32::WHITE
                } else {
                    Color32::from_gray(219)
                },
                background: if code {
                    Color32::from_rgb(26, 28, 30)
                } else {
                    Color32::TRANSPARENT
                },
                ..Default::default()
            },
        );
        rest = &rest[end..];
    }
    job
}

#[cfg(test)]
mod tests {
    use super::*;

    fn project_detail(reference: &str, document: &str) -> Value {
        json!({"ref":reference,"sha256":"a".repeat(64),"is_latest":true,
            "record":{"kind":"project","name":"Project","identity":{}},
            "description":{"text":document,"sha256":"b".repeat(64),"path":"attachments/project.md"}})
    }

    #[test]
    fn project_document_save_preserves_markdown_and_record_revision_precondition() {
        let detail = project_detail("project:example@7", "# Original\n\nProject purpose.\n");
        let mut edit = Edit::new(&detail, "description");
        assert_eq!(edit.value, detail["description"]["text"]);
        assert!(edit.save_params().is_none(), "unchanged text is a no-op");
        let markdown = "# Updated purpose\r\n\r\n- Low pH ≤ 2\r\n- α / β\r\n\r\n```text\n  preserved whitespace  \n```\n";
        edit.value = markdown.into();
        assert_eq!(
            edit.save_params().unwrap(),
            json!({"ref":"project:example@7","expected_sha256":"a".repeat(64),
                "patch":{"description":markdown}})
        );
        edit.value.clear();
        assert_eq!(edit.save_params().unwrap()["patch"]["description"], "");
        edit.sha256.clear();
        assert!(
            edit.save_params().is_none(),
            "wait for the new revision digest"
        );
    }

    #[test]
    fn project_document_limit_counts_utf8_bytes_and_missing_attachment_can_be_added() {
        let detail = json!({"ref":"project:empty@1","sha256":"a".repeat(64),
            "record":{"kind":"project"},"description":null});
        let mut edit = Edit::new(&detail, "description");
        assert_eq!(edit.value, "");
        edit.value = "é".repeat(MAX_DOCUMENT_BYTES / 2);
        assert!(edit.can_save());
        edit.value.push('é');
        assert!(edit.save_params().is_none());
        edit.value = "New project description".into();
        assert!(edit.can_save());
    }

    #[test]
    fn project_document_recovery_retains_new_typing_and_refreshes_its_saved_baseline() {
        let before = "project:example@1";
        let after = "project:example@2";
        let mut edit = Edit::new(&project_detail(before, "Original"), "description");
        edit.value = "New typing\n".into();
        let mut explorer = Explorer {
            selected: before.into(),
            edit: Some(edit),
            ..Default::default()
        };
        let changed = json!({"changed_refs":[{"before_ref":before,"after_ref":after}]});
        explorer.reconcile_edit(
            &changed,
            &json!({"ref":before,"patch":{"description":"Earlier submitted draft"}}),
        );
        let pending = explorer.edit.as_ref().unwrap();
        assert_eq!(pending.reference, after);
        assert_eq!(pending.value, "New typing\n");
        assert!(pending.save_params().is_none());
        explorer.selected = after.into();
        let mut received = project_detail(after, "Earlier submitted draft");
        received["sha256"] = json!("c".repeat(64));
        assert!(explorer.accept_detail(after, received));
        let retained = explorer.edit.as_ref().unwrap();
        assert_eq!(retained.original, "Earlier submitted draft");
        assert_eq!(retained.value, "New typing\n");
        let params = retained.save_params().unwrap();
        assert_eq!(params["expected_sha256"], "c".repeat(64));
        explorer.reconcile_edit(
            &json!({"changed_refs":[{"before_ref":after,"after_ref":"project:example@3"}]}),
            &params,
        );
        assert!(
            explorer.edit.is_none(),
            "successful current draft closes the editor"
        );
    }

    #[test]
    fn copy_document_and_pencil_share_height_at_regular_and_large_font_sizes() {
        for font_size in [14., 26.] {
            let context = egui::Context::default();
            context.style_mut(|style| {
                style.text_styles.insert(
                    egui::TextStyle::Button,
                    egui::FontId::proportional(font_size),
                );
            });
            let _ = context.run(egui::RawInput::default(), |context| {
                egui::CentralPanel::default().show(context, |ui| {
                    ui.horizontal_wrapped(|ui| {
                        let (copy, pencil) = document_buttons(ui, true, true);
                        let pencil = pencil.unwrap();
                        assert!((copy.rect.height() - pencil.rect.height()).abs() < 0.1);
                        assert!((copy.rect.center().y - pencil.rect.center().y).abs() < 0.1);
                        assert!(pencil.rect.left() > copy.rect.right());
                        assert!(pencil.enabled());
                    });
                    ui.horizontal(|ui| {
                        let (_, pencil) = document_buttons(ui, true, false);
                        assert!(!pencil.unwrap().enabled());
                    });
                    ui.horizontal(|ui| {
                        assert!(document_buttons(ui, false, true).1.is_none());
                    });
                });
            });
        }
    }

    #[test]
    fn long_project_document_keeps_save_and_cancel_above_scrolling_markdown() {
        let context = egui::Context::default();
        let mut edit = Edit::new(
            &project_detail("project:large@1", "Original"),
            "description",
        );
        edit.value = "Research goals and constraints.\n\n".repeat(500);
        edit.focus = false;
        let output = context.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    Vec2::new(640., 420.),
                )),
                ..Default::default()
            },
            |context| {
                egui::CentralPanel::default().show(context, |ui| {
                    let top = ui.cursor().top();
                    assert_eq!(document_editor(ui, &mut edit, false), (false, false));
                    assert!(ui.cursor().top() - top < 380.);
                });
            },
        );
        fn find(shape: &egui::Shape, label: &str) -> Option<f32> {
            match shape {
                egui::Shape::Text(text) if text.galley.job.text == label => Some(text.pos.y),
                egui::Shape::Vec(shapes) => shapes.iter().find_map(|shape| find(shape, label)),
                _ => None,
            }
        }
        for label in ["Save document", "Cancel"] {
            let y = output
                .shapes
                .iter()
                .find_map(|shape| find(&shape.shape, label))
                .expect("document action is rendered");
            assert!(y < 80., "{label} stays above the long document");
        }
        assert_eq!(
            edit.value,
            "Research goals and constraints.\n\n".repeat(500)
        );
    }

    #[test]
    fn long_sequence_editor_keeps_actions_above_bounded_text() {
        let context = egui::Context::default();
        let mut edit = Edit {
            reference: "construct:large-plasmid@1".into(),
            sha256: "digest".into(),
            field: "sequence".into(),
            value: "ATGC".repeat(4000),
            original: "ATGC".into(),
            focus: false,
        };
        let output = context.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    Vec2::new(640., 420.),
                )),
                ..Default::default()
            },
            |context| {
                egui::CentralPanel::default().show(context, |ui| {
                    let top = ui.cursor().top();
                    assert_eq!(
                        Workbench::sequence_editor(ui, &mut edit, false),
                        (false, false)
                    );
                    assert!(
                        ui.cursor().top() - top < 400.,
                        "a large plasmid must not push the actions outside the details viewport"
                    );
                });
            },
        );
        fn find(shape: &egui::Shape, label: &str) -> Option<f32> {
            match shape {
                egui::Shape::Text(text) if text.galley.job.text == label => Some(text.pos.y),
                egui::Shape::Vec(shapes) => shapes.iter().find_map(|shape| find(shape, label)),
                _ => None,
            }
        }
        for label in ["Save sequence", "Cancel"] {
            let y = output
                .shapes
                .iter()
                .find_map(|shape| find(&shape.shape, label))
                .expect("sequence action is rendered");
            assert!(
                y < 180.,
                "{label} remains above the independently scrolling text"
            );
        }
        assert_eq!(edit.value.len(), 16000);
    }

    #[test]
    fn refreshed_project_index_advances_navigation_only_within_the_same_family() {
        assert!(project_in_index(
            "project:example@1",
            &[json!({"ref":"project:example@2"})]
        ));
        assert!(!project_in_index("project:example@1", &[]));
        assert!(!project_in_index(
            "project:example@1",
            &[json!({"ref":"project:other@1"})]
        ));
        assert_eq!(
            latest_project(
                "project:example@1",
                &[
                    json!({"ref":"project:other@99"}),
                    json!({"ref":"project:example@2"}),
                    json!({"ref":"project:example@10"}),
                ]
            ),
            Some("project:example@10".into())
        );
        assert!(
            latest_project("project:example@10", &[json!({"ref":"project:example@2"})]).is_none()
        );
        assert!(latest_project("", &[json!({"ref":"project:example@2"})]).is_none());
    }

    fn detail() -> Value {
        json!({"ref":"construct:protein@2","record":{"kind":"construct","id":"protein","revision":2,"name":"Protein", "identity":{"molecule_type":"protein","sequence":"ACDE"}},"submission":{"allowed":true}})
    }

    #[test]
    fn curated_names_never_fall_back_to_verbose_names_or_ids() {
        let mut value = json!({"ref":"construct:example@1","kind":"construct","alt_name":"","name":"pGC077 — long source name (protein product)","verbose_name":"Long source name","inventory_id":"pGC077","modality":"protein"});
        assert_eq!(display_name(&value), "");
        assert_eq!(presentation(&value, "inventory_id"), "pGC077");
        assert_eq!(kind_label(&value), "protein");
        value["alt_name"] = json!("Short name");
        assert_eq!(display_name(&value), "Short name");
        let detail = json!({"record":value,"alt_name":"","verbose_name":"Long source name"});
        assert_eq!(display_name(&detail), "");
        assert_eq!(presentation(&detail, "verbose_name"), "Long source name");
        assert_eq!(
            display_name(&json!({"record":{"kind":"project","name":"Shared objective"}})),
            "Shared objective"
        );
    }

    #[test]
    fn unrelated_writes_and_recovered_older_saves_preserve_new_typing() {
        let reference = "construct:example@1";
        let edit = Edit {
            reference: reference.into(),
            sha256: "a".repeat(64),
            field: "alt_name".into(),
            value: "New typing".into(),
            original: "Original".into(),
            focus: false,
        };
        let mut explorer = Explorer {
            selected: reference.into(),
            edit: Some(edit.clone()),
            ..Default::default()
        };
        let other = json!({"changed_refs":[{"before_ref":"construct:other@1","after_ref":"construct:other@2"}]});
        explorer.reconcile_edit(
            &other,
            &json!({"ref":"construct:other@1","patch":{"archived":true}}),
        );
        assert_eq!(explorer.edit.as_ref().unwrap().value, "New typing");
        assert_eq!(explorer.edit.as_ref().unwrap().sha256, "a".repeat(64));
        let changed =
            json!({"changed_refs":[{"before_ref":reference,"after_ref":"construct:example@2"}]});
        explorer.reconcile_edit(
            &changed,
            &json!({"ref":reference,"patch":{"alt_name":"Older submitted value"}}),
        );
        assert_eq!(explorer.edit.as_ref().unwrap().value, "New typing");
        assert_eq!(
            explorer.edit.as_ref().unwrap().reference,
            "construct:example@2"
        );
        assert!(explorer.edit.as_ref().unwrap().sha256.is_empty());
        explorer.selected = "construct:example@2".into();
        assert!(explorer.accept_detail("construct:example@2",json!({"ref":"construct:example@2","record":{},"alt_name":"Older submitted value","sha256":"b".repeat(64)})));
        assert_eq!(
            explorer.edit.as_ref().unwrap().original,
            "Older submitted value"
        );
        assert_eq!(explorer.edit.as_ref().unwrap().value, "New typing");
        assert_eq!(explorer.edit.as_ref().unwrap().sha256, "b".repeat(64));
        explorer.edit = Some(edit);
        explorer.reconcile_edit(
            &changed,
            &json!({"ref":reference,"patch":{"alt_name":"New typing"}}),
        );
        assert!(explorer.edit.is_none());
    }

    #[test]
    fn opening_an_old_saved_explorer_starts_at_projects_without_stale_kind_filters() {
        let extras = BTreeMap::from([(
            "library_explorer".into(),
            json!({"selected":"construct:old@1","project":"project:old@1","kind":"project"}),
        )]);
        let explorer = Explorer::restore(&extras);
        assert!(explorer.project.is_empty());
        assert!(explorer.selected.is_empty());
        assert!(explorer.kind.is_empty());
        assert!(!explorer.archive);
    }

    #[test]
    fn composer_uses_exact_record_and_explicit_permission() {
        let mut value = detail();
        let input = pinned_input(&value, "B".into()).unwrap();
        assert_eq!(
            input.source,
            json!({"kind":"library","ref":"construct:protein@2"})
        );
        assert_eq!(input.chain_id, "B");
        value["submission"] = json!({"allowed":false,"reason":"Protein product requires review"});
        assert_eq!(
            pinned_input(&value, "B".into()).err().unwrap(),
            "Protein product requires review"
        );
        value["submission"] = Value::Null;
        assert!(pinned_input(&value, "B".into()).is_err());
    }

    #[test]
    fn composer_rejects_floating_mismatched_and_nonmolecular_refs() {
        for reference in [
            "construct:protein",
            "construct:protein@1",
            "construct:other@2",
        ] {
            let mut value = detail();
            value["ref"] = json!(reference);
            assert!(pinned_input(&value, "A".into()).is_err());
        }
        let mut value = detail();
        value["record"]["kind"] = json!("project");
        value["ref"] = json!("project:protein@2");
        assert!(pinned_input(&value, "A".into()).is_err());
    }

    #[test]
    fn late_detail_cannot_replace_current_selection() {
        let mut explorer = Explorer {
            selected: "construct:other@1".into(),
            ..Default::default()
        };
        assert!(!explorer.accept_detail("construct:protein@2", detail()));
        assert!(explorer.detail.is_null());
        explorer.selected = "construct:protein@2".into();
        assert!(explorer.accept_detail("construct:protein@2", detail()));
        assert!(!explorer.accept_detail(
            "construct:protein@2",
            json!({"ref":"construct:other@1","record":{}})
        ));
    }

    #[test]
    fn search_includes_aliases_and_preserves_review_candidates() {
        let record = json!({"ref":"construct:editor@1","kind":"construct","molecule_type":"protein","aliases":["pGC009"],"review_status":"review_required"});
        let mut explorer = Explorer {
            query: "pgc009 protein".into(),
            review_only: true,
            ..Default::default()
        };
        assert!(explorer.visible(&record));
        explorer.molecule = "dna".into();
        assert!(!explorer.visible(&record));
        explorer.molecule.clear();
        explorer.kind = "project".into();
        assert!(!explorer.visible(&record));
    }

    #[test]
    fn sequence_copy_and_numbered_display_preserve_order() {
        let sequence = "ACGT".repeat(23);
        assert_eq!(wrapped_sequence(&sequence).replace('\n', ""), sequence);
        let numbered = numbered_sequence(&sequence);
        assert!(numbered.starts_with("      1  "));
        assert!(numbered.contains("\n     81  "));
    }

    #[test]
    fn markdown_tables_keep_cell_content_and_stop_before_the_next_paragraph() {
        let lines = [
            "| Construct | Status |",
            "| :--- | ---: |",
            "| `example@1` | **Needs review** |",
            "",
            "Following paragraph",
        ];
        let (end, table) = markdown_table(&lines, 0).unwrap();
        assert_eq!(end, 3);
        assert_eq!(
            table,
            vec![
                vec!["Construct", "Status"],
                vec!["`example@1`", "**Needs review**"]
            ]
        );
        assert!(markdown_table(&["Not | a table", "Ordinary prose"], 0).is_none());
        assert!(markdown_table(&["A | B", "--- | invalid"], 0).is_none());
    }
    #[test]
    fn hierarchy_retains_parent_context_for_matching_children_and_keeps_orphans() {
        let parent = json!({"ref":"construct:parent@2","kind":"construct","molecule_type":"dna","molecular_form":"plasmid"});
        let child = json!({"ref":"construct:child@1","kind":"construct","molecule_type":"protein","parent_ref":"construct:parent@1"});
        let list = vec![parent.clone(), child.clone()];
        let expanded = BTreeSet::from(["construct:parent".into()]);
        let filtered = hierarchy(&list, &expanded, |r| text(r, "molecule_type") == "protein");
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].1, 0);
        assert_eq!(filtered[1].1, 1);
        let collapsed = BTreeSet::new();
        assert_eq!(hierarchy(&list, &collapsed, |_| true).len(), 1);
        let filtered_collapsed =
            hierarchy(&list, &collapsed, |r| text(r, "molecule_type") == "protein");
        assert_eq!(filtered_collapsed.len(), 1);
        assert_eq!(text(&filtered_collapsed[0].0, "ref"), "construct:parent@2");
        let orphan = hierarchy(&[child], &collapsed, |_| true);
        assert_eq!(orphan.len(), 1);
        assert_eq!(orphan[0].1, 0);
    }

    #[test]
    fn reopening_a_project_collapses_even_cached_hierarchy_and_ignores_saved_expansion() {
        let preferences = BTreeMap::from([(
            "library_explorer".into(),
            json!({"expanded":["construct:parent"], "query":"old search"}),
        )]);
        let mut explorer = Explorer::restore(&preferences);
        explorer.records = vec![
            json!({"ref":"construct:parent@2","kind":"construct","molecule_type":"dna","molecular_form":"plasmid"}),
            json!({"ref":"construct:child@1","kind":"construct","molecule_type":"protein","parent_ref":"construct:parent@1"}),
        ];
        assert!(explorer.expanded.is_empty());
        explorer.open_project("project:first@1");
        explorer.toggle_all_parents();
        assert_eq!(
            hierarchy(&explorer.records, &explorer.expanded, |_| true).len(),
            2
        );
        explorer.open_project("project:first@1");
        assert_eq!(
            hierarchy(&explorer.records, &explorer.expanded, |_| true).len(),
            1
        );
        assert!(explorer.query.is_empty());
        assert!(explorer.preferences().get("expanded").is_none());
    }

    #[test]
    fn toggle_all_collapses_partial_expansion_and_expands_only_linked_parents() {
        let records = vec![
            json!({"ref":"construct:first@2","kind":"construct","molecule_type":"dna","molecular_form":"plasmid"}),
            json!({"ref":"construct:first-child@1","kind":"construct","molecule_type":"protein","parent_ref":"construct:first@1"}),
            json!({"ref":"construct:second@1","kind":"construct","molecule_type":"dna","molecular_form":"plasmid"}),
            json!({"ref":"construct:second-child@1","kind":"construct","molecule_type":"protein","encoded_by_ref":"construct:second@1"}),
            json!({"ref":"construct:empty@1","kind":"construct","molecule_type":"dna","molecular_form":"plasmid"}),
            json!({"ref":"construct:standalone@1","kind":"construct","molecule_type":"protein"}),
        ];
        let parents = expandable_parents(&records);
        assert_eq!(
            parents,
            BTreeSet::from(["construct:first".into(), "construct:second".into()])
        );
        let mut explorer = Explorer {
            records,
            expanded: BTreeSet::from(["construct:first".into()]),
            ..Default::default()
        };
        explorer.toggle_all_parents();
        assert!(explorer.expanded.is_empty());
        explorer.toggle_all_parents();
        assert_eq!(explorer.expanded, parents);
        assert_eq!(
            hierarchy(&explorer.records, &explorer.expanded, |_| true).len(),
            6
        );
        explorer.open_project("project:another@1");
        assert!(explorer.expanded.is_empty());
    }

    #[test]
    fn plain_dna_and_rna_parents_keep_their_children_collapsed_until_expanded() {
        for molecule in ["dna", "rna"] {
            let parent =
                json!({"ref":"construct:source@3","kind":"construct","molecule_type":molecule});
            let child = json!({"ref":"construct:product@2","kind":"construct","molecule_type":"protein","parent_ref":"construct:source@2","derivation_kind":"derived"});
            let standalone = json!({"ref":"construct:standalone@1","kind":"construct","molecule_type":"protein"});
            let mut explorer = Explorer {
                records: vec![parent, child, standalone],
                ..Default::default()
            };
            explorer.open_project("project:sequences@1");
            assert_eq!(
                expandable_parents(&explorer.records),
                BTreeSet::from(["construct:source".into()])
            );
            let collapsed = hierarchy(&explorer.records, &explorer.expanded, |_| true);
            assert_eq!(collapsed.len(), 2);
            assert_eq!(collapsed[0].2, 1);
            assert_eq!(text(&collapsed[1].0, "ref"), "construct:standalone@1");
            explorer.toggle_all_parents();
            let expanded = hierarchy(&explorer.records, &explorer.expanded, |_| true);
            assert_eq!(expanded.len(), 3);
            assert_eq!(text(&expanded[1].0, "ref"), "construct:product@2");
            assert_eq!(expanded[1].1, 1);
            let filtered = hierarchy(&explorer.records, &explorer.expanded, |record| {
                text(record, "ref") == "construct:product@2"
            });
            assert_eq!(filtered.len(), 2);
            assert_eq!(text(&filtered[0].0, "ref"), "construct:source@3");
            explorer.open_project("project:sequences@1");
            assert!(explorer.expanded.is_empty());
        }
    }
}
