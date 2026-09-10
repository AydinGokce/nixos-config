//! Read-only shared MSA observation and exact, durable lifecycle commands.
use super::*;

const MAX_FRESH_SECONDS: f64 = 30.;
const POLL_SECONDS: u64 = 5;

#[derive(Default)]
pub(super) struct Worker {
    pub open: bool,
    status: Value,
    received: Option<Instant>,
    last_poll: Option<Instant>,
    last_click: Option<Instant>,
    serial: u64,
    invalidated: bool,
    read_error: String,
    command_message: String,
    command_failed: bool,
    receipts: BTreeMap<String, Value>,
    job_received: BTreeMap<String, Instant>,
}

fn seconds(value: &Value) -> Option<f64> {
    value.as_f64().filter(|n| n.is_finite() && *n >= 0.)
}

fn duration(seconds: f64) -> String {
    let total = seconds.max(0.).ceil() as u64;
    if total >= 3600 {
        format!("{}:{:02}:{:02}", total / 3600, total / 60 % 60, total % 60)
    } else {
        format!("{}:{:02}", total / 60, total % 60)
    }
}

/// Use the head clock plus monotonic elapsed time, never the desktop wall clock.
fn fresh_epoch(status: &Value, elapsed: f64) -> Option<f64> {
    let server = seconds(&status["server_epoch"])?;
    let checked = seconds(&status["checked_epoch"])?;
    let bound = seconds(&status["stale_after_seconds"])
        .unwrap_or(MAX_FRESH_SECONDS)
        .min(MAX_FRESH_SECONDS);
    if !elapsed.is_finite()
        || elapsed < 0.
        || checked > server + 1.
        || server - checked + elapsed > bound
        || status["stale"] == true
        || text(status, "state") == "uncertain"
    {
        return None;
    }
    Some(server + elapsed)
}

fn deadline_label(status: &Value, epoch: Option<f64>) -> String {
    let Some(now) = epoch else {
        return "Countdown unavailable".into();
    };
    if let Some(end) = seconds(&status["shutdown_epoch"]) {
        if end <= now {
            return "Checking shutdown status".into();
        }
        let reason = text(status, "shutdown_reason");
        let label = if reason.contains("idle") {
            "Idle shutdown"
        } else {
            "Shutdown"
        };
        return format!("{label} in {}", duration(end - now));
    }
    if let Some(end) = seconds(&status["hard_deadline_epoch"]) {
        return if end <= now {
            "Checking runtime limit".into()
        } else {
            format!("Runtime left {}", duration(end - now))
        };
    }
    if matches!(text(status, "state"), "absent" | "failed") {
        "No active worker countdown".into()
    } else {
        "Shutdown time not yet available".into()
    }
}

fn control_message(value: &Value) -> (String, bool) {
    if let Some(error) = value.get("error").filter(|v| !v.is_null()) {
        return (
            error.as_str().map(str::to_owned).unwrap_or_else(|| {
                let message = text(error, "message");
                if message.is_empty() {
                    error.to_string()
                } else {
                    message.into()
                }
            }),
            true,
        );
    }
    if text(value, "state") == "pending" {
        return (
            "Command accepted; waiting for the worker to acknowledge it.".into(),
            false,
        );
    }
    let result = &value["result"];
    if text(result, "status") == "rejected" {
        return (
            format!("Worker command rejected: {}", text(result, "reason")),
            true,
        );
    }
    if text(value, "action") == "extend"
        && text(result, "status") == "applied"
        && let Some(added) = seconds(&result["applied_seconds"])
    {
        return (
            format!(
                "Added {} of idle keep-warm time. {}",
                duration(added),
                text(result, "reason")
            ),
            false,
        );
    }
    for key in ["message", "reason"] {
        if !text(result, key).is_empty() {
            return (text(result, key).into(), false);
        }
    }
    (
        "Worker command completed. Refreshing its current status.".into(),
        false,
    )
}

