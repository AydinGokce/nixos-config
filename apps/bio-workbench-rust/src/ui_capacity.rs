//! Provider capacity is independent of the running shared MSA session.
use super::*;

const POLL_SECONDS: u64 = 5;
const MAX_FRESH_SECONDS: f64 = 120.;

#[derive(Clone, PartialEq, Debug)]
pub(super) struct Request {
    endpoint: String,
    serial: u64,
}

#[derive(Default)]
pub(super) struct Capacity {
    pub snapshot: Value,
    received: Option<Instant>,
    attempted: Option<Instant>,
    pending: Option<Request>,
    serial: u64,
    pub error: String,
}

fn seconds(value: &Value) -> Option<f64> {
    value.as_f64().filter(|n| n.is_finite() && *n >= 0.)
}

/// A cached server reply must not restart the age of its original observation.
fn snapshot_age(snapshot: &Value, field: &str, elapsed: f64) -> Option<f64> {
    let server = seconds(&snapshot["server_epoch"])?;
    let observed = seconds(&snapshot[field])?;
    if !elapsed.is_finite() || elapsed < 0. || observed > server + 1. {
        return None;
    }
    Some((server - observed).max(0.) + elapsed)
}

impl Capacity {
    pub fn begin(&mut self, endpoint: String, refresh: bool, now: Instant) -> Option<Request> {
        if self.pending.is_some()
            || (!refresh
                && self.attempted.is_some_and(|at| {
                    now.saturating_duration_since(at) < Duration::from_secs(POLL_SECONDS)
                }))
        {
            return None;
        }
        self.serial += 1;
        let request = Request {
            endpoint,
            serial: self.serial,
        };
        self.pending = Some(request.clone());
        self.attempted = Some(now);
        Some(request)
    }

    fn accepts(&self, request: &Request, endpoint: &str) -> bool {
        request.endpoint == endpoint && self.pending.as_ref() == Some(request)
    }

    pub fn fail(&mut self, request: &Request, endpoint: &str, error: &str) -> bool {
        if !self.accepts(request, endpoint) {
            return false;
        }
        self.pending = None;
        self.error = error.into();
        true
    }

    pub fn receive(
        &mut self,
        request: &Request,
        endpoint: &str,
        snapshot: Value,
        now: Instant,
    ) -> bool {
        if !self.accepts(request, endpoint) {
            return false;
        }
        self.pending = None;
        if snapshot["schema"] != 1
            || !matches!(text(&snapshot, "state"), "ready" | "partial" | "error")
            || seconds(&snapshot["server_epoch"]).is_none()
            || (!snapshot["msa_available"].is_boolean() && !snapshot["msa_available"].is_null())
            || !snapshot["gpus"].is_array()
        {
            self.error = "Invalid capacity reply. Availability is unknown.".into();
            return true;
        }
        self.snapshot = snapshot;
        self.received = Some(now);
        self.error.clear();
        true
    }

    fn elapsed(&self, now: Instant) -> f64 {
        self.received.map_or(f64::INFINITY, |at| {
            now.saturating_duration_since(at).as_secs_f64()
        })
    }

    pub fn refreshing(&self) -> bool {
        self.pending.is_some() || (self.error.is_empty() && self.snapshot["refreshing"] == true)
    }

    pub fn pending(&self) -> bool {
        self.pending.is_some()
    }

    pub fn fresh(&self, connected: bool, now: Instant) -> bool {
        if !connected || !self.error.is_empty() || text(&self.snapshot, "state") == "error" {
            return false;
        }
        let field = if self.snapshot["observed_epoch"].is_number() {
            "observed_epoch"
        } else {
            "checked_epoch"
        };
        let bound = seconds(&self.snapshot["stale_after_seconds"])
            .unwrap_or(MAX_FRESH_SECONDS)
            .min(MAX_FRESH_SECONDS);
        snapshot_age(&self.snapshot, field, self.elapsed(now))
            .is_some_and(|age| age <= bound && self.snapshot["stale"] != true)
    }