fn unresolved_operation(op: &Value, receipts: &BTreeMap<String, Value>) -> bool {
    if op["current_connection"] != true
        || !matches!(text(op, "method"), "worker.extend" | "worker.shutdown")
    {
        return false;
    }
    match text(op, "status") {
        "queued" | "running" | "uncertain" => true,
        "complete" => {
            let result = receipts
                .get(text(&op["result"], "control_id"))
                .unwrap_or(&op["result"]);
            text(result, "state") == "pending"
        }
        _ => op.pointer("/error/uncertain") == Some(&json!(true)),
    }
}

fn control_reason(
    status: &Value,
    action: &str,
    epoch: Option<f64>,
    blocked: bool,
) -> Option<String> {
    if blocked {
        return Some(
            "A worker command is awaiting its receipt. Recover that exact command first.".into(),
        );
    }
    let Some(now) = epoch else {
        return Some("A fresh worker observation is required.".into());
    };
    if seconds(&status["hard_deadline_epoch"]).is_some_and(|end| end <= now)
        || seconds(&status["shutdown_epoch"]).is_some_and(|end| end <= now)
    {
        return Some("The last observed deadline has elapsed; refresh the worker status.".into());
    }
    if [
        "session_id",
        "invocation_id",
        "intent_sha256",
        "launch_sha256",
    ]
    .iter()
    .any(|key| text(&status["target"], key).is_empty())
    {
        return Some("No exact worker generation is available for this control.".into());
    }
    let capability = &status["controls"][action];
    if capability["enabled"] != true {
        let reason = text(capability, "reason");
        return Some(if reason.is_empty() {
            "Unavailable for this worker.".into()
        } else {
            reason.into()
        });
    }
    None
}

fn eta_label(progress: &Value, elapsed: f64, fresh: bool) -> String {
    let eta = &progress["eta"];
    let scope = match text(eta, "scope") {
        "startup" => "Startup ETA",
        "job" => "Run ETA",
        _ => "Stage ETA",
    };
    if !fresh
        || progress["stale"] == true
        || text(eta, "state") == "stale"
        || seconds(&progress["age_seconds"]).unwrap_or(0.) + elapsed > MAX_FRESH_SECONDS
    {
        return format!("{scope}: update stale");
    }
    match text(eta, "state") {
        "estimate" => seconds(&eta["seconds"])
            .map(|value| {
                if value <= elapsed {
                    format!("{scope}: updating estimate")
                } else {
                    format!("{scope}: ~{}", duration(value - elapsed))
                }
            })
            .unwrap_or_else(|| format!("{scope}: unknown")),
        "range" => match (
            seconds(&eta["lower_seconds"]),
            seconds(&eta["upper_seconds"]),
        ) {
            (Some(_), Some(high)) if high <= elapsed => format!("{scope}: updating estimate"),
            (Some(low), Some(high)) if high >= low => format!(
                "{scope}: ~{} - {}",
                duration((low - elapsed).max(0.)),
                duration((high - elapsed).max(0.))
            ),
            _ => format!("{scope}: unknown"),
        },
        _ => format!("{scope}: unknown"),
    }
}

impl Worker {
    fn elapsed(&self) -> f64 {
        self.received
            .map_or(f64::INFINITY, |at| at.elapsed().as_secs_f64())
    }
    fn epoch(&self, connected: bool) -> Option<f64> {
        if !connected || !self.read_error.is_empty() || self.invalidated {
            return None;
        }
        fresh_epoch(&self.status, self.elapsed())
    }
    fn state_label(&self, connected: bool) -> (&'static str, Color32) {
        if self.received.is_none() {
            return ("MSA UNKNOWN", AMBER);
        }
        if self.epoch(connected).is_none() {
            return ("MSA STALE", AMBER);
        }
        match text(&self.status, "state") {
            "absent" => ("MSA OFFLINE", Color32::GRAY),
            "starting" => ("MSA STARTING", AMBER),
            "warming" => ("MSA WARMING", AMBER),
            "ready" | "idle" => ("MSA ONLINE", GREEN),
            "busy" => ("MSA BUSY", GREEN),
            "closing" => ("MSA CLOSING", AMBER),
            "failed" => ("MSA FAILED", RED),
            _ => ("MSA UNKNOWN", AMBER),
        }
    }
    fn age_label(&self) -> String {
        let age = seconds(&self.status["server_epoch"])
            .zip(seconds(&self.status["checked_epoch"]))
            .map(|(server, checked)| (server - checked).max(0.) + self.elapsed());
        match age.filter(|age| age.is_finite()) {
            Some(age) => format!("Last observation {} ago", duration(age)),
            None => "No worker observation received".into(),
        }
    }
}

impl Workbench {
    pub(super) fn worker_observed_job(&mut self, job: &Value) {
        let id = text(job, "job_id");
        if !id.is_empty() {
            self.worker.job_received.insert(id.into(), Instant::now());
        }
    }

    pub(super) fn worker_job_eta(&self, job: &Value) -> Option<String> {
        let progress = &job["progress"];
        if !progress["eta"].is_object() {
            return None;
        }
        let elapsed = self
            .worker
            .job_received
            .get(text(job, "job_id"))
            .map_or(f64::INFINITY, |at| at.elapsed().as_secs_f64());
        Some(eta_label(progress, elapsed, self.connected))
    }

    pub(super) fn worker_refresh(&mut self) {
        if !self.connected
            || self
                .pending
                .values()
                .any(|p| matches!(p.purpose, Purpose::WorkerStatus(_)))
        {
            return;
        }
        self.worker.last_poll = Some(Instant::now());
        self.request(
            "worker.status",
            json!({}),
            Purpose::WorkerStatus(self.worker.serial),
        );
    }

    fn worker_operation(&self) -> Option<Value> {
        self.session
            .as_ref()?
            .retryable_operations()
            .into_iter()
            .find(|op| unresolved_operation(op, &self.worker.receipts))
    }

    pub(super) fn worker_poll(&mut self) {
        if self
            .worker
            .last_poll
            .is_none_or(|at| at.elapsed() >= Duration::from_secs(POLL_SECONDS))
        {
            self.worker_refresh();
            if let Some(op) = self.worker_operation()
                && text(&op, "status") == "complete"
                && !text(&op["result"], "control_id").is_empty()
            {
                let control_id = text(&op["result"], "control_id").to_owned();
                self.request(
                    "worker.control_get",
                    json!({"control_id":control_id}),
                    Purpose::WorkerReceipt(control_id),
                );
            }
        }
    }

    pub(super) fn worker_received_status(&mut self, serial: u64, value: Value) {
        if serial != self.worker.serial {
            self.worker.last_poll = None;
            return;
        }
        if value["schema"] != 1
            || !value["state"].is_string()
            || seconds(&value["server_epoch"]).is_none()
        {
            self.worker.read_error = "Invalid worker status reply; controls disabled.".into();
            return;
        }
        self.worker.status = value;
        self.worker.received = Some(Instant::now());
        self.worker.invalidated = false;
        self.worker.read_error.clear();
        self.failures
            .retain(|failure| !matches!(failure.purpose, Purpose::WorkerStatus(_)));
    }

    pub(super) fn worker_received_control(&mut self, value: Value) {
        if text(&value, "control_id").is_empty()
            || !matches!(text(&value, "state"), "pending" | "complete" | "failed")
        {
            self.worker.command_message =
                "The worker command returned an invalid receipt. Recover its exact saved request."
                    .into();
            return;
        }
        let old = self
            .worker
            .receipts
            .insert(text(&value, "control_id").into(), value.clone());
        (self.worker.command_message, self.worker.command_failed) = control_message(&value);
        if old
            .as_ref()
            .is_none_or(|old| text(old, "state") != text(&value, "state"))
        {
            self.worker.serial += 1;
            self.worker.last_poll = None;
            self.worker.invalidated = true;
            self.worker_refresh();
        }
    }

    pub(super) fn worker_failed(&mut self, purpose: &Purpose, message: &str) {
        match purpose {
            Purpose::WorkerStatus(serial) if *serial == self.worker.serial => {
                self.worker.read_error = message.into();
            }
            Purpose::WorkerControl(_) | Purpose::WorkerReceipt(_) => {
                self.worker.command_message = message.into();
                self.worker.command_failed = true;
                self.worker.last_poll = None;
            }
            _ => {}
        }
    }