    pub fn label(&self, connected: bool, now: Instant) -> (&'static str, Color32) {
        if !connected || !self.error.is_empty() || text(&self.snapshot, "state") == "error" {
            return ("MSA availability unknown", AMBER);
        }
        if self.received.is_none() {
            return if self.pending() {
                ("Checking MSA availability", AMBER)
            } else {
                ("MSA availability unknown", AMBER)
            };
        }
        if !self.fresh(connected, now) {
            return ("MSA availability stale", AMBER);
        }
        match self.snapshot["msa_available"].as_bool() {
            Some(true) => ("MSA available", GREEN),
            Some(false) => ("MSA unavailable", Color32::GRAY),
            None => ("MSA availability unknown", AMBER),
        }
    }

    pub fn age_label(&self, now: Instant) -> String {
        match snapshot_age(&self.snapshot, "checked_epoch", self.elapsed(now)) {
            Some(age) => format!("Last update {} sec ago", age.floor() as u64),
            None => "Last update: no complete snapshot yet".into(),
        }
    }
}

fn quantity(value: &Value, suffix: &str) -> String {
    seconds(value).map_or_else(|| "—".into(), |n| format!("{n:.0}{suffix}"))
}

pub(super) fn gpu_table(ui: &mut egui::Ui, capacity: &Capacity, connected: bool) {
    let current = capacity.fresh(connected, Instant::now());
    let gpus = rows(&capacity.snapshot, "gpus");
    ui.horizontal_wrapped(|ui| {
        ui.strong(if current {
            "Available Verda GPUs"
        } else {
            "Last observed Verda GPUs"
        });
        ui.weak(format!("{} offers", gpus.len()));
        if text(&capacity.snapshot, "state") == "partial" {
            ui.colored_label(AMBER, "Partial update");
        }
    });
    ui.small("MSA eligibility uses the configured full-database worker's RAM, region and price limits. CPU capacity is also included in the availability indicator.");
    let message = text(&capacity.snapshot, "msa_message");
    if !message.is_empty() {
        ui.label(message);
    }
    if !capacity.error.is_empty() {
        ui.colored_label(AMBER, &capacity.error);
    } else if let Some(error) = capacity
        .snapshot
        .get("error")
        .filter(|value| !value.is_null())
    {
        ui.colored_label(
            AMBER,
            error
                .as_str()
                .unwrap_or("Provider capacity check was incomplete."),
        );
    }
    if !current && !gpus.is_empty() {
        ui.colored_label(
            AMBER,
            "This previous GPU list may have changed. Refresh to check again.",
        );
    }
    if gpus.is_empty() {
        ui.weak(if current && text(&capacity.snapshot, "state") == "ready" {
            "No GPUs were reported available."
        } else {
            "No current GPU list available."
        });
        return;
    }
    egui::ScrollArea::both()
        .id_salt("msa-capacity-gpus")
        .max_height(260.)
        .auto_shrink([false, true])
        .show(ui, |ui| {
            // A horizontal scroll area starts with provisional grid widths.
            // Keep cells on one line so the grid measures their full width and
            // scrolls long GPU names instead of wrapping them character by character.
            ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
            egui::Grid::new("msa-capacity-grid")
                .num_columns(7)
                .min_col_width(64.)
                .striped(true)
                .spacing([14., 6.])
                .show(ui, |ui| {
                    for title in [
                        "GPU / instance",
                        "Region",
                        "Contract",
                        "Total VRAM",
                        "Host RAM",
                        "$/hour",
                        "MSA",
                    ] {
                        ui.strong(title);
                    }
                    ui.end_row();
                    for gpu in gpus {
                        let instance = text(gpu, "instance_type");
                        let name = text(gpu, "name");
                        let count = gpu["gpu_count"].as_u64().unwrap_or(1);
                        ui.label(format!(
                            "{count}× {}",
                            if name.is_empty() { instance } else { name }
                        ))
                        .on_hover_text(instance);
                        ui.label(text(gpu, "location"));
                        ui.label(text(gpu, "contract"));
                        ui.monospace(quantity(&gpu["gpu_memory_gib"], " GiB"));
                        ui.monospace(quantity(&gpu["ram_gib"], " GiB"));
                        ui.monospace(
                            seconds(&gpu["price_hourly"])
                                .map_or_else(|| "—".into(), |n| format!("{n:.3}")),
                        );
                        let eligible = gpu["msa_eligible"] == true;
                        ui.colored_label(
                            if eligible && current {
                                GREEN
                            } else {
                                Color32::GRAY
                            },
                            if eligible { "Eligible" } else { "Not eligible" },
                        )
                        .on_hover_text(text(gpu, "reason"));
                        ui.end_row();
                    }
                });
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(available: Value) -> Value {
        json!({"schema":1,"server_epoch":1000.,"checked_epoch":998.,"state":"ready",
            "stale_after_seconds":120,"msa_available":available,"gpus":[]})
    }

    fn receive(capacity: &mut Capacity, snapshot: Value, now: Instant) {
        let request = capacity.begin("head-one".into(), true, now).unwrap();
        assert!(capacity.receive(&request, "head-one", snapshot, now));
    }

    #[test]
    fn startup_checks_immediately_and_five_second_poll_does_not_overlap() {
        let now = Instant::now();
        let mut capacity = Capacity::default();
        let request = capacity.begin("head-one".into(), false, now).unwrap();
        assert_eq!(capacity.label(true, now).0, "Checking MSA availability");
        assert!(
            capacity
                .begin("head-one".into(), true, now + Duration::from_secs(5))
                .is_none()
        );
        assert!(capacity.receive(&request, "head-one", snapshot(json!(false)), now));
        assert!(
            capacity
                .begin("head-one".into(), false, now + Duration::from_secs(4))
                .is_none()
        );
        assert!(
            capacity
                .begin("head-one".into(), false, now + Duration::from_secs(5))
                .is_some()
        );
    }

    #[test]
    fn manual_refresh_bypasses_timer_and_errors_stay_unknown_until_recovered() {
        let now = Instant::now();
        let mut capacity = Capacity::default();
        receive(&mut capacity, snapshot(json!(false)), now);
        assert_eq!(capacity.label(true, now).0, "MSA unavailable");
        let request = capacity
            .begin("head-one".into(), true, now + Duration::from_secs(1))
            .unwrap();
        assert!(capacity.fail(&request, "head-one", "Provider offline"));
        assert_eq!(capacity.label(true, now).0, "MSA availability unknown");
        assert!(!capacity.fresh(true, now));
        assert!(
            capacity
                .begin("head-one".into(), false, now + Duration::from_secs(2))
                .is_none()
        );
        receive(
            &mut capacity,
            snapshot(json!(true)),
            now + Duration::from_secs(3),
        );
        assert_eq!(
            capacity.label(true, now + Duration::from_secs(3)).0,
            "MSA available"
        );
    }

    #[test]
    fn age_includes_cached_server_time_and_expires_without_desktop_wall_clock() {
        let now = Instant::now();
        let mut capacity = Capacity::default();
        receive(&mut capacity, snapshot(json!(true)), now);
        assert_eq!(
            capacity.age_label(now + Duration::from_secs(10)),
            "Last update 12 sec ago"
        );
        assert_eq!(
            capacity.label(true, now + Duration::from_secs(118)).0,
            "MSA available"
        );
        assert_eq!(
            capacity.label(true, now + Duration::from_secs(119)).0,
            "MSA availability stale"
        );
        assert_eq!(capacity.label(false, now).0, "MSA availability unknown");
        assert_eq!(
            snapshot_age(&snapshot(json!(true)), "checked_epoch", f64::INFINITY),
            None
        );
    }

    #[test]
    fn old_endpoint_and_superseded_reply_never_change_current_observation() {
        let now = Instant::now();
        let mut capacity = Capacity::default();
        let old = capacity.begin("head-one".into(), false, now).unwrap();
        assert!(!capacity.receive(&old, "head-two", snapshot(json!(true)), now));
        assert!(!capacity.fail(&old, "head-two", "Old head failed"));
        assert!(capacity.snapshot.is_null());
        assert!(capacity.receive(&old, "head-one", snapshot(json!(false)), now));
        let current = capacity.begin("head-one".into(), true, now).unwrap();
        assert!(!capacity.receive(&old, "head-one", snapshot(json!(true)), now));
        assert!(!capacity.fail(&old, "head-one", "Late failure"));
        assert!(capacity.receive(&current, "head-one", snapshot(json!(false)), now));
        assert_eq!(capacity.label(true, now).0, "MSA unavailable");
    }

    #[test]
    fn partial_result_does_not_claim_complete_update_or_unknown_capacity_is_absent() {
        let now = Instant::now();
        let mut capacity = Capacity::default();
        let mut partial = snapshot(Value::Null);
        partial["checked_epoch"] = Value::Null;
        partial["observed_epoch"] = json!(1000.);
        partial["state"] = json!("partial");
        receive(&mut capacity, partial.clone(), now);
        assert_eq!(capacity.label(true, now).0, "MSA availability unknown");
        assert_eq!(
            capacity.age_label(now),
            "Last update: no complete snapshot yet"
        );
        partial["msa_available"] = json!(true);
        receive(&mut capacity, partial, now);
        assert_eq!(capacity.label(true, now).0, "MSA available");
    }

    #[test]
    fn invalid_or_failed_responses_cannot_advertise_available_capacity() {
        let now = Instant::now();
        for mutate in ["error", "bad-schema", "future-checked"] {
            let mut capacity = Capacity::default();
            let mut bad = snapshot(json!(true));
            match mutate {
                "error" => bad["state"] = json!("error"),
                "bad-schema" => bad["schema"] = json!(2),
                _ => bad["checked_epoch"] = json!(1010.),
            }
            receive(&mut capacity, bad, now);
            assert_ne!(capacity.label(true, now).0, "MSA available");
        }
    }

    #[test]
    fn gpu_table_headers_and_long_names_remain_single_line_at_narrow_and_wide_sizes() {
        fn find<'a>(shape: &'a egui::Shape, label: &str) -> Option<&'a egui::epaint::TextShape> {
            match shape {
                egui::Shape::Text(text) if text.galley.job.text == label => Some(text),
                egui::Shape::Vec(shapes) => shapes.iter().find_map(|shape| find(shape, label)),
                _ => None,
            }
        }
        let now = Instant::now();
        let mut capacity = Capacity::default();
        let mut value = snapshot(json!(true));
        value["gpus"] = json!([{
            "instance_type":"1RTXPRO6000.48V","name":"NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
            "location":"FIN-02","contract":"regular","gpu_count":1,
            "gpu_memory_gib":89.,"ram_gib":168.,"price_hourly":1.93,
            "msa_eligible":false,"reason":"Insufficient host RAM"
        }]);
        receive(&mut capacity, value, now);
        for width in [600., 900.] {
            let context = egui::Context::default();
            ui_style::configure(&context);
            let input = || egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    Vec2::new(width, 500.),
                )),
                ..Default::default()
            };
            // The first pass sizes the grid; the next pass must preserve a readable layout.
            for _ in 0..2 {
                let output = context.run(input(), |context| {
                    egui::CentralPanel::default()
                        .show(context, |ui| gpu_table(ui, &capacity, true));
                });
                for label in [
                    "GPU / instance",
                    "Region",
                    "1× NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
                ] {
                    let shape = output
                        .shapes
                        .iter()
                        .find_map(|shape| find(&shape.shape, label))
                        .unwrap_or_else(|| panic!("{label} is painted at width {width}"));
                    assert_eq!(
                        shape.galley.rows.len(),
                        1,
                        "{label} must not wrap at width {width}"
                    );
                }
            }
        }
    }
}