    fn worker_blocked(&self) -> bool {
        self.worker_operation().is_some()
            || self
                .pending
                .values()
                .any(|p| matches!(p.purpose, Purpose::WorkerControl(_)))
            || self
                .worker
                .last_click
                .is_some_and(|at| at.elapsed() < Duration::from_millis(750))
    }

    fn worker_send_control(&mut self, action: &str) {
        let epoch = self.worker.epoch(self.connected);
        if epoch.is_none()
            || control_reason(&self.worker.status, action, epoch, self.worker_blocked()).is_some()
        {
            return;
        }
        let method = format!("worker.{action}");
        let params = json!({"request_key":uid(),"target":self.worker.status["target"]});
        self.worker.last_click = Some(Instant::now());
        self.worker.command_message.clear();
        self.worker.command_failed = false;
        self.worker.serial += 1;
        if self
            .request(&method, params, Purpose::WorkerControl(method.clone()))
            .is_none()
        {
            self.worker.command_message =
                "The worker command could not be saved or sent. See the console.".into();
        }
    }

    pub(super) fn worker_indicator(&mut self, ui: &mut egui::Ui) {
        let epoch = self.worker.epoch(self.connected);
        let (label, color) = self.worker.state_label(self.connected);
        let suffix = if epoch.is_none() && self.worker.received.is_some() {
            format!(" · {}", self.worker.age_label())
        } else if let Some(end) = seconds(&self.worker.status["shutdown_epoch"])
            && let Some(now) = epoch
            && end > now
        {
            format!(" · {}", duration(end - now))
        } else {
            String::new()
        };
        if ui.small_button(RichText::new(format!("{label}{suffix}")).color(color).strong())
            .on_hover_text("Shared private MSA worker. Inspect startup, shutdown countdown and keep-warm controls.")
            .clicked()
        {
            self.worker.open = !self.worker.open;
            self.worker_refresh();
        }
    }

    pub(super) fn worker_run_summary(&mut self, ui: &mut egui::Ui) {
        egui::Frame::group(ui.style()).show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                let (label, color) = self.worker.state_label(self.connected);
                ui.colored_label(color, label);
                ui.small(deadline_label(
                    &self.worker.status,
                    self.worker.epoch(self.connected),
                ));
                if ui.small_button("Worker details").clicked() {
                    self.worker.open = true;
                }
            });
            self.worker_progress(ui);
            if self.worker.epoch(self.connected).is_none() {
                ui.small(self.worker.age_label());
            }
        });
    }

    fn worker_progress(&self, ui: &mut egui::Ui) {
        let progress = &self.worker.status["progress"];
        if !progress.is_object() {
            let message = text(&self.worker.status, "message");
            if !message.is_empty() {
                ui.label(message);
            }
            if matches!(text(&self.worker.status, "state"), "starting" | "warming") {
                ui.small("Startup ETA: unknown; waiting for measured progress.");
            }
            return;
        }
        ui.horizontal_wrapped(|ui| {
            let scope = if text(progress, "scope") == "gpu" {
                "GPU"
            } else {
                "MSA"
            };
            ui.strong(format!("{scope} / {}", text(progress, "stage")));
            ui.small(text(progress, "stage_state"));
        });
        ui.label(text(progress, "message"));
        if let (Some(done), Some(total)) =
            (seconds(&progress["completed"]), seconds(&progress["total"]))
            && total > 0.
            && done <= total
        {
            ui.add(
                egui::ProgressBar::new((done / total) as f32)
                    .text(format!("{done:.0} / {total:.0} {}", text(progress, "unit"))),
            );
        }
        if seconds(&progress["total"]).is_none()
            && let Some(done) = seconds(&progress["completed"])
        {
            ui.small(format!("{done:.0} {}", text(progress, "unit")));
        }
        ui.small(eta_label(
            progress,
            self.worker.elapsed(),
            self.worker.epoch(self.connected).is_some(),
        ));
        if !text(&progress["eta"], "basis").is_empty() {
            ui.small(text(&progress["eta"], "basis"));
        }
    }

    pub(super) fn worker_dialog(&mut self, ctx: &egui::Context) {
        if !self.worker.open {
            return;
        }
        let mut open = self.worker.open;
        egui::Window::new("Shared MSA worker").id(egui::Id::new("msa-worker-controls"))
            .open(&mut open).default_width(510.).resizable(true).show(ctx, |ui| {
            let epoch = self.worker.epoch(self.connected);
            let (label,color) = self.worker.state_label(self.connected);
            ui.horizontal(|ui| {
                ui.colored_label(color, RichText::new(label).strong());
                if ui.add_enabled(!self.pending.values().any(|p| matches!(p.purpose,Purpose::WorkerStatus(_))),egui::Button::new("Refresh")).clicked() { self.worker_refresh(); }
            });
            ui.small(self.worker.age_label());
            ui.separator();
            ui.strong(deadline_label(&self.worker.status, epoch));
            if let (Some(now),Some(end)) = (epoch, seconds(&self.worker.status["hard_deadline_epoch"])) {
                ui.small(if end > now { format!("Fixed runtime limit in {}", duration(end-now)) } else { "Fixed runtime limit elapsed; awaiting observation".into() });
            }
            let active = !text(&self.worker.status,"active_request_id").is_empty();
            let queued = self.worker.status["queued_requests"].as_u64().unwrap_or(0);
            ui.small(format!("{} active search · {queued} accepted / queued", u8::from(active)));
            self.worker_progress(ui);
            ui.separator();
            let blocked = self.worker_blocked();
            let stale_reason = "A fresh worker observation is required.".to_owned();
            let extend_reason = if epoch.is_none() { Some(stale_reason.clone()) } else { control_reason(&self.worker.status,"extend",epoch,blocked) };
            let shutdown_reason = if epoch.is_none() { Some(stale_reason) } else { control_reason(&self.worker.status,"shutdown",epoch,blocked) };
            let count = queued + u64::from(active);
            let shutdown_label = if count > 0 { format!("Finish {count} searches and shut down") } else { "Shut down now".into() };
            ui.horizontal_wrapped(|ui| {
                if ui.add_enabled(extend_reason.is_none(),egui::Button::new("+15 minutes"))
                    .on_hover_text(extend_reason.as_deref().unwrap_or("Add 15 minutes of idle keep-warm time, within the original runtime limit.")).clicked() { self.worker_send_control("extend"); }
                if ui.add_enabled(shutdown_reason.is_none(),egui::Button::new(shutdown_label))
                    .on_hover_text(shutdown_reason.as_deref().unwrap_or("Reject new searches, finish already accepted work, then shut down this shared MSA worker.")).clicked() { self.worker_send_control("shutdown"); }
            });
            for reason in [extend_reason.as_deref(),shutdown_reason.as_deref()].into_iter().flatten().collect::<BTreeSet<_>>() { ui.small(reason); }
            ui.small("+15 minutes keeps this shared MSA service warm within its original paid runtime. GPU prediction workers have separate lifetimes.");
            ui.small("Shutdown finishes accepted searches and refuses new ones. It does not cancel unrelated predictions.");
            if !self.worker.read_error.is_empty() { ui.colored_label(RED,&self.worker.read_error); }
            if !self.worker.command_message.is_empty() { ui.colored_label(if self.worker.command_failed { RED } else { AMBER },&self.worker.command_message); }
            if let Some(op) = self.worker_operation() {
                ui.separator();
                ui.small(format!("Saved {} command: {}",text(&op,"method"),text(&op,"status")));
                let id = text(&op,"id").to_owned();
                if text(&op,"status") == "complete" {
                    ui.small("Waiting for the durable head receipt; checking automatically.");
                } else if ui.add_enabled(!self.pending.contains_key(&id),egui::Button::new("Recover exact command")).clicked() {
                    self.retry(&id,Purpose::WorkerControl(text(&op,"method").into()),text(&op,"method").into());
                }
            }
        });
        self.worker.open = open;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn status() -> Value {
        json!({"schema":1,"state":"idle","server_epoch":1000.,"checked_epoch":998.,"stale_after_seconds":30,
            "shutdown_epoch":1100.,"shutdown_reason":"idle","hard_deadline_epoch":2000.,"target":{"session_id":"exact","invocation_id":"one","intent_sha256":"a","launch_sha256":"b"},
            "controls":{"extend":{"enabled":true},"shutdown":{"enabled":true}}})
    }
    #[test]
    fn countdown_uses_head_clock_and_expires_observation_without_local_wall_clock() {
        let status = status();
        assert_eq!(fresh_epoch(&status, 10.), Some(1010.));
        assert_eq!(
            deadline_label(&status, fresh_epoch(&status, 10.)),
            "Idle shutdown in 1:30"
        );
        assert_eq!(fresh_epoch(&status, 29.), None);
        assert_eq!(deadline_label(&status, None), "Countdown unavailable");
    }
    #[test]
    fn elapsed_deadlines_never_claim_zero_is_a_fresh_shutdown_countdown() {
        let mut status = status();
        status["shutdown_epoch"] = json!(1005.);
        assert_eq!(
            deadline_label(&status, Some(1010.)),
            "Checking shutdown status"
        );
        assert!(control_reason(&status, "extend", Some(1010.), false).is_some());
    }
    #[test]
    fn freshness_fails_closed_on_bad_epoch_explicit_stale_or_uncertain() {
        for (key, value) in [
            ("checked_epoch", json!(1100.)),
            ("stale", json!(true)),
            ("state", json!("uncertain")),
            ("server_epoch", json!(null)),
        ] {
            let mut s = status();
            s[key] = value;
            assert_eq!(fresh_epoch(&s, 0.), None);
        }
        assert_eq!(fresh_epoch(&status(), f64::INFINITY), None);
    }
    #[test]
    fn capability_reason_and_pending_command_disable_actions() {
        let mut s = status();
        s["controls"]["extend"] = json!({"enabled":false,"reason":"The full 15 minutes exceeds the fixed runtime limit."});
        assert!(
            control_reason(&s, "extend", Some(1000.), false)
                .unwrap()
                .contains("full 15 minutes")
        );
        assert!(
            control_reason(&s, "shutdown", Some(1000.), true)
                .unwrap()
                .contains("awaiting")
        );
        assert_eq!(control_reason(&s, "shutdown", Some(1000.), false), None);
    }
    #[test]
    fn busy_without_idle_deadline_shows_fixed_runtime_without_inventing_idle() {
        let mut s = status();
        s["state"] = json!("busy");
        s["shutdown_epoch"] = Value::Null;
        assert_eq!(deadline_label(&s, Some(1010.)), "Runtime left 16:30");
    }
    #[test]
    fn eta_keeps_stage_scope_and_stale_unknown_distinct() {
        let p =
            json!({"eta":{"state":"range","scope":"stage","lower_seconds":60,"upper_seconds":120}});
        assert_eq!(eta_label(&p, 10., true), "Stage ETA: ~0:50 - 1:50");
        assert_eq!(eta_label(&p, 10., false), "Stage ETA: update stale");
        assert_eq!(
            eta_label(&json!({"eta":{"state":"unknown","scope":"job"}}), 0., true),
            "Run ETA: unknown"
        );
    }
    #[test]
    fn control_receipt_reports_rejection_and_actual_partial_credit() {
        let rejected = json!({"state":"complete","action":"extend","result":{"status":"rejected","reason":"Hard runtime cap","applied_seconds":0}});
        let (message, failed) = control_message(&rejected);
        assert!(failed && message.contains("rejected") && message.contains("Hard runtime cap"));
        let applied = json!({"state":"complete","action":"extend","result":{"status":"applied","applied_seconds":600,"reason":"Bounded by fixed runtime"}});
        let (message, failed) = control_message(&applied);
        assert!(!failed && message.contains("10:00") && !message.contains("15"));
    }

    #[test]
    fn pending_durable_receipt_survives_restart_and_old_endpoint_does_not_block() {
        let mut op = json!({"current_connection":true,"method":"worker.extend","status":"complete","result":{"control_id":"one","state":"pending"}});
        assert!(unresolved_operation(&op, &BTreeMap::new()));
        let receipts =
            BTreeMap::from([("one".into(), json!({"control_id":"one","state":"complete"}))]);
        assert!(!unresolved_operation(&op, &receipts));
        op["status"] = json!("uncertain");
        assert!(unresolved_operation(&op, &receipts));
        op["current_connection"] = json!(false);
        assert!(!unresolved_operation(&op, &receipts));
    }
}
